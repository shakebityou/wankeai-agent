"""合规闸自检脚本（ASCII 输出，避免 Windows 控制台代码页乱码）。

用法：
    python scripts/check_compliance_gate.py

覆盖 BE-17 DoD 的四项：① 7 类脱敏；② 外链 100% 拦截；
③ 绝对化 / 承诺性表述；④ 敏感信息一律不提供。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.compliance import enforce_outbound, get_outbound_gate, scan_outbound  # noqa: E402

#: (用例说明, 输入文本, 期望 blocked, 期望脱敏后文本是否应变化)
CASES: list[tuple[str, str, bool]] = [
    # --- 外链（硬拦截）---
    ("http url", "详情见 http://example.com/item", True),
    ("https url", "https://taobao.com/x 这里", True),
    ("bare domain", "去 taobao.com 看看", True),
    ("pdd domain", "pinduoduo.com 上有", True),
    ("bare www", "www.example.com", True),
    ("app scheme", "weixin://dl/business", True),
    ("ip port", "访问 192.168.1.1:8080", True),
    ("jump phrase 1", "亲，我把链接发您哦", True),
    ("jump phrase 2", "点这里就能看到", True),
    ("jump phrase 3", "请跳转到活动页", True),
    ("jump phrase 4", "长按识别二维码领券", True),
    # --- 敏感信息（硬拦截）---
    ("id card", "身份证号 110101199003071234", True),
    ("bank card", "卡号 6222021234567890123", True),
    ("sensitive phrase", "请提供身份证照片", True),
    ("verify code", "把短信验证码发我", True),
    ("wechat ask", "加微信详聊", True),
    # --- 正常内容（放行，但可能脱敏）---
    ("clean product", "奶油小熊 20cm 现货，手洗 30 度温水加中性洗涤剂轻揉", False),
    ("clean faq", "签收后 7 天内可无理由退货，商品需保持完好", False),
    ("phone pii", "我的手机号 13812345678", False),
    ("order pii", "您的订单 202501150001 已发货", False),
    ("name pii", "收货人张伟已签收", False),
    ("email pii", "邮箱 zhangsan@example.com", False),
    # --- 风险表述（默认仅记录）---
    ("absolute", "这款是全网最低价", False),
    ("absolute 2", "本店销量第一", False),
    ("promise regex", "我保证给您改价", False),
    ("promise phrase", "保证赔偿您的损失", False),
]


def main() -> int:
    gate = get_outbound_gate()
    failures: list[str] = []

    print("=" * 78)
    print("BE-17 outbound compliance gate self-check")
    print("=" * 78)

    for label, text, expect_blocked in CASES:
        result = scan_outbound(text)
        safe = enforce_outbound(text)
        status = "BLOCK" if result.blocked else "PASS "
        mark = "ok" if result.blocked == expect_blocked else "FAIL"
        if mark == "FAIL":
            failures.append(f"{label}: blocked={result.blocked} expected={expect_blocked}")
        extra = f"masked={result.masked}" if result.masked else ""
        flags = f"flags={result.flags}" if result.flags else ""
        print(f"[{mark}] {label:18s} {status} {extra} {flags}")
        print(f"       in : {text}")
        print(f"       out: {safe}")
        if result.violations:
            print(f"       viol: {result.violations}")

    # --- 订单号白名单 ---
    print("-" * 78)
    order_no = "202501150001"
    whitelist_out = gate.enforce(
        f"您的订单 {order_no} 目前状态：已发货", allow_order_nos={order_no}
    )
    normal_out = gate.enforce(f"您的订单 {order_no} 目前状态：已发货")
    print(f"[whitelist] allow_order_nos -> {whitelist_out}")
    print(f"[whitelist] default       -> {normal_out}")
    if order_no not in whitelist_out:
        failures.append("order whitelist: verified order_no was masked unexpectedly")
    if order_no in normal_out:
        failures.append("order whitelist: unverified order_no was NOT masked")

    # --- 幂等性：重复脱敏结果不变 ---
    print("-" * 78)
    once = enforce_outbound("手机 13812345678 邮箱 zhangsan@example.com")
    twice = enforce_outbound(once)
    print(f"[idempotent] once : {once}")
    print(f"[idempotent] twice: {twice}")
    if once != twice:
        failures.append("masking is not idempotent")

    print("=" * 78)
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"ALL PASSED ({len(CASES)} cases + whitelist + idempotency)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
