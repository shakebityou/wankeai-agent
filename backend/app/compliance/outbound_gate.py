"""出站合规闸（HC-1 外链零输出 / BE-17 / AI-03 合规闸）。

**本模块是所有出站文本的唯一出口。** 任何要发送给用户或写回渠道的文本，
必须先经 `OutboundGate.enforce()`；未经本闸的文本不得出站。

三级判定：

| 级别 | 命中内容 | 处理 |
| --- | --- | --- |
| `block` | 外链（`http(s)://`、裸域名、平台域名）、唤起协议、跳转引导话术、身份证/银行卡/微信 QQ | **整条替换**为 `FALLBACK_REPLY`，`ok=False` |
| `mask` | 手机号 / 姓名 / 地址 / 订单号 / 运单号 / 邮箱 | **就地脱敏**后放行（不拦截） |
| `flag` | 绝对化用语、承诺性表述、商家禁用表述 | 默认**记录并告警**；`strict=True` 时同样替换 |

判定依据：
    * 任务书 HC-1「不生成、不发送任何外链或跳转卡片」，第 1225 行 BE-02 DoD
      「出站内容中不含任何外链（正则扫描命中即测试失败）」；
    * 商家 `接口对接说明书.md` 0.8 节「跳转链接 / 小程序 / H5 唤起 ❌ 不接入」；
    * 任务书第 1412 行 BE-17「合规闸（敏感词/绝对化用语/承诺性表述/外链）」；
    * 任务书第 1408 行 BE-17 脱敏规则 7 类 + 执行位置 ③「Agent 应答引用订单号时」
      与 ⑤「送模型时（不得送含个人信息原文）」。

订单号的会话白名单
------------------
任务书 `2.2.4` 的订单纯文本模板示例中含**完整订单号**
（「您的订单 202501150001 目前状态：已发货」），而 BE-17 执行位置 ③ 要求
应答引用订单号时脱敏为 `2025****0001`。两者张力通过 `enforce(..., allow_order_nos=...)`
化解：传入当前会话**已核验**的订单号，这些号原样保留，其余一律脱敏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 命中外链/跳转后对用户的兜底话术（**纯文字、无链接**，任务书 2.4.2 节口径）
FALLBACK_REPLY = "马上为您转接人工客服～"

# ---------------------------------------------------------------------------
# 敏感信息（硬拦截，一律不提供）
# ---------------------------------------------------------------------------

#: 身份证号（18 位，含 X 结尾）
_ID_CARD_RE = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
#: 银行卡号（16-19 位连续数字）
_BANK_CARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
#: 索要 / 提供身份证、银行卡、验证码、私加联系方式的表述
_SENSITIVE_PHRASE_RE = re.compile(
    r"(?:"
    r"身份证(?:号|号码|正面|反面|照片|复印件)|银行卡(?:号|号码|照片)|"
    r"验证码|短信验证码|支付密码|登录密码|"
    r"(?:加|扫|留)\s*(?:微信|QQ|V信|vx)|微信号是|QQ号是|"
    r"私(?:下|聊)\s*(?:加|联系)"
    r")",
    re.IGNORECASE,
)

#: 姓名（带称谓前缀）。`app.core.logging._CN_NAME_RE` 用「前一个字符不是汉字」
#: 规避误伤，但「收货人张伟已签收」里 `张` 前面正是汉字 `人`，会漏。
#: 出站闸额外补一条**带前缀**的规则，覆盖收货人 / 联系人 / 买家等真实话术。
_CN_NAME_PREFIX_RE = re.compile(
    r"((?:收货人|收件人|联系人|买家|客户|顾客|用户|我叫|姓名为|名字是|签收人)"
    r"\s*[:：]?\s*)([\u4e00-\u9fa5]{2,4})(?=[\s，。,.、；;：:！!？?）)】]|已|在|先生|女士|$)"
)

# ---------------------------------------------------------------------------
# 外链与跳转（硬拦截）
# ---------------------------------------------------------------------------

#: 外链正则：协议、裸 www、以及会出现在客服话术里的平台域名
_LINK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("scheme_http", re.compile(r"https?://", re.IGNORECASE)),
    ("scheme_app", re.compile(r"\b(?:weixin|wechat|alipays?|taobao|tmall|pinduoduo|douyin|sinaweibo|qq)://", re.IGNORECASE)),
    ("bare_www", re.compile(r"(?<![\w@.-])www\.[A-Za-z0-9.-]+", re.IGNORECASE)),
    (
        "bare_domain",
        # 前置负向断言排除 `@`：邮箱 `zhangsan@example.com` 里的 `example.com`
        # 不是外链，必须交给 PII 脱敏而不是被合规闸拦截。
        re.compile(
            r"(?<![\w@.-])[A-Za-z0-9-]+\.(?:com|cn|net|org|top|xyz|shop|vip|io)\b",
            re.IGNORECASE,
        ),
    ),
    ("ip_port", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{2,5})?\b")),
)

#: 跳转引导话术（与 `HandoffResponse._no_link` 的禁用词表保持同一口径）
_REDIRECT_PHRASES: tuple[str, ...] = (
    "我把链接发您",
    "把链接发你",
    "链接发您",
    "点击链接",
    "点这里",
    "点此",
    "戳这里",
    "扫码",
    "扫一扫",
    "二维码",
    "长按识别",
    "小程序",
    "跳转",
    "复制到浏览器",
    "打开浏览器",
    "H5 页面",
    "详情页链接",
)

# ---------------------------------------------------------------------------
# 绝对化用语 / 承诺性表述（默认仅记录）
# ---------------------------------------------------------------------------

#: 绝对化用语（《广告法》第九条口径 + 商家《品牌与IP资料.md》话术约束）
_ABSOLUTE_PHRASES: tuple[str, ...] = (
    "最好的",
    "最好用",
    "最便宜",
    "最低价",
    "全网最低",
    "国家级",
    "世界级",
    "顶级",
    "第一品牌",
    "销量第一",
    "绝对",
    "100%",
    "百分百",
    "永久",
    "永不",
    "包治",
    "特效",
    "独一无二",
    "独家首发",
)

#: 承诺性表述。**注意**：商家《品牌与IP资料.md》5.1 节确有
#: 「假一赔十」「7 天无理由」等**已公示承诺**，这些是合规的对外口径，
#: 因此本表只列**客服不得自行加码**的表述，不列商家已公示条款。
#: 依据商家《营销活动规则.md》第 255 行「客服答复价格类咨询时，须以系统
#: 实际结算价为准，**不得口头承诺折扣或改价**」与第 257 行「不得承诺
#: 未经审批的额外优惠」。
_PROMISE_PHRASES: tuple[str, ...] = (
    "保证赔偿",
    "保证退款",
    "保证赔付",
    "无条件退",
    "包退包换",
    "假一赔万",
    "假一赔百",
    "我承诺",
    "我们承诺赔",
    "一定给您",
    "保证解决",
    "百分之百",
)

#: 承诺性表述的**正则**形式。短语表只能穷举固定说法，正则覆盖「我保证
#: 给您改价」「承诺少收运费」这类插入了主语的变体。
_PROMISE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "promise_repricing",
        re.compile(r"(?:我|我们)?(?:保证|承诺|一定|肯定|绝对)\s*(?:给您|帮你|为您|给您们)?\s*(?:改价|降价|少收|抹零|免运费|包邮)"),
    ),
    (
        "promise_discount",
        re.compile(r"(?:我|我们)?(?:保证|承诺|一定|肯定|绝对|私下)\s*(?:给您|帮你|为您)?\s*(?:折扣|优惠|返现|返红包|补偿|赔偿|红包)"),
    ),
    (
        "promise_refund",
        re.compile(r"(?:保证|承诺|一定|肯定|绝对)\s*(?:退款|退货|换货|赔付|免单|白送|免费送)"),
    ),
    (
        "promise_sla",
        re.compile(r"(?:保证|承诺|一定|肯定)\s*(?:能)?\s*\d+\s*(?:小时|天|日)\s*(?:内)?\s*(?:到|送达|发货|回复|处理)"),
    ),
)


@dataclass
class ComplianceScanResult:
    """合规扫描结果。

    Attributes:
        ok: 是否可直接出站（`block` 级零命中即 True）。
        text: 处理后的文本（`block` 命中时已替换为兜底话术；否则为脱敏后文本）。
        blocked: 是否被硬拦截并替换。
        violations: 命中的违规项标识列表，如 `["scheme_http", "点这里"]`。
        flags: 仅记录未拦截的项（绝对化 / 承诺性）。
        masked: 被自动脱敏的字段类别，如 `["phone", "order_no"]`。
        original: 原始文本（排障用，**仅入日志且经脱敏**）。
    """

    ok: bool = True
    text: str = ""
    blocked: bool = False
    violations: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    masked: list[str] = field(default_factory=list)
    original: str = ""

    @property
    def has_violation(self) -> bool:
        """是否命中任何硬拦截项。"""
        return bool(self.violations)

    @property
    def is_clean(self) -> bool:
        """是否零命中（无拦截、无记录项、无脱敏）。"""
        return not self.violations and not self.flags and not self.masked

    def to_dict(self) -> dict[str, Any]:
        """导出为可日志 / 可入库的字典（**不含原文**）。"""
        return {
            "ok": self.ok,
            "blocked": self.blocked,
            "violations": list(self.violations),
            "flags": list(self.flags),
            "masked": list(self.masked),
            "text_length": len(self.text),
        }


class OutboundGate:
    """出站合规闸。

    Attributes:
        strict: True 时绝对化 / 承诺性表述同样触发拦截替换。
        extra_phrases: 追加的禁用表述（如从商家材料包动态加载）。
        enabled: 关闭开关，仅用于本地调试；**生产必须为 True**。
    """

    def __init__(
        self,
        *,
        strict: bool = False,
        extra_phrases: tuple[str, ...] = (),
        enabled: bool | None = None,
    ) -> None:
        self.strict = strict
        self.extra_phrases = tuple(extra_phrases)
        self.enabled = (
            get_settings().compliance_gate_enabled if enabled is None else enabled
        )
        #: 扫描计数，供 `/metrics` 与验收报告使用
        self.stats: dict[str, int] = {
            "scanned": 0,
            "blocked": 0,
            "flagged": 0,
            "passed": 0,
        }

    # -- 扫描 ---------------------------------------------------------------

    def scan(
        self, text: str | None, *, allow_order_nos: set[str] | None = None
    ) -> ComplianceScanResult:
        """扫描文本，返回判定结果（**不修改入参**）。

        Args:
            text: 待扫描文本。None 或空串视为通过。
            allow_order_nos: 本会话已核验的订单号白名单。这些号不做脱敏
                （见模块 docstring「订单号的会话白名单」）。

        Returns:
            `ComplianceScanResult`。
        """
        raw = text or ""
        result = ComplianceScanResult(text=raw, original=raw)
        if not self.enabled or not raw:
            return result

        self.stats["scanned"] += 1

        # 1) 外链（硬拦截）
        for name, pattern in _LINK_PATTERNS:
            if pattern.search(raw):
                result.violations.append(name)

        # 2) 跳转引导话术（硬拦截）
        for phrase in _REDIRECT_PHRASES:
            if phrase in raw:
                result.violations.append(phrase)

        # 3) 敏感信息（硬拦截：身份证 / 银行卡 / 验证码 / 私加联系方式）
        for name, pattern in (
            ("sensitive_id_card", _ID_CARD_RE),
            ("sensitive_bank_card", _BANK_CARD_RE),
            ("sensitive_phrase", _SENSITIVE_PHRASE_RE),
        ):
            if pattern.search(raw):
                result.violations.append(name)

        # 4) 绝对化 / 承诺性 / 自定义禁用表述（默认仅记录，strict 时拦截）
        soft_hits: list[str] = []
        for phrase in _ABSOLUTE_PHRASES:
            if phrase in raw:
                soft_hits.append(phrase)
        for phrase in _PROMISE_PHRASES:
            if phrase in raw:
                soft_hits.append(phrase)
        for name, pattern in _PROMISE_PATTERNS:
            if pattern.search(raw):
                soft_hits.append(name)
        for phrase in self.extra_phrases:
            if phrase and phrase in raw:
                soft_hits.append(phrase)

        if self.strict and soft_hits:
            result.violations.extend(soft_hits)
            soft_hits = []
        result.flags.extend(soft_hits)

        # 5) PII 就地脱敏（不拦截，记录类别）
        masked_text, masked_fields = self._mask_pii(raw, allow_order_nos or set())
        result.masked = masked_fields

        # 6) 汇总
        if result.violations:
            result.ok = False
            result.blocked = True
            result.text = FALLBACK_REPLY
            self.stats["blocked"] += 1
            logger.warning(
                "出站内容被合规闸拦截",
                extra={
                    "extra": {
                        "violations": result.violations,
                        "text_length": len(raw),
                    }
                },
            )
        else:
            result.text = masked_text
            if result.flags:
                self.stats["flagged"] += 1
                logger.warning(
                    "出站内容命中风险表述（未拦截）",
                    extra={"extra": {"flags": result.flags}},
                )
            else:
                self.stats["passed"] += 1
        return result

    # -- PII 脱敏 -----------------------------------------------------------

    @staticmethod
    def _mask_pii(
        text: str, allow_order_nos: set[str]
    ) -> tuple[str, list[str]]:
        """对文本执行 7 类脱敏，返回 `(脱敏后文本, 命中类别列表)`。

        类别标识用于审计与验收报告；**不返回原文**。
        """
        # 惰性导入避免 core.logging 与 compliance 的循环依赖
        from app.core.logging import (  # noqa: PLC0415
            mask_address,
            mask_bank_card,
            mask_cn_name,
            mask_email,
            mask_id_card,
            mask_order_no,
            mask_phone,
            mask_tracking_no,
            mask_wechat_qq,
        )

        #: 顺序有讲究：长数字（身份证/银行卡）→ 联系方式 → 订单号 → 姓名。
        #: 与 `app.core.logging._MASK_PIPELINE` 同序，保证日志与出站口径一致。
        steps: tuple[tuple[str, Any], ...] = (
            ("id_card", mask_id_card),
            ("bank_card", mask_bank_card),
            ("email", mask_email),
            ("wechat_qq", mask_wechat_qq),
            ("phone", mask_phone),
            ("tracking_no", mask_tracking_no),
            ("address", mask_address),
            ("order_no", mask_order_no),
            ("cn_name", mask_cn_name),
        )

        out = text
        hits: list[str] = []
        for name, fn in steps:
            after = fn(out)
            if after != out:
                hits.append(name)
                out = after

        # 带称谓前缀的姓名（「收货人张伟已签收」这类核心脱敏规则会漏的写法）
        prefixed = _CN_NAME_PREFIX_RE.sub(
            lambda m: m.group(1) + m.group(2)[0] + "*" * (len(m.group(2)) - 1), out
        )
        if prefixed != out:
            if "cn_name" not in hits:
                hits.append("cn_name")
            out = prefixed

        # 订单号白名单：把已核验订单号从脱敏结果还原
        for order_no in allow_order_nos:
            if not order_no or order_no not in text:
                continue
            masked_form = mask_order_no(order_no)
            if masked_form != order_no and masked_form in out:
                out = out.replace(masked_form, order_no)
                if "order_no" in hits and order_no not in out:
                    continue
        return out, hits

    # -- 强制 ---------------------------------------------------------------

    def enforce(
        self,
        text: str | None,
        *,
        fallback: str | None = None,
        allow_order_nos: set[str] | None = None,
    ) -> str:
        """扫描并返回**可安全出站**的文本（命中硬拦截项则替换）。

        Args:
            text: 待发送文本。
            fallback: 自定义兜底话术；None 用 `FALLBACK_REPLY`。
            allow_order_nos: 本会话已核验的订单号白名单（不脱敏）。

        Returns:
            安全文本。未命中硬拦截时返回**已脱敏**文本。
        """
        result = self.scan(text, allow_order_nos=allow_order_nos)
        if result.blocked:
            return fallback or FALLBACK_REPLY
        return result.text

    def check(self, text: str | None) -> bool:
        """快捷判定：是否可安全出站（零硬拦截）。"""
        return self.scan(text).ok

    def enforce_result(
        self,
        text: str | None,
        *,
        fallback: str | None = None,
        allow_order_nos: set[str] | None = None,
    ) -> ComplianceScanResult:
        """与 `enforce` 相同，但返回完整扫描结果（审计与单测用）。"""
        result = self.scan(text, allow_order_nos=allow_order_nos)
        if result.blocked:
            result.text = fallback or FALLBACK_REPLY
        return result

    # -- 批量 ---------------------------------------------------------------

    def enforce_segments(self, segments: list[str]) -> list[str]:
        """逐段强制（BE-02 长消息分段后需逐段复检）。"""
        return [self.enforce(s) for s in segments]

    def snapshot(self) -> dict[str, int]:
        """导出统计快照。"""
        return dict(self.stats)


_GATE: OutboundGate | None = None


def get_outbound_gate() -> OutboundGate:
    """获取全局合规闸单例。"""
    global _GATE  # noqa: PLW0603
    if _GATE is None:
        _GATE = OutboundGate()
    return _GATE


def set_outbound_gate(gate: OutboundGate | None) -> None:
    """替换全局合规闸（**测试专用**）。"""
    global _GATE  # noqa: PLW0603
    _GATE = gate


def scan_outbound(
    text: str | None, *, allow_order_nos: set[str] | None = None
) -> ComplianceScanResult:
    """便捷函数：用全局闸扫描。"""
    return get_outbound_gate().scan(text, allow_order_nos=allow_order_nos)


def enforce_outbound(
    text: str | None,
    *,
    fallback: str | None = None,
    allow_order_nos: set[str] | None = None,
) -> str:
    """便捷函数：用全局闸强制。"""
    return get_outbound_gate().enforce(
        text, fallback=fallback, allow_order_nos=allow_order_nos
    )


def assert_no_outbound_link(text: str | None) -> None:
    """断言文本零外链（BE-02 DoD 自动化校验的复用入口）。

    Args:
        text: 待校验文本。

    Raises:
        AssertionError: 命中任一外链 / 跳转规则。
    """
    result = scan_outbound(text)
    link_violations = [
        v
        for v in result.violations
        if v.startswith("scheme_") or v.startswith("bare_") or v in _REDIRECT_PHRASES
    ]
    if link_violations:
        raise AssertionError(f"出站内容含外链/跳转：{link_violations}")


__all__ = [
    "FALLBACK_REPLY",
    "ComplianceScanResult",
    "OutboundGate",
    "assert_no_outbound_link",
    "enforce_outbound",
    "get_outbound_gate",
    "scan_outbound",
    "set_outbound_gate",
]
