"""BE-03 会话与多轮状态管理。

设计意图
--------
本模块只干三件事，其余全部推给编排层：

1. **状态存取**：按 `session_id` 读写会话状态，Redis 为热存储、DB 为冷备份。
2. **指代消解**：把「它 / 那款 / 刚才说的」这类无主指代替换成具体实体。
3. **槽位继承**：把上一轮的 SKU / 尺码 / 订单号继承到本轮。

**为什么状态里绝对不能出现完整隐私信息**
------------------------------------------------
HC-2 要求「最小必要、可删除可追溯」，而会话状态是最容易被忽视的泄漏点：
它会被 `repr` 进日志、被 `json.dumps` 进 Redis、被快照进 `session_states`
表。只要有一处带上了完整手机号，就等于把用户隐私复制到了一个生命周期
比订单本身长得多的地方（TTL 30 分钟 × N 次重连）。

所以 `SessionState` 的全部写入路径都过 `mask_value`（见 `app.core.logging`），
并且有 `assert_no_pii` 做入库前兜底自检——**宁可抛异常也不能静默落库**。

**为什么用 Redis 而不是只放 DB**
------------------------------------------------
每轮对话要读一次、写一次。走 DB 是 2 次往返 + 行锁，走 Redis 是 2 次内存
读。30 分钟 TTL 也让「会话结束即消失」成为默认行为，不需要清理任务。
DB 只在会话状态发生**结构性变化**（新增实体、转人工）时落一份快照，
用于 Redis 故障后的恢复与事后审计。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.enums import Channel, Emotion, IntentCategory
from app.core.logging import get_logger, mask_value
from app.core.trace import get_trace_id, new_session_id

logger = get_logger(__name__)

#: 会话状态在 Redis 中的 key 前缀
SESSION_KEY_PREFIX = "wk:session:"

#: 强制引导转人工的轮次阈值（BE-03 DoD：turn_count > 10 强制转人工）
FORCE_HANDOFF_TURN_THRESHOLD = 10

#: 连续负面情绪达到此值即标记（AI-04 DoD：连续 3 轮负面触发标记）
NEGATIVE_STREAK_THRESHOLD = 3


# ---------------------------------------------------------------------------
# 指代识别
# ---------------------------------------------------------------------------

#: 无主指代词。命中且当前轮没有新实体时，需要从历史里找回指代对象。
#: **刻意不含「这/那」单字**：口语里「这个怎么洗」的「这个」多数场景
#: 指的就是上一句提到的商品，但「这么贵」「那样吧」不是指代，
#: 单字召回精度太低会把大量非指代句子误判成指代。
_PRONOUN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"那[款个只件套]"),
    re.compile(r"这[款个只件套](?![么样])"),
    re.compile(r"它(?:家|们)?(?!们)"),
    re.compile(r"刚才(?:说|提|问|讲)的"),
    re.compile(r"之前(?:说|提|问|讲)的"),
    re.compile(r"上面(?:说|提)的"),
    re.compile(r"前面(?:说|提)的"),
    re.compile(r"同款|另一个|另一只|其他款"),
    re.compile(r"贵的?那[个款]|便宜那[个款]|大的?那[个款]|小的?那[个款]"),
)

#: 指代词里天然带「比较」语义的，解消时要按尺寸/价格挑而不是取最后一个
_COMPARATIVE_HINTS: dict[str, str] = {
    "大的": "max_size",
    "贵的那": "max_price",
    "便宜那": "min_price",
    "小的": "min_size",
}

#: SKU 编码（商家主数据形如 `WK-BEAR-20`）
_SKU_RE = re.compile(r"WK-[A-Z]+-\d+", re.IGNORECASE)

#: 尺码（20cm / 20 厘米 / 四十厘米）
_SIZE_RE = re.compile(r"(\d{2,3})\s*(?:cm|厘米|CM)", re.IGNORECASE)

#: 订单号（脱敏后形如 2026****0001，也可能是完整 12 位）
_ORDER_RE = re.compile(r"\b(\d{4}\*{4}\d{4}|\d{12})\b")

#: 运单号
_TRACKING_RE = re.compile(r"\b(\d{4}\*{4}\d{4}|\d{10,18})\b")


def detect_pronoun(text: str) -> str | None:
    """检测文本里是否含无主指代。

    Args:
        text: 用户本轮原文。

    Returns:
        命中的指代词；无指代返回 `None`。
    """
    for pattern in _PRONOUN_PATTERNS:
        m = pattern.search(text or "")
        if m:
            return m.group(0)
    return None


def is_comparative(text: str) -> str | None:
    """判断指代的方向（要大的还是要便宜的）。

    Args:
        text: 用户本轮原文。

    Returns:
        `max_size` / `min_size` / `max_price` / `min_price` / `None`。
    """
    for hint, direction in _COMPARATIVE_HINTS.items():
        if hint in (text or ""):
            return direction
    return None


# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    """会话状态。字段**严格对齐任务书 2.2.2 / 项目文档 5.2 节**。

    Attributes:
        session_id: 会话 ID（商家回调的 session_id，不自造）。
        channel: 渠道。
        intent_stack: 意图栈。多轮里用户会先问 A 再问 B，栈顶为当前意图，
            栈底保留最早意图——用于「还有刚才那个问题」时回退。
        slots: 槽位（sku_id / size / color / order_no 等）。
        entities: 实体（按类型归组的原文，如 `{"sku": [...], "size": [...]}`）。
        turn_count: 轮次计数（用户每发一条 +1）。
        unresolved_count: 连续未解决计数（兜底答案或检索无依据 +1）。
        emotion: 当前情绪。
        negative_streak: 连续负面轮数（AI-04 用）。
        buyer_bound: 是否已绑定买家身份（BE-08 用）。
        bound_order_no: 已绑定的订单号（脱敏后）。
        last_query: 上一轮归一化后的查询（指代消解的素材）。
        last_skus: 上一轮提到的 SKU 列表（按出现顺序）。
        handoff: 是否已转人工。
        created_at / updated_at: 时间戳。
    """

    session_id: str
    channel: str = Channel.TAOBAO.value
    intent_stack: list[str] = field(default_factory=list)
    slots: dict[str, Any] = field(default_factory=dict)
    entities: dict[str, list[str]] = field(default_factory=dict)
    turn_count: int = 0
    unresolved_count: int = 0
    emotion: str = Emotion.NEUTRAL.value
    negative_streak: int = 0
    buyer_bound: bool = False
    bound_order_no: str | None = None
    last_query: str = ""
    last_skus: list[str] = field(default_factory=list)
    last_sizes: list[str] = field(default_factory=list)
    handoff: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------
    # 意图栈
    # ------------------------------------------------------------------

    @property
    def current_intent(self) -> str | None:
        """栈顶意图（当前在聊什么）。"""
        return self.intent_stack[-1] if self.intent_stack else None

    def push_intent(self, intent: str) -> None:
        """压入意图。

        同一意图不重复压栈（连续三轮都在问售后不该让栈长到 3），
        否则「回退到上一个话题」就永远不会落在正确的位置。
        """
        if not intent:
            return
        if self.intent_stack and self.intent_stack[-1] == intent:
            return
        self.intent_stack.append(intent)

    def pop_intent(self) -> str | None:
        """弹出栈顶（切回上一个话题用）。"""
        return self.intent_stack.pop() if self.intent_stack else None

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 化的字典（**写入前统一过脱敏**）。"""
        raw = {
            "session_id": self.session_id,
            "channel": self.channel,
            "intent_stack": list(self.intent_stack),
            "slots": dict(self.slots),
            "entities": {k: list(v) for k, v in self.entities.items()},
            "turn_count": self.turn_count,
            "unresolved_count": self.unresolved_count,
            "emotion": self.emotion,
            "negative_streak": self.negative_streak,
            "buyer_bound": self.buyer_bound,
            "bound_order_no": self.bound_order_no,
            "last_query": self.last_query,
            "last_skus": list(self.last_skus),
            "last_sizes": list(self.last_sizes),
            "handoff": self.handoff,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return mask_value(raw)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        """从字典还原。容忍缺失字段（跨版本兼容）。"""
        return cls(
            session_id=data.get("session_id") or new_session_id(),
            channel=data.get("channel") or Channel.TAOBAO.value,
            intent_stack=list(data.get("intent_stack") or []),
            slots=dict(data.get("slots") or {}),
            entities={
                k: list(v) for k, v in (data.get("entities") or {}).items()
            },
            turn_count=int(data.get("turn_count") or 0),
            unresolved_count=int(data.get("unresolved_count") or 0),
            emotion=data.get("emotion") or Emotion.NEUTRAL.value,
            negative_streak=int(data.get("negative_streak") or 0),
            buyer_bound=bool(data.get("buyer_bound")),
            bound_order_no=data.get("bound_order_no"),
            last_query=data.get("last_query") or "",
            last_skus=list(data.get("last_skus") or []),
            last_sizes=list(data.get("last_sizes") or []),
            handoff=bool(data.get("handoff")),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )

    def to_json(self) -> str:
        """序列化为 JSON 字符串（写 Redis 用）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | bytes) -> SessionState:
        """从 JSON 还原。"""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return cls.from_dict(json.loads(raw))


# ---------------------------------------------------------------------------
# PII 兜底自检
# ---------------------------------------------------------------------------

#: 不允许出现在会话状态里的完整隐私模式。
#: 注意这里只查**完整**形态——脱敏后的 `138****8888` 是允许的。
#:
#: 地址的判定口径要特别小心：`mask_address` 的设计是**保留省市区**、
#: 只脱敏街道门牌（坐席要靠省市判断「江浙沪次日达」这类时效）。
#: 因此「浙江省杭州市西湖区」是合规内容，不能算泄漏；
#: 真正构成泄漏的是省市区**之后**的具体门牌（楼栋门牌号可定位到住户）。
#: 第一版正则写成「省+市+区+2 个字符」，把 `浙江省杭州市西湖区****`
#: 也判成泄漏，结果是脱敏做到了、自检却仍然拦——业务被堵死。
_FORBIDDEN_PII: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("完整手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("完整身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("完整银行卡", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("完整邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    # 地址：省市区之后若还跟着**门牌级**信息（路/街/号/幢/室等）才是泄漏。
    # 已脱敏的 `****`、`[门牌已屏蔽]` 不含这些字，自然放行。
    (
        "完整地址",
        re.compile(
            r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼]"
            r"[\u4e00-\u9fa5]{1,6}(?:省|市)?[\u4e00-\u9fa5]{0,8}(?:市|区|县|旗)"
            r"[\u4e00-\u9fa5A-Za-z0-9]{0,12}"
            r"(?:路|街|道|巷|弄|号|幢|栋|单元|室|楼|层|户)"
        ),
    ),
    # 门牌残段：省市区已被 `****` 打断时，上面那条连不起来，
    # 需要单独认「数字+门牌单位」的整串（`100 号 3 幢 502 室`）。
    # 这条**不看前缀**，因此对「任意位置出现的门牌」都成立——
    # 门牌本身就是可定位到住户的信息，不该出现在会话状态里。
    (
        "门牌残段",
        re.compile(r"\d+\s*(?:号|幢|栋|单元|室|楼|层|户|弄|巷|座)"),
    ),
)


def assert_no_pii(payload: Any, *, where: str = "session_state") -> None:
    """入库/入缓存前的隐私兜底自检。

    **宁可抛异常也不能静默落库**：一旦完整手机号进了 Redis 或 DB，
    想事后清理就得同时改缓存、改快照表、改日志，成本远高于一次报错。

    Args:
        payload: 待检查对象（字典/字符串，会自动 JSON 化）。
        where: 出错时报告的写入位置。

    Raises:
        ValueError: 命中任一完整隐私模式。
    """
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, default=str
    )
    for label, pattern in _FORBIDDEN_PII:
        m = pattern.search(text)
        if m:
            raise ValueError(
                f"会话状态中检测到{label}（未脱敏），拒绝写入 {where}："
                f"命中片段 {m.group(0)[:4]}***"
            )


# ---------------------------------------------------------------------------
# 状态仓库
# ---------------------------------------------------------------------------


class SessionStore:
    """会话状态仓库。Redis 热存 + DB 冷备。

    Attributes:
        cache: 缓存端口（Redis 或内存实现）。
        ttl_seconds: Redis TTL（默认 1800 秒 = 30 分钟）。
    """

    def __init__(self, cache: Any = None, *, ttl_seconds: int | None = None) -> None:
        self._cache = cache
        settings = get_settings()
        self.ttl_seconds = ttl_seconds or settings.session_ttl_seconds

    @property
    def cache(self) -> Any:
        """惰性获取缓存（便于测试注入）。"""
        if self._cache is None:
            from app.adapters.cache import get_cache

            self._cache = get_cache()
        return self._cache

    def _key(self, session_id: str) -> str:
        return f"{SESSION_KEY_PREFIX}{session_id}"

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def load(self, session_id: str, *, channel: str | None = None) -> SessionState:
        """读取会话状态；不存在则新建。

        Args:
            session_id: 会话 ID。
            channel: 新建时使用的渠道。

        Returns:
            会话状态。
        """
        from app.core.asyncbridge import run_maybe_async as _run_maybe_async

        raw = _run_maybe_async(self.cache.get(self._key(session_id)))
        if raw:
            try:
                state = SessionState.from_json(raw)
                # TTL 续期：用户还在聊就说明会话还活着
                self.save(state, refresh_ttl=True)
                return state
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "会话状态解析失败，按新会话处理 session_id=%s err=%s",
                    session_id,
                    exc,
                )
        return SessionState(
            session_id=session_id, channel=channel or Channel.TAOBAO.value
        )

    def save(self, state: SessionState, *, refresh_ttl: bool = False) -> None:
        """写入会话状态。

        Args:
            state: 会话状态。
            refresh_ttl: 是否重置 TTL（读取时续期用）。

        Raises:
            ValueError: 状态里检出完整隐私信息。
        """
        from app.core.asyncbridge import run_maybe_async as _run_maybe_async

        state.updated_at = time.time()
        payload = state.to_json()
        # 写缓存前兜底：任何路径漏了脱敏，这里最后拦一次
        assert_no_pii(payload, where=f"redis:{self._key(state.session_id)}")
        _run_maybe_async(
            self.cache.set(
                self._key(state.session_id), payload, ttl=self.ttl_seconds
            )
        )

    def delete(self, session_id: str) -> bool:
        """删除会话状态（会话结束清理，HC-2 可删除要求）。"""
        from app.core.asyncbridge import run_maybe_async as _run_maybe_async

        return bool(
            _run_maybe_async(self.cache.delete(self._key(session_id)))
        )

    def ttl(self, session_id: str) -> int:
        """剩余存活秒数（测试与监控用）。"""
        from app.core.asyncbridge import run_maybe_async as _run_maybe_async

        fn = getattr(self.cache, "ttl", None)
        if fn is None:
            return -1
        return int(_run_maybe_async(fn(self._key(session_id))) or -1)


def get_session_store() -> SessionStore:
    """全局会话仓库单例。"""
    global _STORE
    if _STORE is None:
        _STORE = SessionStore()
    return _STORE


def set_session_store(store: SessionStore | None) -> None:
    """替换单例（测试用）。"""
    global _STORE
    _STORE = store


_STORE: SessionStore | None = None


# ---------------------------------------------------------------------------
# 指代消解与槽位继承
# ---------------------------------------------------------------------------


@dataclass
class ResolveResult:
    """指代消解结果。

    Attributes:
        query: 消解后的查询（可能等于原文）。
        resolved: 是否发生了消解。
        method: 消解方式（`none` / `new_entity` / `last_sku` /
            `comparative` / `last_query`）。
        confidence: 置信度（0-1）。低于阈值应当反问而不是猜。
        note: 可读的说明，直接进日志与坐席上下文卡片。
    """

    query: str
    resolved: bool = False
    method: str = "none"
    confidence: float = 1.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """导出为字典。"""
        return {
            "query": self.query,
            "resolved": self.resolved,
            "method": self.method,
            "confidence": self.confidence,
            "note": self.note,
        }


def extract_entities(text: str) -> dict[str, list[str]]:
    """从文本里抽取实体。

    抽取顺序刻意是「SKU → 尺码 → 订单号 → 运单号」，且抽取后
    **不删除**原文，因为消解需要保留用户原本的表述方式。

    Args:
        text: 用户原文。

    Returns:
        按类型归组的实体列表（去重、保序）。
    """
    out: dict[str, list[str]] = {}
    text = text or ""

    skus = [m.upper() for m in _SKU_RE.findall(text)]
    if skus:
        out["sku"] = _dedup(skus)

    sizes = [f"{m}cm" for m in _SIZE_RE.findall(text)]
    if sizes:
        out["size"] = _dedup(sizes)

    orders = _dedup(_ORDER_RE.findall(text))
    if orders:
        out["order_no"] = orders

    trackings = [
        t for t in _dedup(_TRACKING_RE.findall(text)) if t not in orders
    ]
    if trackings:
        out["tracking_no"] = trackings

    return out


def _dedup(items: list[str]) -> list[str]:
    """去重保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def resolve_reference(
    text: str,
    state: SessionState,
    entities: dict[str, list[str]] | None = None,
) -> ResolveResult:
    """指代消解。

    处理三类情况（对应 BE-03 DoD 的 ≥50 条测试集）：

    1. **本轮自带实体** → 无需消解，但要把实体记进状态供下一轮用。
    2. **本轮有指代、上一轮提过 SKU** → 把指代替换成具体 SKU。
       若指代带比较语义（「大的那个」「便宜那款」），按尺寸/价格挑。
    3. **本轮有指代但没有可继承的 SKU** → 退化为「拼上一轮查询」，
       且**置信度降到 0.5**，让编排层反问而不是硬猜。

    Args:
        text: 用户本轮原文。
        state: 会话状态（会被读取，不会被修改）。
        entities: 本轮已抽取的实体；为空则现抽。

    Returns:
        `ResolveResult`。
    """
    original = (text or "").strip()
    if not original:
        return ResolveResult(query=original, note="空输入")

    ents = entities if entities is not None else extract_entities(original)
    pronoun = detect_pronoun(original)

    # 情况 1：本轮自带 SKU，直接算新实体，不需要消解
    if ents.get("sku"):
        return ResolveResult(
            query=original,
            resolved=False,
            method="new_entity",
            confidence=1.0,
            note=f"本轮自带 SKU {ents['sku']}",
        )

    # 情况 2：有指代 + 历史有 SKU
    if pronoun and state.last_skus:
        direction = is_comparative(original)
        target = _pick_sku(state, direction)
        if target:
            return ResolveResult(
                query=_substitute_reference(original, pronoun, target),
                resolved=True,
                method="comparative" if direction else "last_sku",
                confidence=0.9 if direction else 0.85,
                note=f"指代「{pronoun}」→ {target}"
                + (f"（{direction}）" if direction else ""),
            )

    # 情况 3：有指代但无可继承实体 → 拼上一轮查询，置信度压低
    if pronoun:
        if state.last_query:
            return ResolveResult(
                query=f"{state.last_query} {original}".strip(),
                resolved=True,
                method="last_query",
                confidence=0.5,
                note=f"指代「{pronoun}」无具体实体，拼接上一轮查询（低置信）",
            )
        return ResolveResult(
            query=original,
            resolved=False,
            method="none",
            confidence=0.3,
            note=f"指代「{pronoun}」但无任何历史，需反问",
        )

    # 无指代：若本轮只给了尺码（「30cm 的多少钱」），补上历史 SKU 前缀
    if ents.get("size") and state.last_skus:
        sku_hint = state.last_skus[0]
        return ResolveResult(
            query=f"{sku_hint} {original}",
            resolved=True,
            method="size_inherit",
            confidence=0.8,
            note=f"尺码 {ents['size']} 继承历史 SKU {sku_hint}",
        )

    return ResolveResult(query=original, resolved=False, note="无指代，无需消解")


def _pick_sku(state: SessionState, direction: str | None) -> str | None:
    """按方向从历史 SKU 里挑一个。

    Args:
        state: 会话状态。
        direction: 比较方向。

    Returns:
        选中的 SKU；无法判定返回最后一个提到的。
    """
    if not state.last_skus:
        return None
    if not direction:
        return state.last_skus[-1]

    def size_of(sku: str) -> int:
        m = re.search(r"-(\d+)$", sku)
        return int(m.group(1)) if m else 0

    if direction == "max_size":
        return max(state.last_skus, key=size_of)
    if direction == "min_size":
        return min(state.last_skus, key=size_of)
    # 价格方向需要查主数据，这里不查库——降级为按尺寸近似（同行同款价格随尺寸升）
    if direction == "max_price":
        return max(state.last_skus, key=size_of)
    if direction == "min_price":
        return min(state.last_skus, key=size_of)
    return state.last_skus[-1]


def _substitute_reference(text: str, pronoun: str, target: str) -> str:
    """把指代词替换成具体实体。

    **保留原文其余部分**：用户可能说「那个大的能机洗吗」，
    替换后应是「WK-BEAR-40 能机洗吗」，而不是只返回 SKU。

    Args:
        text: 原文。
        pronoun: 指代词。
        target: 具体实体。

    Returns:
        替换后的文本。
    """
    idx = text.find(pronoun)
    if idx < 0:
        return f"{target} {text}".strip()
    return f"{text[:idx]}{target}{text[idx + len(pronoun):]}".strip()


def inherit_slots(
    state: SessionState, entities: dict[str, list[str]]
) -> dict[str, Any]:
    """槽位继承：把本轮实体并进槽位，保留上轮未覆盖的槽位。

    规则：**本轮有的覆盖，本轮没有的继承**。这样「30cm 的」之后再说
    「要红色的」，尺码不会丢。

    Args:
        state: 会话状态（不修改）。
        entities: 本轮实体。

    Returns:
        继承后的槽位字典。
    """
    slots = dict(state.slots)
    if entities.get("sku"):
        slots["sku_id"] = entities["sku"][-1]
        slots["sku_ids"] = list(entities["sku"])
    if entities.get("size"):
        slots["size"] = entities["size"][-1]
    if entities.get("order_no"):
        slots["order_no"] = entities["order_no"][-1]
    if entities.get("tracking_no"):
        slots["tracking_no"] = entities["tracking_no"][-1]
    return slots


def update_state_after_turn(
    state: SessionState,
    *,
    text: str,
    resolved_query: str,
    entities: dict[str, list[str]],
    intent: str | None = None,
    resolved_unresolved: bool = False,
) -> SessionState:
    """一轮对话结束后更新状态。

    Args:
        state: 会话状态（原地修改）。
        text: 用户本轮原文。
        resolved_query: 消解后的查询（记入 `last_query`）。
        entities: 本轮实体。
        intent: 本轮意图。
        resolved_unresolved: 本轮是否解决了一个之前未解决的问题。

    Returns:
        更新后的状态。
    """
    state.turn_count += 1
    if intent:
        state.push_intent(intent)

    state.slots = inherit_slots(state, entities)
    for key, values in entities.items():
        bucket = state.entities.setdefault(key, [])
        for v in values:
            if v not in bucket:
                bucket.append(v)

    if entities.get("sku"):
        state.last_skus = list(entities["sku"])
    if entities.get("size"):
        state.last_sizes = list(entities["size"])
    state.last_query = resolved_query
    if resolved_unresolved and state.unresolved_count > 0:
        state.unresolved_count = 0
    return state


def should_force_handoff(state: SessionState) -> bool:
    """轮次超限是否强制转人工（BE-03 DoD：turn_count > 10）。

    Args:
        state: 会话状态。

    Returns:
        是否强制转人工。
    """
    return state.turn_count > FORCE_HANDOFF_TURN_THRESHOLD


def is_negative(emotion: str) -> bool:
    """是否负面情绪。"""
    return emotion in (Emotion.DISSATISFIED.value, Emotion.ANGRY.value)
