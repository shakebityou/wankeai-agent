"""结构化日志与脱敏过滤（BE-17 合规要求之一：日志落盘时脱敏）。

**硬性要求**（任务书 BE-17 DoD）：
    日志 / 错误堆栈 / 监控看板中**零完整敏感字段**，含异常堆栈。

实现方式：
    1. 所有日志经 `SensitiveFilter` 过滤，正则替换敏感字段为脱敏形式；
    2. 输出为 JSON 行，字段包含 `ts` / `level` / `logger` / `trace_id` /
       `session_id` / `buyer_id` / `msg` / `extra`；
    3. 敏感字段在**进入 formatter 之前**就被替换，因此异常堆栈中的
       手机号、地址同样被脱敏。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

from app.core.trace import get_buyer_id, get_session_id, get_trace_id

# ---------------------------------------------------------------------------
# 脱敏规则（BE-17，7 类）
# ---------------------------------------------------------------------------
#: 手机号：138****8888
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
#: 身份证（18 位，含 X 结尾）。**一律不得提供**
_ID_CARD_RE = re.compile(
    r"(?<!\d)([1-9]\d{5})(\d{4})(\d{2})(\d{2})(\d{3})([\dXx])(?!\d)"
)
#: 银行卡（16-19 位连续数字，避免与订单号 12 位冲突）
_BANK_CARD_RE = re.compile(r"(?<!\d)(\d{6})\d{6,9}(\d{4})(?!\d)")
#: 邮箱：z***@example.com
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
#: 运单号：7836****4471（常见 10-15 位纯数字，且带快递前缀）
_TRACKING_RE = re.compile(r"\b([A-Z]{2}\d{2})(\d{4,8})(\d{4})\b")
#: 订单号：2025****0001（商家约定 order_no 为 12 位数字）
_ORDER_NO_RE = re.compile(r"(?<!\d)(\d{4})\d{4}(\d{4})(?!\d)")
#: 中文姓名：张**（2-4 字中文姓名，常见姓氏开头）
#:
#: **为什么要排除一批名字首字**：像「元」「满」「明」「平」「和」「方」
#: 这类单姓同时是高频普通词。「满 99 元包邮」「明天发货」「和平路」「强推」
#: 都会被当成人名打成 `元**` / `明天**`。脱敏过度比不脱敏更糟——
#: 用户看到的是一句被莫名截断的话，而真正该拦的隐私反而在别处被忽略。
#:
#: 判定口径是**名字首字**（`m.group(2)[0]`）落在下表里就不算姓名。
#: 这张表服务于「客服话术/商品文案」这一特定语料域，
#: 不是通用中文姓名识别——`_CN_NAME_RE` 本身也只用于本项目的日志脱敏。
_NAME_FALSE_POSITIVE_SUFFIX = (
    # 金额/营销
    "元", "价", "款", "券", "包", "邮", "减", "折", "赠", "优", "惠",
    "满", "买", "送", "活", "动", "秒", "杀",
    # 时间
    "天", "日", "月", "年", "周", "季", "早", "晚", "上", "下", "午",
    "前", "后", "点", "分", "秒", "次", "遍", "趟", "期", "间",
    # 常见形容词/副词
    "显", "示", "白", "色", "夜", "饭", "素", "气", "星", "面", "条",
    "子", "头", "力", "度", "式", "向", "法", "案", "华", "丽", "好",
    "行", "当", "中", "边", "里", "外", "来", "去", "种", "类", "个",
    "件", "只", "张", "字", "号", "码", "量", "重", "长", "宽", "高",
    "厚", "深", "远", "近", "快", "慢", "新", "老", "大", "小", "多",
    "少", "全", "半", "实", "真", "假", "正", "副", "总", "各", "每",
    # 业务动词
    "发", "退", "换", "补", "寄", "收", "付", "查", "问", "说", "看",
    "开", "关", "加", "减", "改", "选", "拍", "下", "取", "到",
)
_CN_NAME_RE = re.compile(
    r"(?<![\u4e00-\u9fa5])([赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华"
    r"金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    r"费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹"
    r"姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强"
    r"贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯昝管卢莫经房裘缪"
    r"干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮"
    r"羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭"
    r"厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴"
    r"郁胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边扈燕冀郏浦尚农"
    r"温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广"
    r"禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关"
    r"蒯相查后荆红游竺权逯盖益桓公])([\u4e00-\u9fa5]{1,2})(?=[\s，。,.、；;：:！!？?）)】]|$)"
)
#: 微信/QQ 号。**一律删除**
_WECHAT_QQ_RE = re.compile(
    r"(?:微信|weixin|wechat|vx|V信|扣扣|QQ|qq)\s*[:：]?\s*[A-Za-z0-9_-]{5,20}"
)
#: 地址：浙江省杭州市****（保留省市级，街道以下脱敏）
_ADDRESS_RE = re.compile(
    r"((?:北京|天津|上海|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|"
    r"山东|河南|湖北|湖南|广东|海南|四川|贵州|云南|陕西|甘肃|青海|台湾|内蒙古|广西|"
    r"西藏|宁夏|新疆|香港|澳门)[\u4e00-\u9fa5]{0,6}?(?:市|自治州|地区)?[\u4e00-\u9fa5]{0,6}?(?:市|区|县|旗))"
    r"[\u4e00-\u9fa5A-Za-z0-9\-]{2,}(?:号|路|街|道|巷|弄|栋|幢|单元|室|楼|村|组|镇|乡)"
)

#: 门牌号残段：`100 号 3 幢 502 室`、`12栋3单元501`、`88号`。
#:
#: **为什么单独一条**：上面的 `_ADDRESS_RE` 要求「省市区」后**紧跟**门牌，
#: 一旦中间被 `****` 或空格打断就整体匹配失败。而真实数据恰恰是这种形态
#: ——商家导出的语料已经把「省市区」脱敏成 `浙江省杭州市西湖区****`，
#: 门牌 `100 号 3 幢 502 室` 却是明文。此时整条地址规则无处可匹配，
#: 门牌就原样流进会话状态/日志，足以定位到具体住户。
#:
#: 匹配单元设计成「数字 + 门牌单位」的**整串**，一次吃掉
#: （`100 号 3 幢 502 室`），而不是分几次替换——分次会产出
#: `[门牌已屏蔽] [门牌已屏蔽]` 这种噪声。
#:
#: 要求**必须紧跟门牌单位**是刻意的：否则「满 99 元」「3 天无理由」
#: 「20cm」「第 3 次」「48 小时」「5 个工作日」这些营销/售后数字
#: 会被误伤（第一版就把「满 99 元」打成了「满 99 元**」）。
_HOUSE_UNIT = r"(?:号|幢|栋|单元|室|楼|层|户|弄|巷|组|队|排|座)"
_HOUSE_NO_RE = re.compile(
    rf"\d+\s*{_HOUSE_UNIT}(?:\s*\d+\s*{_HOUSE_UNIT})*"
)
#: 敏感键名 → 强制全掩码（键名出现即视为敏感，值整体替换）
_SENSITIVE_KEYS = frozenset(
    {
        "id_card",
        "idcard",
        "id_no",
        "identity_card",
        "bank_card",
        "bankcard",
        "card_no",
        "cvv",
        "password",
        "passwd",
        "app_secret",
        "access_key_secret",
        "secret",
        "token",
        "authorization",
        "internal_token",
        "api_key",
        "apikey",
        "wechat",
        "weixin",
        "qq",
    }
)


def mask_phone(text: str) -> str:
    """手机号脱敏：`13812345678` → `138****5678`。"""
    return _PHONE_RE.sub(r"\1****\2", text)


def mask_id_card(text: str) -> str:
    """身份证脱敏：一律替换为 `[身份证已屏蔽]`。"""
    return _ID_CARD_RE.sub("[身份证已屏蔽]", text)


def mask_bank_card(text: str) -> str:
    """银行卡脱敏：一律替换为 `[银行卡已屏蔽]`。"""
    return _BANK_CARD_RE.sub("[银行卡已屏蔽]", text)


def mask_email(text: str) -> str:
    """邮箱脱敏：`zhangsan@example.com` → `z***@example.com`。"""
    return _EMAIL_RE.sub(r"\1***\2", text)


def mask_tracking_no(text: str) -> str:
    """运单号脱敏：`ZT123456789012` → `ZT12****9012`。"""
    return _TRACKING_RE.sub(r"\1****\3", text)


def mask_order_no(text: str) -> str:
    """订单号脱敏：`202501150001` → `2025****0001`（前 4 后 4）。"""
    return _ORDER_NO_RE.sub(r"\1****\2", text)


def mask_cn_name(text: str) -> str:
    """姓名脱敏：`张三` → `张**`，`欧阳娜娜` → `欧**`（保留姓氏）。

    用回调而非正则负向断言，是为了能做**两项**排除：
    1. 名字首字是高频普通词（`元包邮`、`明天发`）——保留原文；
    2. 匹配为空时原样返回，避免 `*` * 0 之类的边界问题。
    """
    false_positive = set(_NAME_FALSE_POSITIVE_SUFFIX)

    def _replace(m: re.Match[str]) -> str:
        given = m.group(2)
        if not given or given[0] in false_positive:
            return m.group(0)
        return m.group(1) + "*" * len(given)

    return _CN_NAME_RE.sub(_replace, text)


def mask_address(text: str) -> str:
    """地址脱敏：`浙江省杭州市西湖区文一西路100号` → `浙江省杭州市****`。

    两段处理，缺一不可：

    1. `_ADDRESS_RE` 吃「完整地址」，保留省级定位（坐席需要知道大概区域
       才能判断时效，如「江浙沪次日达」）。
    2. `_HOUSE_NO_RE` 吃**残段**：商家语料常是「省市区已脱敏 + 门牌明文」
       的形态（`浙江省杭州市西湖区**** 100 号 3 幢 502 室`），第一段
       规则因为中间有 `****` 而匹配不上，必须靠第二段兜住门牌。
    """
    text = _ADDRESS_RE.sub(r"\1****", text)
    return _HOUSE_NO_RE.sub("[门牌已屏蔽]", text)


def mask_wechat_qq(text: str) -> str:
    """微信/QQ 号一律删除。"""
    return _WECHAT_QQ_RE.sub("[联系方式已屏蔽]", text)


#: 脱敏函数执行顺序。**顺序有讲究**：先处理长数字（身份证/银行卡），
#: 再处理订单号，避免 12 位订单号被银行卡规则误伤，反之亦然。
_MASK_PIPELINE = (
    mask_id_card,
    mask_bank_card,
    mask_email,
    mask_wechat_qq,
    mask_phone,
    mask_tracking_no,
    mask_address,
    mask_order_no,
    mask_cn_name,
)


def mask_text(text: str) -> str:
    """对任意文本执行全部脱敏规则（幂等：重复调用不改变结果）。

    Args:
        text: 原始文本，可能是日志消息、异常堆栈、prompt 片段。

    Returns:
        脱敏后的文本。
    """
    if not text:
        return text
    result = text
    for fn in _MASK_PIPELINE:
        result = fn(result)
    return result


def mask_value(value: Any) -> Any:
    """递归脱敏任意 JSON 可序列化值。

    规则：
        * 字符串 → 走 `mask_text`；
        * dict → 键名命中 `_SENSITIVE_KEYS` 时整体替换为 `***`，否则递归；
        * list/tuple → 逐元素递归；
        * 其他类型原样返回。
    """
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS:
                out[k] = "***"
            else:
                out[k] = mask_value(v)
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(mask_value(v) for v in value)
    return value


class SensitiveFilter(logging.Filter):
    """日志过滤器：把 record 的消息与参数脱敏后再交给 formatter。

    **必须在 formatter 之前执行**，这样异常堆栈（`exc_text`）也一并被脱敏。
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            if isinstance(record.msg, str):
                record.msg = mask_text(record.msg)
            elif record.msg is not None:
                record.msg = mask_value(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = mask_value(record.args)  # type: ignore[assignment]
                elif isinstance(record.args, tuple):
                    record.args = tuple(mask_value(a) for a in record.args)
        except Exception:  # pragma: no cover - 过滤器自身绝不抛异常
            pass
        return True


class JsonFormatter(logging.Formatter):
    """JSON 行格式化器，自动注入 trace_id / session_id / buyer_id。"""

    def format(self, record: logging.LogRecord) -> str:
        """把日志记录序列化为单行 JSON。"""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": get_trace_id(),
            "msg": record.getMessage(),
        }
        session_id = get_session_id()
        buyer_id = get_buyer_id()
        if session_id:
            payload["session_id"] = session_id
        if buyer_id:
            payload["buyer_id"] = buyer_id

        # 业务附加字段：logger.info("...", extra={"extra": {...}})
        extra = getattr(record, "extra", None)
        if extra is not None:
            payload["extra"] = mask_value(extra)

        if record.exc_info:
            payload["exc"] = mask_text(self.formatException(record.exc_info))

        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:  # pragma: no cover
            return json.dumps(
                {"level": record.levelname, "msg": mask_text(str(record.msg))},
                ensure_ascii=False,
            )


_CONFIGURED = False


def setup_logging(level: str | int = logging.INFO) -> None:
    """初始化根日志器：JSON 输出 + 敏感信息过滤。重复调用安全。

    Args:
        level: 日志级别，默认 INFO。
    """
    global _CONFIGURED  # noqa: PLW0603
    root = logging.getLogger()
    if _CONFIGURED:
        root.setLevel(level)
        return

    root.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SensitiveFilter())
    root.addHandler(handler)
    root.setLevel(level)

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "urllib3", "pymilvus", "elastic_transport"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取带脱敏能力的 logger。

    Args:
        name: 通常传 `__name__`。

    Returns:
        已挂载 `SensitiveFilter` 的 logger（即使根日志器未初始化也安全）。
    """
    logger = logging.getLogger(name)
    if not any(isinstance(f, SensitiveFilter) for f in logger.filters):
        logger.addFilter(SensitiveFilter())
    return logger


__all__ = [
    "JsonFormatter",
    "SensitiveFilter",
    "get_logger",
    "mask_address",
    "mask_bank_card",
    "mask_cn_name",
    "mask_email",
    "mask_id_card",
    "mask_order_no",
    "mask_phone",
    "mask_text",
    "mask_tracking_no",
    "mask_value",
    "mask_wechat_qq",
    "setup_logging",
]
