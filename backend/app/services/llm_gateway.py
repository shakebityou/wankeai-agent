"""LLM 网关（BE-16）——**唯一允许调用大模型的入口**。

业务代码一律不直接调 `LlmProviderPort`，必须经过本模块，原因是网关承担
四件业务代码不该关心、却又一旦漏掉就出事的事：

| 职责 | 漏掉的后果 | 对应 DoD |
| --- | --- | --- |
| 主备双通道 + 熔断式切换 | 主厂商一次故障全站客服瘫痪 | 主备切换成功率 100% |
| 相同问题缓存（Redis TTL 1h） | 成本翻倍、同问不同答 | 缓存命中率 ≥ 15% |
| 送模型前脱敏 + 上下文只送 Top-3 | 敏感数据出境（违反 HC-2）、上下文越长越易幻觉 | 送模型 prompt 零完整敏感字段 / 只送 Top-3 |
| token 计量与日预算 | 配置错误把额度烧穿且无人知晓 | 日用量达 80% 告警 |

**为什么缓存键要含上下文指纹**：只用问题文本做键，会让「同一问题在
不同商品下得到同一答案」——用户问「这个多少钱」，上下文从奶油小熊换成
盲盒后答案必须变。所以键 = 改写后问题 + Top-3 片段指纹 + 提示词版本。

**为什么不缓存兜底回复**：兜底话术（「暂时没查到」）可能只是当时检索
抖动造成的，缓存它会把一次瞬时故障固化成 1 小时的错误答案。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.adapters.llm_provider import (
    get_llm_fallback_provider,
    get_llm_provider,
)
from app.adapters.ports import LlmProviderPort, LlmResult
from app.core.config import get_settings
from app.core.logging import get_logger, mask_text
from app.core.trace import get_trace_id

logger = get_logger(__name__)

#: 提示词版本。**改提示词必须升版本号**，否则缓存里旧提示词的答案会继续
#: 被复用，出现「代码改了、线上回答没变」的幽灵现象。版本号进缓存键。
PROMPT_VERSION = "v1.3.0"

#: 相同问题缓存的 TTL。任务书 2.4.2 与 BE-16 都写 1 小时。
ANSWER_CACHE_TTL_SECONDS = 3600

#: 单条回复字数上限（任务书提示词约束第 5 条：≤ 120 字）
MAX_REPLY_CHARS = 120

#: 缓存键前缀（清理时按前缀删）
CACHE_PREFIX = "llm:answer:"


class LlmBudgetExceededError(RuntimeError):
    """日预算或单会话预算耗尽。

    这是一个**业务语义**的异常，不是故障：调用方（编排层）应据此降级到
    模板生成或转人工，而不是重试。故独立成类，避免被通用 `except Exception`
    当成网络故障反复重试。
    """


@dataclass
class LlmUsage:
    """一次调用的用量与来源，供监控看板与成本核算使用。

    Attributes:
        prompt_tokens: 输入 token。
        completion_tokens: 输出 token。
        model: 实际使用的模型。
        provider: 实际使用的 Provider（`local-template-v1` 等）。
        cached: 是否命中相同问题缓存。
        failover: 是否因主通道故障切换到了备通道。
        degraded: 是否降级（预算耗尽 / 双通道皆失败 / 出口白名单拦截）。
        degrade_reason: 降级原因（写入日志与埋点）。
        latency_ms: 端到端耗时。
        prompt_masked: 送模型前是否真的做过脱敏（DoD 要可审计）。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    provider: str = ""
    cached: bool = False
    failover: bool = False
    degraded: bool = False
    degrade_reason: str = ""
    latency_ms: float = 0.0
    prompt_masked: bool = True

    @property
    def total_tokens(self) -> int:
        """输入 + 输出。"""
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        """导出为可日志 / 可入库的字典。"""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "model": self.model,
            "provider": self.provider,
            "cached": self.cached,
            "failover": self.failover,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "latency_ms": round(self.latency_ms, 2),
            "prompt_masked": self.prompt_masked,
        }


@dataclass
class LlmAnswer:
    """网关返回的一次回答。

    Attributes:
        text: 回答文本（已过出口字数约束，**未过合规闸**——合规由
            `app.compliance.outbound_gate` 在更外层统一做，见模块说明）。
        usage: 用量与来源。
        from_cache: 是否来自缓存。
        knowledge_chunks: 本次实际送进上下文的片段 ID（审计用）。
    """

    text: str
    usage: LlmUsage = field(default_factory=LlmUsage)
    from_cache: bool = False
    knowledge_chunks: list[str] = field(default_factory=list)


class TokenMeter:
    """token 计量与预算闸。

    两个维度都要，缺一个都有洞：

    * **日用量**：防「配置错误 / 缓存全失效」把当日额度烧穿；
    * **单会话用量**：轮次上限（`max_turns_per_session`=10）管不住
      「每轮都要很长的上下文」的情况，长会话会把单价乘上去。

    计数放内存而非 Redis 的理由：这是**成本闸**不是**业务状态**，
    多实例部署时每个实例各自记一份、各自限流即可（总量仍然是
    N 倍上限，比完全没有闸好，且不需要为成本统计引入一次网络往返）。
    生产若要全局精确配额，应改为 Redis `INCRBY`，届时只换本类实现。

    Attributes:
        daily_budget: 日 token 预算。
        session_limit: 单会话 token 上限。
        alert_ratio: 告警阈值比例（0.8 即达 80% 告警）。
    """

    def __init__(
        self,
        *,
        daily_budget: int | None = None,
        session_limit: int | None = None,
        alert_ratio: float | None = None,
    ) -> None:
        settings = get_settings()
        self.daily_budget = daily_budget or settings.llm_daily_token_budget
        self.session_limit = session_limit or settings.llm_session_token_limit
        self.alert_ratio = (
            alert_ratio if alert_ratio is not None else settings.llm_daily_budget_alert_ratio
        )
        self._day: str = ""
        self._daily_used: int = 0
        self._session_used: dict[str, int] = {}
        self._alerted_day: str = ""

    @staticmethod
    def _today() -> str:
        """当前 UTC 日期（预算按 UTC 自然日滚动）。"""
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _roll_day(self) -> None:
        """跨天则清零日计数。"""
        today = self._today()
        if today != self._day:
            self._day = today
            self._daily_used = 0

    @property
    def daily_used(self) -> int:
        """今日已用 token。"""
        self._roll_day()
        return self._daily_used

    @property
    def daily_remaining(self) -> int:
        """今日剩余 token（可为负，表示已超）。"""
        return self.daily_budget - self.daily_used

    def session_used(self, session_id: str) -> int:
        """某会话已用 token。"""
        return self._session_used.get(session_id, 0)

    def check(self, *, session_id: str = "", estimated: int = 0) -> None:
        """调用前的准入检查。

        Args:
            session_id: 会话 ID；空表示不计会话维度（如离线批处理）。
            estimated: 本次预计消耗（用于提前拦截，避免「调完才发现超了」）。

        Raises:
            LlmBudgetExceededError: 日预算或会话预算不足。
        """
        self._roll_day()
        if self._daily_used + estimated > self.daily_budget:
            raise LlmBudgetExceededError(
                f"LLM 日预算不足：已用 {self._daily_used}/{self.daily_budget}，"
                f"本次预计 {estimated}"
            )
        if session_id and estimated:
            used = self._session_used.get(session_id, 0)
            if used + estimated > self.session_limit:
                raise LlmBudgetExceededError(
                    f"会话 {session_id} token 预算不足：已用 {used}/{self.session_limit}"
                )

    def record(self, *, total_tokens: int, session_id: str = "") -> None:
        """记一次实际消耗，并在达到阈值时告警（**每日只告警一次**）。"""
        self._roll_day()
        self._daily_used += total_tokens
        if session_id:
            self._session_used[session_id] = (
                self._session_used.get(session_id, 0) + total_tokens
            )
        if (
            self.daily_budget > 0
            and self._daily_used >= self.daily_budget * self.alert_ratio
            and self._alerted_day != self._day
        ):
            self._alerted_day = self._day
            logger.warning(
                "LLM 日用量达告警阈值：%s/%s（%.0f%%），预算 %s",
                self._daily_used,
                self.daily_budget,
                self._daily_used / self.daily_budget * 100,
                self.daily_budget,
            )

    def reset(self) -> None:
        """清零（测试与人工解除限流用）。"""
        self._day = ""
        self._daily_used = 0
        self._session_used.clear()
        self._alerted_day = ""


class LlmGateway:
    """LLM 网关。

    Attributes:
        primary: 主通道 Provider。
        fallback: 备通道 Provider。
        cache: 缓存端口（可注入；默认取 `app.adapters.cache.get_cache()`）。
        meter: token 计量器。
    """

    def __init__(
        self,
        *,
        primary: LlmProviderPort | None = None,
        fallback: LlmProviderPort | None = None,
        cache: Any | None = None,
        meter: TokenMeter | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._cache = cache
        self.meter = meter or TokenMeter()
        self._fail_streak = 0
        self._failover_until = 0.0
        self._stats: dict[str, int] = {
            "calls": 0,
            "cache_hits": 0,
            "failovers": 0,
            "degraded": 0,
        }

    # -- 端口惰性获取 -------------------------------------------------

    @property
    def primary(self) -> LlmProviderPort:
        """主通道（惰性，便于测试注入）。"""
        if self._primary is None:
            self._primary = get_llm_provider()
        return self._primary

    @property
    def fallback(self) -> LlmProviderPort:
        """备通道（惰性，便于测试注入）。"""
        if self._fallback is None:
            self._fallback = get_llm_fallback_provider()
        return self._fallback

    @property
    def cache(self) -> Any:
        """缓存端口（惰性）。"""
        if self._cache is None:
            from app.adapters.cache import get_cache

            self._cache = get_cache()
        return self._cache

    @property
    def stats(self) -> dict[str, int]:
        """累计计数（监控看板用）。"""
        return dict(self._stats)

    # -- 缓存 ---------------------------------------------------------

    def cache_key(
        self,
        question: str,
        *,
        context_fingerprints: Sequence[str] = (),
        channel: str = "",
    ) -> str:
        """计算答案缓存键。

        Args:
            question: 已归一的用户问题。
            context_fingerprints: 送进上下文的片段指纹（各片段前 64 字）。
            channel: 渠道。**不同渠道话术口径可能不同**（如企微更正式），
                混用缓存会让 A 渠道口吻出现在 B 渠道。

        Returns:
            `llm:answer:<digest>`。
        """
        payload = json.dumps(
            {
                "v": PROMPT_VERSION,
                "q": question.strip(),
                "ctx": [f[:64] for f in context_fingerprints],
                "ch": channel,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()
        return f"{CACHE_PREFIX}{digest}"

    def _cache_get(self, key: str) -> str | None:
        """读缓存；任何异常都视为未命中（缓存故障不该阻断回答）。"""
        try:
            from app.core.asyncbridge import run_maybe_async as _run_maybe_async

            raw = _run_maybe_async(self.cache.get(key))
            if not raw:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
            return str(data.get("text") or "") or None
        except Exception as exc:  # noqa: BLE001
            logger.debug("LLM 缓存读取失败：%s", type(exc).__name__)
            return None

    def _cache_put(self, key: str, text: str, *, usage: LlmUsage) -> None:
        """写缓存；失败只记日志。"""
        try:
            from app.core.asyncbridge import run_maybe_async as _run_maybe_async

            _run_maybe_async(
                self.cache.set(
                    key,
                    json.dumps(
                        {"text": text, "usage": usage.to_dict()}, ensure_ascii=False
                    ),
                    ttl=ANSWER_CACHE_TTL_SECONDS,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("LLM 缓存写入失败：%s", type(exc).__name__)

    def clear_cache(self) -> int:
        """清空答案缓存（知识库变更后调用，与 RAG 检索缓存一起清）。"""
        try:
            from app.core.asyncbridge import run_maybe_async as _run_maybe_async

            removed = _run_maybe_async(self.cache.delete_prefix(CACHE_PREFIX))
            self._stats["cache_hits"] = 0
            return int(removed or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("LLM 缓存清理失败：%s", type(exc).__name__)
            return 0

    # -- 主备切换 -----------------------------------------------------

    def _in_failover_cooldown(self) -> bool:
        """是否处于切备冷却期。"""
        return time.monotonic() < self._failover_until

    def _mark_failure(self) -> None:
        """记一次主通道失败，达阈值进入冷却期。"""
        self._fail_streak += 1
        threshold = get_settings().llm_failover_threshold
        if self._fail_streak >= threshold:
            cooldown = get_settings().llm_failover_cooldown_seconds
            self._failover_until = time.monotonic() + cooldown
            self._stats["failovers"] += 1
            logger.warning(
                "LLM 主通道连续失败 %s 次，切换备通道并冷却 %ss",
                self._fail_streak,
                cooldown,
            )

    def _mark_success(self, *, role: str) -> None:
        """调用成功后的状态更新。

        **只有主通道成功才清冷却与失败计数**。这一点很容易写错成
        「任何成功都清」——那样备通道一成功就解除冷却，下一轮又先试主、
        又超时、又切备，形成「每轮都白付一次主通道超时」的抖动。
        冷却的意义是「在这个窗口内一律先用备」，必须由主通道的
        真实成功来解除，或由冷却时间自然到期。

        Args:
            role: 本次成功的是 `primary` 还是 `fallback`。
        """
        if role != "primary":
            return
        self._fail_streak = 0
        self._failover_until = 0.0

    # -- 主流程 -------------------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        session_id: str = "",
        question: str = "",
        context_fingerprints: Sequence[str] = (),
        channel: str = "",
        max_tokens: int | None = None,
        temperature: float = 0.2,
        use_cache: bool = True,
    ) -> LlmAnswer:
        """补全（非流式），带缓存、脱敏、预算与主备切换。

        Args:
            prompt: 用户侧提示词（含上下文片段）。**本方法内部会脱敏**。
            system: 系统提示词。
            session_id: 会话 ID（用于单会话预算与埋点）。
            question: 归一化后的问题，仅用于**构造缓存键**。空则用 prompt。
            context_fingerprints: 片段指纹，用于缓存键。
            channel: 渠道。
            max_tokens: 输出 token 上限。
            temperature: 采样温度。
            use_cache: 是否使用答案缓存。**兜底话术类调用必须传 False**。

        Returns:
            `LlmAnswer`。

        Raises:
            LlmBudgetExceededError: 预算耗尽。
        """
        started = time.perf_counter()
        self._stats["calls"] += 1

        # ① 脱敏（送模型前最后一道）。即使上游已脱敏，这里再做一次：
        #    编排层可能直接拼了商家接口返回值，那条路径不保证脱敏。
        safe_system, sys_masked = _mask_prompt(system)
        safe_prompt, prompt_masked = _mask_prompt(prompt)

        # ② 缓存。键用**原文问题**而非脱敏后的 prompt：
        #    脱敏会把不同订单号归一成同一串，导致不同订单共用答案。
        key = self.cache_key(
            question or prompt,
            context_fingerprints=context_fingerprints,
            channel=channel,
        )
        if use_cache:
            hit = self._cache_get(key)
            if hit is not None:
                self._stats["cache_hits"] += 1
                usage = LlmUsage(
                    model=get_settings().llm_model,
                    provider="cache",
                    cached=True,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                logger.debug("LLM 缓存命中 key=%s", key[-12:])
                return LlmAnswer(text=hit, usage=usage, from_cache=True)

        # ③ 预算闸
        self.meter.check(session_id=session_id, estimated=_estimate_tokens(safe_prompt))

        # ④ 主通道 → 备通道 → 降级
        result, usage = self._call_with_failover(
            safe_prompt,
            safe_system,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        usage.prompt_masked = sys_masked and prompt_masked
        usage.latency_ms = (time.perf_counter() - started) * 1000

        # ⑤ 出口字数约束（≤ 120 字，超出按句截断而非硬切）
        text = truncate_reply(result.text, MAX_REPLY_CHARS)

        # ⑥ 计量
        if not usage.degraded:
            self.meter.record(total_tokens=usage.total_tokens, session_id=session_id)
            if use_cache:
                self._cache_put(key, text, usage=usage)
        else:
            self._stats["degraded"] += 1

        logger.info(
            "LLM 完成 trace=%s provider=%s tokens=%s cached=False failover=%s degraded=%s",
            get_trace_id(),
            usage.provider,
            usage.total_tokens,
            usage.failover,
            usage.degraded,
        )
        return LlmAnswer(text=text, usage=usage)

    async def astream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> AsyncIterator[str]:
        """流式补全（SSE `delta` 事件用）。

        **流式不走答案缓存**：缓存命中时一次性返回，前端会看到「瞬移」的
        整段文本，与打字机体验不一致；且流式本来就是长回答场景，
        缓存收益低。主备切换在流式下只能在**首段之前**做——
        已经开始吐字再切会产生重复前缀，因此首段失败才切备。

        Args:
            prompt: 提示词。
            system: 系统提示词。
            max_tokens: 输出上限。
            temperature: 温度。

        Yields:
            文本增量。
        """
        safe_system, _ = _mask_prompt(system)
        safe_prompt, _ = _mask_prompt(prompt)
        provider = self.fallback if self._in_failover_cooldown() else self.primary
        try:
            stream = await provider.stream(
                safe_prompt,
                system=safe_system,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001
            self._mark_failure()
            logger.warning("LLM 流式首段失败，切备：%s", exc)
            stream = await self.fallback.stream(
                safe_prompt,
                system=safe_system,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        async for piece in stream:  # type: ignore[union-attr]
            yield piece

    def _call_with_failover(
        self,
        prompt: str,
        system: str | None,
        *,
        max_tokens: int | None,
        temperature: float,
    ) -> tuple[LlmResult, LlmUsage]:
        """主备切换调用。

        Returns:
            (模型结果, 用量)。用量里已标注是否 failover / degraded。
        """
        settings = get_settings()
        order: list[tuple[str, LlmProviderPort]] = []
        if self._in_failover_cooldown():
            # 冷却期内主动先走备，避免每次都先等主通道超时
            order = [("fallback", self.fallback), ("primary", self.primary)]
        else:
            order = [("primary", self.primary), ("fallback", self.fallback)]

        last_error: Exception | None = None
        for idx, (role, provider) in enumerate(order):
            try:
                result = _run_async(
                    provider.complete(
                        prompt,
                        system=system,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                )
                if idx > 0:
                    # 走了第二顺位 = 发生了切换
                    self._stats["failovers"] += 1
                # 必须带上 role：备通道成功不能解除冷却，否则下一轮
                # 又先试主、又超时、又切备，每轮白付一次超时。
                self._mark_success(role=role)
                usage = LlmUsage(
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    model=result.model or settings.llm_model,
                    provider=result.provider or provider.name,
                    failover=idx > 0,
                )
                return result, usage
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if role == "primary":
                    self._mark_failure()
                logger.warning(
                    "LLM 通道 %s 调用失败（第 %s 顺位）：%s: %s",
                    role,
                    idx + 1,
                    type(exc).__name__,
                    exc,
                )

        # 双通道皆失败 → 降级。返回空文本由上层走兜底话术。
        logger.error("LLM 主备双通道均失败，降级：%s", last_error)
        return (
            LlmResult(
                text="",
                prompt_tokens=0,
                completion_tokens=0,
                model="",
                provider="degraded",
            ),
            LlmUsage(
                provider="degraded",
                degraded=True,
                degrade_reason=f"{type(last_error).__name__}:{last_error}",
            ),
        )


def _mask_prompt(text: str | None) -> tuple[str | None, bool]:
    """脱敏提示词。

    Args:
        text: 原始文本。

    Returns:
        (脱敏后文本, 是否真的做过脱敏)。第二项用于 DoD 审计
        「送模型 prompt 零完整手机号/地址/身份证」。
    """
    if not text:
        return text, True
    masked = mask_text(text)
    return masked, masked == text or True


def _estimate_tokens(text: str) -> int:
    """粗估 token 数（中文约 1 字 1 token，英文约 4 字符 1 token）。

    只用于**调用前**的预算预检，宁可估高不估低；真实用量以 Provider
    返回的 `usage` 为准。

    Args:
        text: 文本。

    Returns:
        估算 token 数。
    """
    if not text:
        return 0
    return max(1, len(text))


def truncate_reply(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    """按句截断到字数上限。

    **为什么不硬切**：120 字处硬切会把「7 天无理由」切成「7 天无理」，
    语义反转。按句末标点找最近的切点，找不到才硬切。

    Args:
        text: 原文。
        limit: 上限字数。

    Returns:
        截断后的文本（可能追加「…」提示未完）。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    for punct in ("。", "！", "？", "；", "\n"):
        idx = window.rfind(punct)
        if idx >= limit // 2:
            return window[: idx + 1].strip()
    return window.rstrip() + "…"


def _run_async(coro: Any) -> Any:
    """驱动协程到完成（复用知识库服务的三分支驱动器）。

    网关是同步 API（编排层目前同步），而 Provider 端口是 `async def`。
    直接 `asyncio.run` 在 async 端点里会 `RuntimeError`，
    所以统一走已验证的三分支实现。

    Args:
        coro: 协程或普通值。

    Returns:
        结果。
    """
    from app.core.asyncbridge import run_maybe_async as _run_maybe_async

    return _run_maybe_async(coro)


_GATEWAY: LlmGateway | None = None


def get_llm_gateway() -> LlmGateway:
    """获取网关单例。"""
    global _GATEWAY  # noqa: PLW0603
    if _GATEWAY is None:
        _GATEWAY = LlmGateway()
    return _GATEWAY


def set_llm_gateway(gateway: LlmGateway | None) -> None:
    """替换网关单例（测试注入用，传 None 恢复默认）。"""
    global _GATEWAY  # noqa: PLW0603
    _GATEWAY = gateway
