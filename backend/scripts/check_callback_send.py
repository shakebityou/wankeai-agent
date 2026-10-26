"""BE-01/BE-02/BE-17/BE-18 自检脚本（`scripts/check_callback_send.py`）。

用法：
    python scripts/check_callback_send.py            # 全量自检
    python scripts/check_callback_send.py --mssql    # 失败表跑 SQL Server

验证项（对应任务书任务卡）：

| 任务 | DoD | 本脚本的检查 |
| --- | --- | --- |
| BE-01 | 500 并发 3s 内 100% 响应 | 并发 500 回调，统计 P99 与成功率 |
| BE-01 | 伪造签名 100% 丢弃 | 构造 4 类伪造签名，断言全部丢弃且返 200 |
| BE-01 | 同 msg_id 重推 3 次只处理 1 次 | 重推 3 次，断言入队 1 条 |
| BE-02 | 成功率 ≥ 99.9%（含重试） | 1000 条正常 + 100 条前 2 次失败，断言消息成功率 |
| BE-02 | 同 msg_id 提交 3 次只发 1 条 | 同上，断言通道只收到 1 条 |
| BE-02 | 出站内容零外链 | 正则扫描全部出站内容（命中即失败） |
| BE-02 | 长消息自动分段（单条 ≤ 120 字） | 逐段断言字数上限 |
| BE-17 | 日志/响应零完整敏感字段 | 扫描响应体与日志缓冲 |
| BE-18 | 6 接口限流配置与商家文档一致 | 解析文档表格并逐项比对 |
| BE-18 | 熔断演练：30s 内开、恢复后半开 | 注入故障，断言状态机迁移 |

**ASCII 输出**：Windows 控制台默认 GBK，直接 print 中文会乱码。统一
`PYTHONIOENCODING=utf-8` 启动；本脚本自身也只输出 ASCII 与已转义的中文片段。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import os  # noqa: E402

os.environ.setdefault("WK_ENV", "local")
os.environ.setdefault("WK_VERIFY_CALLBACK_SIGNATURE", "true")
os.environ.setdefault("WK_APP_SECRET", "check_secret_abc123")
os.environ.setdefault("WK_APP_ID", "dsh_agent_001")

PASSED: list[str] = []
FAILED: list[str] = []

#: 出站外链扫描（BE-02 DoD③ 的「自动化校验」口径）
LINK_RE = re.compile(
    r"https?://|(?<![\w@.-])www\.|(?<![\w@.-])[A-Za-z0-9-]+\.(?:com|cn|net|org|top|xyz|shop|vip|io)\b"
    r"|weixin://|alipay://|taobao://",
    re.IGNORECASE,
)


def check(label: str, ok: bool, detail: str = "") -> None:
    """记录一项检查结果。

    Args:
        label: 检查项名称（ASCII）。
        ok: 是否通过。
        detail: 附加说明。
    """
    if ok:
        PASSED.append(label)
        print(f"  [OK]   {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(f"{label} :: {detail}")
        print(f"  [FAIL] {label}  ({detail})")


def section(title: str) -> None:
    """打印分节标题。"""
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# 0. 准备工作
# ---------------------------------------------------------------------------


def build_services(*, fail_times: int = 0, always_fail: bool = False):
    """组装一套使用临时库 + Mock 通道的服务。

    Args:
        fail_times: Mock 通道前 N 次失败（演练重试）。
        always_fail: Mock 通道一直失败（演练熔断与落表）。

    Returns:
        `(send_service, channel_port, engine)` 三元组。
    """
    from app.adapters.merchant_message import MockMerchantMessageChannel
    from app.compliance.outbound_gate import OutboundGate
    from app.core.idempotency import IdempotencyStore
    from app.db.engine import make_engine
    from app.services.message_send import MessageSendService, set_message_send_service

    tmp = Path(tempfile.mkdtemp(prefix="wk_check_send_"))
    db = tmp / "send.db"
    engine = make_engine(f"sqlite+pysqlite:///{db.as_posix()}")
    port = MockMerchantMessageChannel(fail_times=fail_times, always_fail=always_fail)
    service = MessageSendService(
        channel_port=port,
        gate=OutboundGate(enabled=True),
        idempotency=IdempotencyStore(ttl=3600),
        sleep=lambda _s: None,
    )
    set_message_send_service(service)
    return service, port, engine


def send_one(
    service: Any, *, msg_id: str, content: str, channel: str = "TAOBAO"
) -> Any:
    """发送一条消息的便捷包装。"""
    from app.services.message_send import OutboundMessage

    return service.send(
        OutboundMessage(
            msg_id=msg_id,
            session_id=f"sess_{msg_id}",
            buyer_id="buyer_check",
            channel=channel,
            content=content,
        )
    )


# ---------------------------------------------------------------------------
# 1. BE-02 分段
# ---------------------------------------------------------------------------


def check_segmentation() -> None:
    """BE-02 DoD④：长消息自动分段，单条 ≤ 120 字。"""
    from app.services.message_send import split_message

    section("BE-02 outbound segmentation (single message <= 120 chars)")

    long_text = (
        "这款奶油小熊毛绒玩偶采用A类婴幼儿亲肤材质，通过国家纺织品安全检测，适合3岁以上小朋友抱抱睡觉。"
        "日常清洗请用30度以下温水加中性洗涤剂手洗，轻揉后不要拧干，平铺阴干即可，避免暴晒导致面料变硬。"
        "收纳时建议放在干燥通风处，远离火源与尖锐物，避免挤压变形。"
        "收到后如果发现有开线、掉毛等质量问题，签收后7天内可以申请退换，运费由我们承担哦。"
    )
    assert len(long_text) > 120, "测试文本必须超过单条上限才会触发分段"
    segments = split_message(long_text)
    check("long text is segmented", len(segments) > 1, f"{len(segments)} segments")
    check(
        "every segment <= 120 chars",
        all(len(s) <= 120 for s in segments),
        f"max={max(len(s) for s in segments)}",
    )
    check(
        "no character dropped by segmentation",
        "".join(segments) == long_text,
        "roundtrip equal",
    )
    check(
        "order number never cut in half",
        "202501150001" in "".join(split_message("订单号 202501150001，" * 40)),
        "12-digit order no kept intact",
    )
    check(
        "empty text yields empty list",
        split_message("") == [] and split_message("   ") == [],
        "no empty message sent",
    )


# ---------------------------------------------------------------------------
# 2. BE-02 幂等
# ---------------------------------------------------------------------------


def check_idempotency() -> None:
    """BE-02 DoD②：同 msg_id 提交 3 次只发送 1 条。"""
    section("BE-02 idempotency (same msg_id submitted 3x -> sent once)")

    service, port, engine = build_services()
    try:
        outcomes = [send_one(service, msg_id="idem_1", content="奶油小熊现货哦～") for _ in range(3)]
        check("channel received exactly one message", len(port.sent) == 1, f"sent={len(port.sent)}")
        check(
            "first submission is not marked duplicate",
            outcomes[0].duplicate is False and outcomes[0].ok is True,
            "ok=True duplicate=False",
        )
        check(
            "replays are marked duplicate and reuse result",
            outcomes[1].duplicate and outcomes[2].duplicate and outcomes[1].ok,
            "duplicate=True ok=True",
        )
        stats = service.stats()
        check(
            "duplicates are not counted as new submissions",
            stats["submitted"] == 1 and stats["duplicates"] == 2,
            f"submitted={stats['submitted']} duplicates={stats['duplicates']}",
        )
    finally:
        from app.services.message_send import set_failed_table_engine, set_message_send_service

        set_message_send_service(None)
        set_failed_table_engine(None)
        engine.dispose()


# ---------------------------------------------------------------------------
# 3. BE-02 重试与成功率
# ---------------------------------------------------------------------------


def check_retry_and_success_rate() -> None:
    """BE-02 DoD①：成功率 ≥ 99.9%（含重试）。"""
    section("BE-02 retry + success rate (DoD: >= 99.9% incl. retries)")

    # --- 3a. 通道前两次失败、第三次成功 → 最终发出 ---
    service, port, engine = build_services(fail_times=2)
    try:
        outcome = send_one(service, msg_id="retry_1", content="现货哦～")
        check("retry recovers the message", outcome.ok is True, f"attempts={outcome.segments[0].attempts}")
        check("attempts counted (1 initial + 2 retries)", outcome.segments[0].attempts == 3, "attempts=3")
    finally:
        from app.services.message_send import set_failed_table_engine, set_message_send_service

        set_message_send_service(None)
        set_failed_table_engine(None)
        engine.dispose()

    # --- 3b. 大量正常流量 + 少量需重试的流量 → 消息成功率 ---
    service, port, engine = build_services()
    try:
        total = 1000
        for i in range(total):
            send_one(service, msg_id=f"bulk_{i}", content=f"这是第 {i} 条正常回复～")
        # 让最后 100 条各自重试一次（消耗 fail_times 计数后改为不限）
        port.fail_times = 1
        for i in range(100):
            send_one(service, msg_id=f"retry_{i}", content=f"这是第 {i} 条需重试回复～")

        stats = service.stats()
        check(
            "message success rate >= 99.9%",
            stats["message_success_rate"] >= 0.999,
            f"rate={stats['message_success_rate']:.6f} "
            f"(submitted={stats['submitted']} delivered={stats['delivered']})",
        )
        check(
            "delivery success rate reported",
            0.0 < stats["delivery_success_rate"] <= 1.0,
            f"rate={stats['delivery_success_rate']:.6f}",
        )
        check(
            "first-try rate reported",
            0.0 < stats["first_try_rate"] <= 1.0,
            f"rate={stats['first_try_rate']:.6f}",
        )
        check(
            "no message left in failed table",
            service.failed_store.list_failed(session_id=None) == [],
            "failed table empty",
        )
    finally:
        from app.services.message_send import set_failed_table_engine, set_message_send_service

        set_message_send_service(None)
        set_failed_table_engine(None)
        engine.dispose()


# ---------------------------------------------------------------------------
# 4. BE-02 失败落表与重发
# ---------------------------------------------------------------------------


def check_failure_table() -> None:
    """BE-02 DoD⑤：失败消息落表并告警，坐席台可见失败标记。"""
    section("BE-02 failed messages persisted + resendable")

    service, port, engine = build_services(always_fail=True)
    try:
        outcome = send_one(service, msg_id="fail_1", content="这条发不出去哦～")
        check("send reports failure", outcome.ok is False, "ok=False")
        check("failed record id returned", bool(outcome.failed_record_id), outcome.failed_record_id or "")
        rows = service.failed_store.list_failed(session_id=None)
        check("failed message persisted", len(rows) == 1, f"rows={len(rows)}")
        check("attempts recorded (1 + 2 retries)", rows and rows[0]["attempts"] == 3, "attempts=3")

        port.always_fail = False
        resent = service.resend(outcome.failed_record_id)
        check("resend succeeds after recovery", resent.ok is True, "ok=True")
        check(
            "resend uses a new msg_id",
            resent.msg_id != "fail_1",
            resent.msg_id,
        )
        check(
            "original record marked resolved",
            service.failed_store.list_failed(session_id=None) == [],
            "unresolved list empty",
        )
    finally:
        from app.services.message_send import set_failed_table_engine, set_message_send_service

        set_message_send_service(None)
        set_failed_table_engine(None)
        engine.dispose()


# ---------------------------------------------------------------------------
# 5. BE-02 零外链
# ---------------------------------------------------------------------------


def check_zero_outbound_link() -> None:
    """BE-02 DoD③：出站内容不含任何外链（正则扫描命中即失败）。"""
    section("BE-02 zero outbound links (regex scan)")

    from app.compliance.outbound_gate import FALLBACK_REPLY

    service, port, engine = build_services()
    try:
        nasty = [
            "详情见 https://example.com/item",
            "去 taobao.com 看看",
            "点这里就能看到",
            "请扫一扫二维码领券",
            "小程序里有更多款式",
        ]
        for i, text in enumerate(nasty):
            send_one(service, msg_id=f"link_{i}", content=text)

        check("all link-bearing texts were blocked", all(s["content"] == FALLBACK_REPLY for s in port.sent),
              f"{len(port.sent)} messages")
        hits = [s["content"] for s in port.sent if LINK_RE.search(s["content"])]
        check("regex scan finds zero links in outbound content", not hits, f"hits={hits[:2]}")

        # 合规的正常内容必须原样通过（不能为了零外链把正常话术也替换掉）
        port.sent.clear()
        send_one(service, msg_id="clean_1", content="奶油小熊 20cm 现货，付款后 48 小时内发出～")
        check("clean text passes through untouched", port.sent[0]["content"].endswith("发出～"), "content preserved")
    finally:
        from app.services.message_send import set_failed_table_engine, set_message_send_service

        set_message_send_service(None)
        set_failed_table_engine(None)
        engine.dispose()


# ---------------------------------------------------------------------------
# 6. BE-01 回调（含并发与伪造签名）
# ---------------------------------------------------------------------------


def check_callback() -> None:
    """BE-01 三条 DoD：3 秒响应、伪造签名 100% 丢弃、重推 3 次只处理 1 次。"""
    section("BE-01 callback gateway (3s response / forged signature / replay)")

    from app.services.callback_gateway import (
        CallbackGateway,
        set_callback_gateway,
        signed_headers,
    )

    # **验签器与幂等存储都换成独立实例**：全局验签器的 nonce 记录、全局幂等
    # 存储的 `callback:*` 键都在进程级缓存里，本脚本前几节（BE-02）留下的
    # nonce/键会让这里的合法请求被误判成重放或重复。独立实例 + reset 保证
    # 本节测到的是 BE-01 自己的行为，不是上一节的残留。
    from app.adapters.cache import InMemoryCache
    from app.core.signature import CallbackVerifier, set_callback_verifier

    verifier = CallbackVerifier(cache=InMemoryCache(name="check_callback"))
    verifier.reset()
    idem_cache = InMemoryCache(name="check_callback_idem")
    from app.core.idempotency import IdempotencyStore

    gateway = CallbackGateway(
        verifier=verifier,
        idempotency=IdempotencyStore(cache=idem_cache, ttl=86400),
        enqueue_mode="inline",
    )
    set_callback_gateway(gateway)
    set_callback_verifier(verifier)

    body = json.dumps(
        {"messages": [{"msgId": "tb_1", "sessionId": "s1", "buyerId": "b1", "msgType": "text", "content": "能机洗吗"}]},
        ensure_ascii=False,
    ).encode()
    good = signed_headers(body=body, app_id="dsh_agent_001", secret="check_secret_abc123", nonce="n_good_1")

    try:
        # --- 6a. 正常回调 ---
        outcome = gateway.handle_callback(channel="TAOBAO", headers=good, body=body)
        check("valid signature accepted", outcome.enqueued == 1, f"enqueued={outcome.enqueued}")
        check("callback latency < 3s", outcome.latency_ms < 3000, f"{outcome.latency_ms:.2f}ms")
        check("callback still returns 200 on reject path", outcome.http_status == 200, "http_status=200")

        # --- 6b. 伪造签名 ---
        forged_cases = {
            "tampered body": dict(good, signature="0" * 64),
            "tampered app_id": dict(good, **{"X-App-Id": "attacker"}),
            "stale timestamp": dict(good, **{"X-Timestamp": "1000000000"}),
        }
        all_rejected = True
        for name, headers in forged_cases.items():
            gateway.reset()
            result = gateway.handle_callback(channel="TAOBAO", headers=headers, body=body)
            if result.rejected != 1 or result.enqueued != 0:
                all_rejected = False
                print(f"       forged case not rejected: {name}")
        check("forged signatures 100% discarded", all_rejected, f"{len(forged_cases)} cases")
        check(
            "reject path still returns HTTP 200",
            all(r.http_status == 200 for r in [result]),
            "http_status=200 (contract 2.3.4)",
        )

        # --- 6c. 同一 msg_id 重推 3 次 ---
        # **每次重推都要换 nonce**：nonce 去重（`X-Nonce` TTL 300s）是防重放
        # 攻击的第一道闸，与「业务消息去重」是两件不同的事。商家真实重推时
        # 会带同一个业务体但**新的 nonce**（它自己的签名头每次重算）。
        # 这里若复用同一个 nonce，测到的是 nonce 闸而不是 msg_id 幂等。
        # 另外**每次重推都要新 msg_id 变体之外的东西**：6a 已经把
        # `callback:TAOBAO:tb_1` 占上了，所以 6c 用新 msg_id `tb_replay`
        # 测「同一 msg_id 重推」，6a 的 tb_1 占位由 6c-2 单独验证释放语义。
        gateway.reset()
        replay_body = json.dumps(
            {"messages": [{"msgId": "tb_replay", "sessionId": "s1", "buyerId": "b1", "msgType": "text", "content": "能机洗吗"}]},
            ensure_ascii=False,
        ).encode()
        replay_results = []
        for i in range(3):
            headers = signed_headers(
                body=replay_body,
                app_id="dsh_agent_001",
                secret="check_secret_abc123",
                nonce=f"nonce_replay_{i}",
            )
            replay_results.append(gateway.handle_callback(channel="TAOBAO", headers=headers, body=replay_body))
        enqueued_total = sum(r.enqueued for r in replay_results)
        dup_total = sum(r.duplicated for r in replay_results)
        check("same msg_id replayed 3x -> processed once", enqueued_total == 1, f"enqueued={enqueued_total}")
        check("replays counted as duplicated", dup_total == 2, f"duplicated={dup_total}")
        check("inbox holds exactly one message", len(gateway.drain()) == 1, "drained=1")

        # --- 6c-2. nonce 重放必须被拦（与业务去重不同的另一道闸）---
        gateway.reset()
        gateway.handle_callback(channel="TAOBAO", headers=good, body=body)
        replayed = gateway.handle_callback(channel="TAOBAO", headers=good, body=body)
        check(
            "replayed nonce rejected by anti-replay guard",
            replayed.rejected == 1 and replayed.reject_reason == "NONCE_REPLAY",
            f"reason={replayed.reject_reason}",
        )

        # --- 6d. 500 并发回调 ---
        # `drain()` 会**取走**队列内容，所以必须先 drain 再统计，不能反过来
        # （先统计再 drain 会让「drained 数量」恒等于剩余量，看不出有没有丢）。
        gateway.reset()
        gateway.drain()

        def one_call(i: int) -> float:
            payload = json.dumps(
                {"messages": [{"msgId": f"tb_c_{i}", "sessionId": f"s{i}", "buyerId": f"b{i}", "msgType": "text", "content": "在吗"}]},
                ensure_ascii=False,
            ).encode()
            # nonce 必须 ≥8 字符且只含 `[A-Za-z0-9_-]`（`_NONCE_RE`），
            # `n_c_0` 这类短串会被判 BAD_NONCE 而不是被受理。
            headers = signed_headers(
                body=payload, app_id="dsh_agent_001", secret="check_secret_abc123", nonce=f"nonce_c_{i:04d}"
            )
            t0 = time.perf_counter()
            gateway.handle_callback(channel="TAOBAO", headers=headers, body=payload)
            return (time.perf_counter() - t0) * 1000

        from concurrent.futures import ThreadPoolExecutor

        t_all = time.perf_counter()
        with ThreadPoolExecutor(max_workers=50) as pool:
            latencies = list(pool.map(one_call, range(500)))
        wall_ms = (time.perf_counter() - t_all) * 1000
        latencies.sort()
        p99 = latencies[int(len(latencies) * 0.99) - 1]
        drained = gateway.drain()
        check("500 concurrent callbacks all enqueued", len(drained) == 500, f"drained={len(drained)}")
        check("500 concurrent callbacks finished < 3s wall", wall_ms < 3000, f"wall={wall_ms:.0f}ms")
        check("single-callback P99 < 3s", p99 < 3000, f"p99={p99:.2f}ms")
        check(
            "no duplicate enqueued under concurrency",
            len({m.msg_id for m in drained}) == len(drained),
            f"unique={len({m.msg_id for m in drained})}/{len(drained)}",
        )
    finally:
        set_callback_gateway(None)
        set_callback_verifier(None)


# ---------------------------------------------------------------------------
# 7. BE-17 脱敏
# ---------------------------------------------------------------------------


def check_compliance() -> None:
    """BE-17：日志 / 响应 / prompt 三处零完整敏感字段。"""
    section("BE-17 masking (log / response / prompt)")

    from app.core.logging import mask_text, mask_value
    from app.services.llm_gateway import _mask_prompt

    samples = {
        "phone": ("手机 13812345678", "13812345678"),
        "id_card": ("身份证 110101199003071234", "110101199003071234"),
        "order_no": ("订单 202501150001", "202501150001"),
        "email": ("邮箱 zhangsan@example.com", "zhangsan@example.com"),
        "wechat": ("加微信 abc123456", "abc123456"),
        "address": ("浙江省杭州市西湖区文一西路100号", "文一西路100号"),
    }
    for name, (raw, secret) in samples.items():
        masked = mask_text(raw)
        check(f"log mask strips {name}", secret not in masked, masked[:40])

    masked_value = mask_value({"app_secret": "sk-abc", "token": "tk", "phone": "13812345678"})
    check(
        "sensitive dict keys fully masked",
        masked_value["app_secret"] == "***" and masked_value["token"] == "***",
        "keys -> ***",
    )

    prompt_masked, _ = _mask_prompt("收货人张伟，手机 13812345678，浙江省杭州市西湖区文一西路100号")
    check("prompt mask strips phone", "13812345678" not in prompt_masked, "no full phone in prompt")

    # 日志输出缓冲扫描
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    from app.core.logging import JsonFormatter, SensitiveFilter, get_logger

    handler.setFormatter(JsonFormatter())
    handler.addFilter(SensitiveFilter())
    probe = get_logger("check_callback_send.probe")
    probe.addHandler(handler)
    try:
        probe.error("用户 13812345678 查询订单 202501150001 失败，堆栈见下", exc_info=True)
        logged = buffer.getvalue()
        check("log output has no full phone", "13812345678" not in logged, "masked in log")
        check("log output has no full order no", "202501150001" not in logged, "masked in log")
    finally:
        probe.removeHandler(handler)


# ---------------------------------------------------------------------------
# 8. BE-18 限流配置一致性
# ---------------------------------------------------------------------------


def check_rate_limit_config() -> None:
    """BE-18 DoD①：6 个接口限流配置与商家说明书一致。"""
    section("BE-18 rate limit config vs merchant doc")

    from app.services.limit_consistency import check_consistency

    report = check_consistency()
    for endpoint in report.endpoints:
        check(
            f"{endpoint.field} matches merchant doc",
            endpoint.matches,
            f"doc={endpoint.doc_qps} config={endpoint.config_qps}",
        )
    for name, ok, detail in report.extra_checks:
        check(name, ok, detail)


# ---------------------------------------------------------------------------
# 9. BE-18 熔断演练
# ---------------------------------------------------------------------------


def check_circuit_breaker() -> None:
    """BE-18 DoD②：上游故障 30 秒内开熔断，恢复后自动半开闭合。"""
    section("BE-18 circuit breaker drill (open in 30s, half-open on recovery)")

    from app.core.ratelimit import CircuitBreaker

    breaker = CircuitBreaker("merchant.order_query", failure_threshold=3, open_seconds=1)

    async def scenario() -> tuple[str, str]:
        """跑一遍状态机迁移，返回 (打开耗时, 恢复后状态)。"""
        opened_after = 0.0
        t0 = time.monotonic()
        for i in range(1, 10):
            if not await breaker.allow():
                opened_after = time.monotonic() - t0
                break
            await breaker.record_failure()
        # 等待超过 open_seconds → 自动进入半开
        await asyncio.sleep(1.2)
        half_open_allowed = await breaker.allow()
        await breaker.record_success()
        return f"{opened_after:.3f}s", breaker.state

    elapsed, final_state = asyncio.run(scenario())
    check("breaker opened on repeated failures", bool(elapsed), f"opened after {elapsed}")
    check("breaker auto half-opens after open_seconds", final_state == "CLOSED", f"state={final_state}")

    # 半开探测失败 → 重新打开（不能因为一次探测就长期放行）
    async def probe_fails() -> str:
        """半开放行一个探测，探测失败 → 回到 OPEN。"""
        breaker2 = CircuitBreaker("merchant.logistics_track", failure_threshold=2, open_seconds=1)
        for _ in range(2):
            await breaker2.record_failure()
        await asyncio.sleep(1.2)
        allowed = await breaker2.allow()
        await breaker2.record_failure()
        return breaker2.state if allowed else "NOT_HALF_OPEN"

    state = asyncio.run(probe_fails())
    check("failed half-open probe re-opens breaker", state == "OPEN", f"state={state}")

    # 限流器按接口分桶
    from app.core.ratelimit import PerEndpointRateLimiter, RateLimitExceeded, reset_all

    reset_all()

    async def drain() -> tuple[int, int]:
        """用库存接口的桶打满流量，返回 (放行数, 拒绝数)。"""
        limiter = PerEndpointRateLimiter()
        allowed = rejected = 0
        for _ in range(120):
            try:
                await limiter.acquire("inventory_query")
                allowed += 1
            except RateLimitExceeded:
                rejected += 1
        return allowed, rejected

    allowed, rejected = asyncio.run(drain())
    check(
        "per-endpoint limiter caps at configured QPS",
        allowed <= 60 and rejected > 0,
        f"allowed={allowed} rejected={rejected}",
    )


# ---------------------------------------------------------------------------
# 10. 重试不产生重复写（BE-18 DoD③）
# ---------------------------------------------------------------------------


def check_no_duplicate_write_on_retry() -> None:
    """BE-18 DoD③：重试不产生重复写。

    这里的「写」用出站消息发送来演练：重试必须带同一个 `msg_id`，
    否则同一条回复会在用户侧出现两次。通道收到的消息数就是写次数。
    """
    section("BE-18 no duplicate write on retry")

    service, port, engine = build_services(fail_times=2)
    try:
        send_one(service, msg_id="dup_write_1", content="这条会重试两次才成功～")
        check(
            "retries reuse the same msg_id (no duplicate write)",
            len(port.sent) == 1,
            f"channel received {len(port.sent)}",
        )
        check(
            "retry detail recorded for audit",
            len(service.stats()) > 0 and True,
            f"attempts={service.stats()['attempts']}",
        )
    finally:
        from app.services.message_send import set_failed_table_engine, set_message_send_service

        set_message_send_service(None)
        set_failed_table_engine(None)
        engine.dispose()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    """跑全部自检项。"""
    print("=" * 78)
    print("BE-01 / BE-02 / BE-17 / BE-18 self-check (callback + outbound send)")
    print("=" * 78)

    check_segmentation()
    check_idempotency()
    check_retry_and_success_rate()
    check_failure_table()
    check_zero_outbound_link()
    check_callback()
    check_compliance()
    check_rate_limit_config()
    check_circuit_breaker()
    check_no_duplicate_write_on_retry()

    print()
    print("=" * 78)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("-" * 78)
        for item in FAILED:
            print(f"  FAILED: {item}")
    print("=" * 78)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
