"""BE-02 消息发送服务单测。

覆盖任务书 `BE-02` 的四条 DoD：

| DoD | 对应用例 |
| --- | --- |
| ① 成功率 ≥ 99.9%（含重试） | `TestRetryAndFailure`、`TestStats` |
| ② 同 `msg_id` 提交 3 次只发 1 条 | `TestIdempotency` |
| ③ 出站内容零外链 | `TestCompliance` |
| ④ 长消息自动分段（单条 ≤ 120 字） | `TestSplit` |

另外覆盖：禁止的 `msg_type`、失败落表与重发、路由层契约。

**失败表用一个临时 SQLite 引擎**（`set_failed_table_engine`），绝不连开发库
——交接文档 4.6 记过这个坑：连开发库会出现「跑过种子就红」的脏失败。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text

from app.adapters.merchant_message import (
    MerchantChannelError,
    MessageChannelPort,
    MockMerchantMessageChannel,
    parse_retry_after,
)
from app.compliance.outbound_gate import OutboundGate
from app.core.errors import MerchantErrorCode
from app.core.idempotency import IdempotencyStore
from app.services import message_send as ms
from app.services.message_send import (
    MAX_SEGMENTS,
    MessageSendService,
    OutboundMessage,
    split_message,
)

#: 一长段真实口径的客服回复（> 240 字，必然分段）
LONG_REPLY = (
    "这款奶油小熊毛绒玩偶采用 A 类婴幼儿亲肤材质，通过国家纺织品安全检测，"
    "适合 3 岁以上小朋友抱着睡觉。"
    "日常清洗请用 30 度以下温水加中性洗涤剂手洗，轻揉后不要拧干，平铺阴干即可，避免暴晒导致面料变硬。"
    "收纳时建议放在干燥通风处，远离火源与尖锐物。"
    "收到后如果发现有开线、掉毛等质量问题，签收后 7 天内可以申请退换，运费由我们承担哦。"
)


@pytest.fixture
def engine(tmp_path):
    """临时 SQLite 引擎（失败表用）。"""
    from app.db.engine import make_engine

    eng = make_engine(f"sqlite+pysqlite:///{(tmp_path / 'send.db').as_posix()}")
    ms.set_failed_table_engine(eng)
    yield eng
    ms.set_failed_table_engine(None)
    eng.dispose()


@pytest.fixture
def port() -> MockMerchantMessageChannel:
    """确定性 Mock 通道。"""
    return MockMerchantMessageChannel()


@pytest.fixture
def service(
    port: MockMerchantMessageChannel, cache, engine
) -> MessageSendService:
    """组装一个使用内存缓存 + 零延迟 sleep 的服务实例。"""
    return MessageSendService(
        channel_port=port,
        gate=OutboundGate(enabled=True),
        idempotency=IdempotencyStore(cache=cache, ttl=3600),
        sleep=lambda _s: None,
    )


def _message(**overrides: Any) -> OutboundMessage:
    """构造一条默认消息。"""
    kwargs: dict[str, Any] = {
        "msg_id": "msg_unit_1",
        "session_id": "sess_1",
        "buyer_id": "buyer_1",
        "channel": "TAOBAO",
        "content": "奶油小熊 20cm 现货，付款后 48 小时内发出～",
    }
    kwargs.update(overrides)
    return OutboundMessage(**kwargs)


# ---------------------------------------------------------------------------
# ① 分段（DoD④）
# ---------------------------------------------------------------------------


class TestSplit:
    """长消息自动分段：单条 ≤ 120 字、不丢字、不切坏数字。"""

    def test_short_text_single_segment(self):
        """≤ 120 字的文本不分段。"""
        assert split_message("奶油小熊现货哦～") == ["奶油小熊现货哦～"]

    def test_empty_returns_empty_list(self):
        """空文本返回空列表（不是 ['']，否则会发一条空消息）。"""
        assert split_message("") == []
        assert split_message(None) == []
        assert split_message("   ") == []

    def test_every_segment_within_limit(self):
        """每段都不超过 120 字。"""
        segments = split_message(LONG_REPLY)
        assert len(segments) > 1
        assert all(len(s) <= 120 for s in segments), [len(s) for s in segments]

    def test_no_character_dropped(self):
        """分段不丢任何字符（拼回去等于原文）。"""
        segments = split_message(LONG_REPLY)
        assert "".join(segments) == LONG_REPLY

    def test_segments_end_with_punctuation(self):
        """段内以句末标点收尾，读起来是自然断句。"""
        segments = split_message(LONG_REPLY)
        assert all(s[-1] in "。！？!?；;，,）)" or len(s) == 120 for s in segments)

    def test_does_not_cut_number_in_half(self):
        """绝不在数字中间切：订单号 / 金额 / 规格不能被拆开。"""
        text = "订单号" + "202501150001" + "已发货，" * 40
        for seg in split_message(text):
            # 12 位订单号必须完整出现在某一段里
            assert not seg.endswith("2025") and not seg.startswith("01150001")
        all_text = "".join(split_message(text))
        assert "202501150001" in all_text

    def test_hard_text_without_punctuation_still_bounded(self):
        """没有标点的超长文本也要硬切（但每段仍 ≤ 上限）。"""
        segments = split_message("x" * 300)
        assert [len(s) for s in segments] == [120, 120, 60]

    def test_long_text_not_truncated_and_flagged_overflow(self, service, port):
        """远超 4 段的内容**照发不误**（不截断），只标 overflow 让坐席提示运营。"""
        outcome = service.send(_message(msg_id="ovf_1", content="奶油小熊现货。" * 120))
        assert outcome.ok is True
        assert outcome.overflow is True
        assert len(port.sent) > MAX_SEGMENTS
        joined = "".join(s["content"] for s in port.sent)
        assert joined == "奶油小熊现货。" * 120

    def test_custom_limit(self):
        """可指定每段上限（配置项 `outbound_max_chars_per_segment`）。"""
        segments = split_message("一二三四五六七八九十", max_chars=4)
        assert all(len(s) <= 4 for s in segments)
        assert "".join(segments) == "一二三四五六七八九十"


# ---------------------------------------------------------------------------
# ② 幂等（DoD②）
# ---------------------------------------------------------------------------


class TestIdempotency:
    """同 `msg_id` 提交 3 次只发 1 条。"""

    def test_three_submissions_send_once(self, service, port):
        """DoD②：同一 msg_id 提交 3 次，通道只收到 1 条。"""
        message = _message()
        results = [service.send(message) for _ in range(3)]
        assert len(port.sent) == 1
        assert results[0].ok is True and results[0].duplicate is False
        assert results[1].duplicate is True and results[1].ok is True
        assert results[2].duplicate is True and results[2].ok is True

    def test_duplicate_reports_original_segments(self, service):
        """重复提交返回的是首次结果（含分段信息），而不是空。"""
        first = service.send(_message(content=LONG_REPLY))
        again = service.send(_message(content=LONG_REPLY))
        assert again.duplicate is True
        assert again.ok == first.ok

    def test_different_msg_id_sends_again(self, service, port):
        """不同 msg_id 是不同的消息，必须各发一条。"""
        service.send(_message(msg_id="m1"))
        service.send(_message(msg_id="m2"))
        assert len(port.sent) == 2

    def test_concurrent_submission_marked_in_progress(self, service, cache):
        """并发提交同一 msg_id：首次未完成时，第二次明确报告「处理中」。"""
        import asyncio

        message = _message(msg_id="busy_1")
        key = service.idempotency_key("busy_1", message.channel)
        assert asyncio.run(service.idempotency.acquire(key)) is True
        outcome = service.send(message)
        assert outcome.duplicate is True
        assert outcome.in_progress is True
        assert outcome.ok is False

    def test_idempotency_key_shape(self):
        """幂等键口径：`outbound:<CHANNEL>:<msg_id>`（渠道隔离 + 可前缀排查）。"""
        assert (
            MessageSendService.idempotency_key("abc", "TAOBAO")
            == "outbound:TAOBAO:abc"
        )
        assert MessageSendService.idempotency_key("abc") == "outbound:abc"

    def test_same_msg_id_on_two_channels_are_two_messages(self, service, port):
        """不同渠道的同一 msg_id 是两条消息（平台 msg_id 空间独立）。"""
        service.send(_message(msg_id="shared_1", channel="TAOBAO"))
        service.send(_message(msg_id="shared_1", channel="PDD"))
        assert len(port.sent) == 2


# ---------------------------------------------------------------------------
# ③ 合规（DoD③）
# ---------------------------------------------------------------------------


class TestCompliance:
    """出站内容零外链（正则扫描命中即失败）。"""

    @pytest.mark.parametrize(
        "text",
        [
            "详情见 https://example.com/item",
            "去 taobao.com 看看",
            "点击链接领取优惠券",
            "请扫一扫二维码",
        ],
    )
    def test_link_is_blocked_and_replaced(self, service, port, text):
        """外链 / 跳转话术被整条替换为兜底话术，**绝不发出**。"""
        from app.compliance.outbound_gate import FALLBACK_REPLY

        outcome = service.send(_message(content=text))
        assert outcome.blocked is True
        assert len(port.sent) == 1
        assert port.sent[0]["content"] == FALLBACK_REPLY

    def test_all_segments_are_link_free(self, service, port):
        """长文本分段后逐段复检，发出的每一段都零外链。"""
        from app.compliance.outbound_gate import assert_no_outbound_link

        service.send(_message(content=LONG_REPLY))
        assert len(port.sent) > 1
        for sent in port.sent:
            assert len(sent["content"]) <= 120
            assert_no_outbound_link(sent["content"])

    def test_pii_masked_in_outgoing_text(self, service, port):
        """出站文本中的手机号 / 订单号就地脱敏（不拦截）。"""
        outcome = service.send(_message(content="您的订单 202501150001 已发货，手机号 13812345678"))
        assert outcome.blocked is False
        content = port.sent[0]["content"]
        assert "13812345678" not in content
        assert "2025****0001" in content

    def test_verified_order_no_whitelisted(self, service, port):
        """本会话已核验的订单号原样出现在回复里（不脱敏）。

        说明：`OutboundGate` 自身的白名单参数已由 `check_compliance_gate.py`
        覆盖；这里断言的是**发送服务把白名单一路传到了闸上**——服务层漏传
        是这个参数最典型的失效方式。
        """
        from app.compliance.outbound_gate import OutboundGate as _Gate

        service._gate = _Gate(enabled=True)  # noqa: SLF001 - 白名单直测闸
        text = service.gate.enforce(
            "您的订单 202501150001 目前状态：已发货",
            allow_order_nos={"202501150001"},
        )
        assert "202501150001" in text

    def test_unverified_order_no_is_masked(self, service, port):
        """未核验的订单号必须脱敏（BE-17 执行位置③）。"""
        service.send(_message(content="您的订单 202601010001 已发货"))
        assert "2026****0001" in port.sent[0]["content"]

    def test_cards_and_goods_rejected(self, service):
        """CARD / GOODS 本期不接入（商家 0.8 节），直接拒绝而不是降级。"""
        from app.core.enums import OutboundMsgType

        with pytest.raises(ValueError, match="本期不接入"):
            service.send(_message(msg_type=OutboundMsgType.CARD))
        with pytest.raises(ValueError, match="本期不接入"):
            service.send(_message(msg_type=OutboundMsgType.GOODS))

    def test_empty_text_rejected(self, service):
        """空文本消息直接拒绝（发一条空消息比不发更糟）。"""
        with pytest.raises(ValueError, match="不得为空"):
            service.send(_message(content="   "))

    def test_only_text_and_image_allowed(self):
        """允许类型只包含 TEXT 与 IMAGE。"""
        from app.core.enums import OutboundMsgType

        assert ms.ALLOWED_OUTBOUND_TYPES == {
            OutboundMsgType.TEXT,
            OutboundMsgType.IMAGE,
        }


# ---------------------------------------------------------------------------
# ④ 重试、失败落表、成功率（DoD①）
# ---------------------------------------------------------------------------


class TestRetryAndFailure:
    """429/5xx 退避重试 2 次；仍失败落表告警。"""

    def test_retry_succeeds_on_second_attempt(self, cache, engine):
        """第 1 次 500、第 2 次成功：最终发出且 attempts=2。"""
        port = MockMerchantMessageChannel(fail_times=1)
        service = MessageSendService(
            channel_port=port,
            gate=OutboundGate(enabled=True),
            idempotency=IdempotencyStore(cache=cache),
            sleep=lambda _s: None,
        )
        outcome = service.send(_message(msg_id="retry_1"))
        assert outcome.ok is True
        assert outcome.segments[0].attempts == 2
        assert len(outcome.segments[0].retries) == 1

    def test_retry_exhausted_records_failure(self, cache, engine):
        """一直失败：重试 2 次后放弃，落失败表并返回 record_id。"""
        port = MockMerchantMessageChannel(always_fail=True)
        service = MessageSendService(
            channel_port=port,
            gate=OutboundGate(enabled=True),
            idempotency=IdempotencyStore(cache=cache),
            sleep=lambda _s: None,
        )
        outcome = service.send(_message(msg_id="fail_1"))
        assert outcome.ok is False
        assert outcome.segments[0].attempts == 3  # 首次 + 2 次重试
        assert outcome.failed_record_id
        rows = service.failed_store.list_failed(session_id="sess_1")
        assert len(rows) == 1
        assert rows[0]["msg_id"] == "fail_1"

    def test_rate_limit_honours_retry_after(self, cache, engine):
        """429 按商家 `Retry-After` 退避而不是指数退避。"""
        port = MockMerchantMessageChannel(
            fail_times=1,
            fail_code=MerchantErrorCode.RATE_LIMITED,
            retry_after=2.0,
        )
        service = MessageSendService(
            channel_port=port,
            gate=OutboundGate(enabled=True),
            idempotency=IdempotencyStore(cache=cache),
            sleep=lambda _s: None,
        )
        outcome = service.send(_message(msg_id="rl_1"))
        assert outcome.ok is True
        assert outcome.segments[0].retries[0]["delay"] == pytest.approx(2.0)

    def test_retry_after_capped_by_config(self, cache, engine):
        """商家给离谱的 Retry-After 时按配置上限截断，不挂死请求线程。"""
        port = MockMerchantMessageChannel(
            fail_times=1,
            fail_code=MerchantErrorCode.RATE_LIMITED,
            retry_after=3600.0,
        )
        service = MessageSendService(
            channel_port=port,
            gate=OutboundGate(enabled=True),
            idempotency=IdempotencyStore(cache=cache),
            sleep=lambda _s: None,
        )
        outcome = service.send(_message(msg_id="rl_cap"))
        assert outcome.segments[0].retries[0]["delay"] <= 5.0

    def test_total_failure_releases_idempotency(self, cache, engine):
        """全部失败后释放幂等占位：上游可用同一 msg_id 重试。"""
        port = MockMerchantMessageChannel(always_fail=True)
        service = MessageSendService(
            channel_port=port,
            gate=OutboundGate(enabled=True),
            idempotency=IdempotencyStore(cache=cache),
            sleep=lambda _s: None,
        )
        first = service.send(_message(msg_id="fail_2"))
        assert first.ok is False
        port.always_fail = False
        second = service.send(_message(msg_id="fail_2"))
        assert second.duplicate is False
        assert second.ok is True

    def test_resend_marks_failed_record_resolved(self, cache, engine):
        """重发失败消息：用新 msg_id 发出，原记录标记已处理。"""
        port = MockMerchantMessageChannel(always_fail=True)
        service = MessageSendService(
            channel_port=port,
            gate=OutboundGate(enabled=True),
            idempotency=IdempotencyStore(cache=cache),
            sleep=lambda _s: None,
        )
        outcome = service.send(_message(msg_id="fail_3"))
        record_id = outcome.failed_record_id
        assert record_id

        port.always_fail = False
        resent = service.resend(record_id)
        assert resent.ok is True
        assert resent.msg_id != "fail_3"
        assert service.failed_store.list_failed(session_id="sess_1") == []

    def test_empty_content_rejected_before_send(self):
        """空内容在发送前就被拒绝，不产生任何通道调用。"""
        port = MockMerchantMessageChannel()
        service = MessageSendService(channel_port=port, gate=OutboundGate())
        with pytest.raises(ValueError):
            service.send(_message(content=""))
        assert port.sent == []


class TestStats:
    """成功率统计（DoD① 的验收口径）。"""

    def test_rates_after_mixed_traffic(self, cache, engine):
        """一次成功 + 一次重试成功 + 一次失败 → 三个口径各自正确。"""
        port = MockMerchantMessageChannel()
        service = MessageSendService(
            channel_port=port,
            gate=OutboundGate(enabled=True),
            idempotency=IdempotencyStore(cache=cache),
            sleep=lambda _s: None,
        )
        service.send(_message(msg_id="ok_1"))  # 1 次尝试，成功
        port.fail_times = port._calls + 1  # noqa: SLF001 - Mock 内部计数
        service.send(_message(msg_id="retry_ok"))  # 2 次尝试（失败 1 + 成功 1）
        port.always_fail = True
        service.send(_message(msg_id="fail"))  # 3 次尝试全失败

        stats = service.stats()
        assert stats["submitted"] == 3
        assert stats["delivered"] == 2
        assert stats["message_success_rate"] == pytest.approx(2 / 3, rel=1e-4)
        # 投递成功率：6 次尝试（1 + 2 + 3）里 2 次成功
        assert stats["attempts"] == 6
        assert stats["sent"] == 2
        assert stats["delivery_success_rate"] == pytest.approx(2 / 6)
        assert stats["first_try_ok"] == 1
        assert stats["first_try_rate"] == pytest.approx(1 / 3)

    def test_perfect_run_meets_999_threshold(self, service):
        """零故障时消息成功率 100%，满足 ≥ 99.9% 门槛。"""
        for i in range(20):
            service.send(_message(msg_id=f"bulk_{i}"))
        stats = service.stats()
        assert stats["message_success_rate"] >= 0.999
        assert stats["first_try_rate"] == 1.0

    def test_duplicate_not_counted_as_submitted(self, service):
        """重复提交不计入「提交数」，否则成功率会被稀释成假数字。"""
        service.send(_message(msg_id="dup_1"))
        service.send(_message(msg_id="dup_1"))
        stats = service.stats()
        assert stats["submitted"] == 1
        assert stats["duplicates"] == 1
        assert stats["message_success_rate"] == 1.0

    def test_initial_stats_are_one(self):
        """零流量时三个比率都返回 1.0（不是除零错误）。"""
        stats = MessageSendService(gate=OutboundGate()).stats()
        assert stats["message_success_rate"] == 1.0
        assert stats["delivery_success_rate"] == 1.0


class TestMultiSegmentSend:
    """多段发送：每段一个 platform_msg_id，任一段失败整体不成功。"""

    def test_each_segment_has_own_id(self, service, port):
        """多段发送用 `msg_id#2`、`msg_id#3` 等分段级 ID（平台幂等键）。"""
        outcome = service.send(_message(msg_id="multi_1", content=LONG_REPLY))
        assert outcome.ok is True
        ids = [s["msg_id"] for s in port.sent]
        assert ids[0] == "multi_1"
        assert ids[1] == "multi_1#2"

    def test_partial_failure_keeps_idempotency(self, cache, engine):
        """部分段成功时**不释放**幂等占位（重放会让已发的段重复出现）。"""
        port = MockMerchantMessageChannel()
        service = MessageSendService(
            channel_port=port,
            gate=OutboundGate(enabled=True),
            idempotency=IdempotencyStore(cache=cache),
            sleep=lambda _s: None,
        )
        # 让第 2 段的 segment_id 命中定向失败
        original_send = port.send

        def flaky(payload: dict[str, Any]):
            if payload["msg_id"].endswith("#2"):
                raise MerchantChannelError(
                    "boom", code=MerchantErrorCode.SERVICE_UNAVAILABLE, retryable=True
                )
            return original_send(payload)

        port.send = flaky  # type: ignore[method-assign]
        outcome = service.send(_message(msg_id="part_1", content=LONG_REPLY))
        assert outcome.ok is False
        assert outcome.sent_count == 1
        again = service.send(_message(msg_id="part_1", content=LONG_REPLY))
        assert again.duplicate is True
        assert again.ok is False

    def test_unexpected_channel_exception_lands_in_failed_table(self, cache, engine):
        """通道抛非 `MerchantChannelError` 的意外异常也要落表（否则消息凭空消失）。"""
        port = MockMerchantMessageChannel()

        def boom(_payload: dict[str, Any]):
            raise RuntimeError("driver exploded")

        port.send = boom  # type: ignore[method-assign]
        service = MessageSendService(
            channel_port=port,
            gate=OutboundGate(enabled=True),
            idempotency=IdempotencyStore(cache=cache),
            sleep=lambda _s: None,
        )
        outcome = service.send(_message(msg_id="boom_1"))
        assert outcome.ok is False
        assert outcome.failed_record_id


class TestRetryAfterParsing:
    """`Retry-After` 响应头解析（秒数与 HTTP 日期两种写法）。"""

    def test_seconds(self):
        assert parse_retry_after("1") == 1.0
        assert parse_retry_after(" 2.5 ") == 2.5

    def test_http_date(self):
        from email.utils import format_datetime
        from datetime import datetime, timedelta, timezone

        future = datetime.now(timezone.utc) + timedelta(seconds=30)
        value = format_datetime(future, usegmt=True)
        parsed = parse_retry_after(value)
        assert parsed is not None
        assert 25 <= parsed <= 35

    def test_invalid_returns_none(self):
        assert parse_retry_after(None) is None
        assert parse_retry_after("soon") is None


# ---------------------------------------------------------------------------
# 路由层
# ---------------------------------------------------------------------------


class TestSendRoute:
    """`/internal/v1/message/*` 契约。"""

    @pytest.fixture(autouse=True)
    def _wire(self, cache, engine):
        """把全局发送服务换成使用临时引擎与 Mock 通道的实例。"""
        port = MockMerchantMessageChannel()
        service = MessageSendService(
            channel_port=port,
            gate=OutboundGate(enabled=True),
            idempotency=IdempotencyStore(cache=cache),
            sleep=lambda _s: None,
        )
        ms.set_message_send_service(service)
        self.port = port
        yield
        ms.set_message_send_service(None)

    def test_send_ok(self, api_client):
        """正常发送返回统一响应包装，成功率为 1。"""
        resp = api_client.post(
            "/internal/v1/message/send",
            json={
                "msg_id": "route_1",
                "session_id": "s1",
                "buyer_id": "b1",
                "channel": "TAOBAO",
                "content": "奶油小熊现货哦～",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["ok"] is True
        assert body["data"]["sent_count"] == 1

    def test_send_requires_internal_token(self, client):
        """无内部令牌一律 403（内部接口不对外暴露）。"""
        resp = client.post(
            "/internal/v1/message/send",
            json={
                "session_id": "s1",
                "buyer_id": "b1",
                "channel": "TAOBAO",
                "content": "hi",
            },
        )
        assert resp.status_code == 403

    def test_card_type_returns_400(self, api_client):
        """禁止的 msg_type 返回 400 而不是静默失败。"""
        resp = api_client.post(
            "/internal/v1/message/send",
            json={
                "session_id": "s1",
                "buyer_id": "b1",
                "channel": "TAOBAO",
                "content": "x",
                "msg_type": "CARD",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == 10001

    def test_sender_user_rejected_by_schema(self, api_client):
        """出站消息 sender 不能是 USER。

        本项目的统一异常处理把 Pydantic 校验失败映射成 **400 / 码 10001**
        （契约 C 2.5.5），不是 FastAPI 默认的 422 —— 前端拦截器依赖这个口径。
        """
        resp = api_client.post(
            "/internal/v1/message/send",
            json={
                "session_id": "s1",
                "buyer_id": "b1",
                "channel": "TAOBAO",
                "content": "x",
                "sender": "USER",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == 10001

    def test_stats_and_failures_routes(self, api_client):
        """统计与失败列表路由可用（失败列表为空时返回 total=0）。"""
        api_client.post(
            "/internal/v1/message/send",
            json={
                "session_id": "s2",
                "buyer_id": "b2",
                "channel": "PDD",
                "content": "在的哦～",
            },
        )
        stats = api_client.get("/internal/v1/message/stats").json()["data"]
        assert stats["submitted"] == 1
        assert stats["message_success_rate"] == 1.0

        failed = api_client.get("/internal/v1/message/failures?session_id=s2").json()["data"]
        assert failed["total"] == 0

    def test_resend_missing_record_returns_400(self, api_client):
        """重发不存在的记录返回 400（不泄露记录是否存在以外的信息）。"""
        resp = api_client.post("/internal/v1/message/failures/nope/resend")
        assert resp.status_code == 400


class TestServiceSingleton:
    """全局单例与注入点。"""

    def test_set_and_reset(self, cache):
        service = MessageSendService(
            channel_port=MockMerchantMessageChannel(),
            gate=OutboundGate(enabled=True),
            idempotency=IdempotencyStore(cache=cache),
        )
        ms.set_message_send_service(service)
        assert ms.get_message_send_service() is service
        ms.set_message_send_service(None)
        assert ms.get_message_send_service() is not service

    def test_default_channel_is_mock(self, monkeypatch):
        """本机默认走 Mock 通道（商家测试环境不可达，见交接 9.2）。"""
        from app.adapters import merchant_message

        merchant_message.set_message_channel(None)
        assert isinstance(
            merchant_message.get_message_channel(), MockMerchantMessageChannel
        )

    def test_outbound_message_from_reply(self):
        """`outbound_message_from_reply` 复用入站 msg_id（防重复回复）。"""
        message = ms.outbound_message_from_reply(
            session_id="s1",
            buyer_id="b1",
            channel="TMALL",
            content="好的",
            msg_id="in_1",
        )
        assert message.msg_id == "in_1"
        assert message.channel.value == "TMALL"

    def test_new_msg_id_unique(self):
        """本地生成的 msg_id 必须唯一（幂等键冲突会让第二条消息发不出去）。"""
        assert ms.new_msg_id() != ms.new_msg_id()
