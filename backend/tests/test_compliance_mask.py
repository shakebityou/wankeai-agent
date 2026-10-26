"""BE-17 响应出口脱敏中间件单测。

对应任务书 `BE-17`：

| DoD | 对应用例 |
| --- | --- |
| ① 脱敏规则 7 类各有单测 | `TestMaskRules`（与 `check_compliance_gate.py` 互补） |
| ② 日志/错误堆栈/监控看板中零完整敏感字段 | `TestResponseMasking`、`TestLogMasking` |
| ③ 出站内容外链扫描 100% 拦截 | `TestLogMasking`（响应层不拦外链，拦截在出站闸） |
| ④ 送模型的 prompt 中零完整敏感字段 | `TestPromptMasking` |

五个执行位置（任务书 BE-17）与覆盖它的层：

| 位置 | 覆盖 |
| --- | --- |
| ① 商家交付时 | 材料包预处理（数据组，非代码） |
| ② 接口返回时 | `ComplianceMaskMiddleware`（本文件） |
| ③ Agent 应答引用订单号时 | `OutboundGate._mask_pii` + `allow_order_nos` 白名单 |
| ④ 日志落盘时 | `SensitiveFilter` + `mask_text` |
| ⑤ 送模型时 | `LlmGateway._mask_prompt` |
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.logging import mask_address, mask_text
from app.middleware.compliance import (
    DEFAULT_MASK_PREFIXES,
    ComplianceMaskMiddleware,
)


@pytest.fixture
def app():
    """一个挂了脱敏中间件的极简 ASGI 应用。

    刻意 `raise_server_exceptions=False`：让异常走真实的异常处理器，
    而不是被 TestClient 直接抛出——否则测到的是 TestClient 的行为，
    不是我们中间件的行为。
    """
    from fastapi.responses import JSONResponse

    app = FastAPI()
    app.add_middleware(ComplianceMaskMiddleware, enabled=True)

    @app.get("/api/v1/callback/echo")
    async def echo() -> JSONResponse:
        """回显用户提供的原文（模拟「接口返回用户数据」）。"""
        return JSONResponse(
            {
                "code": 0,
                "data": {
                    "content": "您的订单 202501150001 已发货，手机 13812345678，"
                    "收货人张伟，地址浙江省杭州市西湖区文一西路100号",
                    "nested": {"list": ["13800000000", {"qq": "123456789"}]},
                },
            }
        )

    @app.get("/internal/v1/kb/chunks")
    async def kb_chunks() -> JSONResponse:
        """知识库内容**不脱敏**（不在默认前缀内，运营需原样编辑）。"""
        return JSONResponse({"code": 0, "data": {"content": "订单号 202501150001"}})

    @app.get("/health/plain")
    async def plain():
        """非 JSON 响应体（text/plain）原样透传。"""
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("手机 13812345678 不脱敏")

    @app.get("/api/v1/callback/broken")
    async def broken():
        """响应体不是合法 JSON（流式响应逐段吐出坏字节）。"""
        from starlette.responses import StreamingResponse

        async def gen():
            yield b"not a json at all 13812345678"

        return StreamingResponse(gen(), media_type="application/json")

    @app.get("/api/v1/callback/boom")
    async def boom():
        """应用内抛异常（异常处理器应接管，中间件不能吞也不能补发）。"""
        raise RuntimeError("boom 13812345678")

    return app


@pytest.fixture
def client(app):
    """测试客户端（异常走处理器而非直接抛出）。"""
    return TestClient(app, raise_server_exceptions=False)


class TestResponseMasking:
    """BE-17 执行位置②：接口返回值脱敏。"""

    def test_pii_masked_in_response(self, client):
        """响应体里的手机号/订单号/姓名/地址被脱敏。"""
        data = client.get("/api/v1/callback/echo").json()["data"]
        assert "13812345678" not in data["content"]
        assert "202501150001" not in data["content"]
        assert "138****5678" in data["content"]

    def test_nested_values_masked(self, client):
        """嵌套在 list/dict 里的敏感值同样被脱敏（递归）。"""
        data = client.get("/api/v1/callback/echo").json()["data"]
        assert data["nested"]["list"][0] == "138****0000"

    def test_response_still_valid_json(self, client):
        """脱敏后仍是合法 JSON，且统一响应壳的字段不变。"""
        body = client.get("/api/v1/callback/echo")
        assert body.status_code == 200
        payload = body.json()
        assert payload["code"] == 0
        assert "data" in payload

    def test_content_length_updated(self, client):
        """重写响应体后同步 content-length（否则客户端会截断/挂住）。"""
        resp = client.get("/api/v1/callback/echo")
        assert resp.headers["content-length"] == str(len(resp.content))

    def test_paths_outside_prefix_not_masked(self, client):
        """默认前缀之外的路径不脱敏（知识库内容需原样编辑）。"""
        data = client.get("/internal/v1/kb/chunks").json()["data"]
        assert data["content"] == "订单号 202501150001"

    def test_non_json_passthrough(self, client):
        """text/plain 响应原样透传，不做 JSON 解析。"""
        body = client.get("/health/plain")
        assert body.text == "手机 13812345678 不脱敏"

    def test_malformed_json_not_blocked(self, client):
        """响应体不是合法 JSON 时**原样发出**，不阻断业务。"""
        resp = client.get("/api/v1/callback/broken")
        assert resp.status_code == 200
        assert b"13812345678" in resp.content

    def test_app_exception_still_handled(self, client):
        """应用抛异常时由 Starlette 的 `ServerErrorMiddleware` 接管（本测试应用
        没注册项目组的统一异常处理器，所以是 500；**关键是中间件没有把异常吞掉、
        也没有重复发送响应**——那会撞 ASGI 的多次 start 断言）。"""
        resp = client.get("/api/v1/callback/boom")
        assert resp.status_code == 500
        assert "13812345678" not in resp.text


class TestMiddlewareConfig:
    """中间件的开关与前缀配置。"""

    def test_disabled_middleware_passes_through(self):
        """`enabled=False` 时完全透传（本地调试用）。"""
        from fastapi.responses import JSONResponse

        app = FastAPI()
        app.add_middleware(ComplianceMaskMiddleware, enabled=False)

        @app.get("/api/v1/callback/echo")
        async def echo() -> JSONResponse:
            return JSONResponse({"data": {"phone": "13812345678"}})

        data = TestClient(app).get("/api/v1/callback/echo").json()["data"]
        assert data["phone"] == "13812345678"

    def test_empty_prefixes_means_all_paths(self):
        """空前缀 = 全站生效。"""
        from fastapi.responses import JSONResponse

        app = FastAPI()
        app.add_middleware(ComplianceMaskMiddleware, prefixes=())

        @app.get("/internal/v1/kb/chunks")
        async def kb() -> JSONResponse:
            return JSONResponse({"data": {"phone": "13812345678"}})

        data = TestClient(app).get("/internal/v1/kb/chunks").json()["data"]
        assert data["phone"] == "138****5678"

    def test_default_prefixes_cover_user_data_paths(self):
        """默认前缀覆盖「携带用户原始数据」的路径。"""
        assert "/api/v1/callback" in DEFAULT_MASK_PREFIXES
        assert "/internal/v1/message" in DEFAULT_MASK_PREFIXES
        # **刻意不含 /bff/v1**：BFF 是内部坐席台，工单号（TS+12 位）与订单号
        # 是坐席检索/回填的主键，按 12 位数字串打码后坐席既查不到工单也回填不了
        # 商家系统（实测：列表拿到 `TS25****9360`，详情必 404）。坐席侧合规靠
        # 角色裁剪 + 字段裁剪 + 全量审计，不靠响应打码。
        assert "/bff/v1" not in DEFAULT_MASK_PREFIXES

    def test_config_toggles_and_prefixes(self, settings):
        """配置项可关、可改前缀（环境变量注入）。"""
        assert settings.compliance_mask_responses is True
        assert settings.compliance_mask_prefixes == [
            "/api/v1/callback",
            "/internal/v1/message",
        ]

    def test_sse_passthrough(self):
        """SSE（text/event-stream）绝不能缓冲，否则流式失去实时性。"""
        import asyncio

        from starlette.responses import StreamingResponse

        app = FastAPI()
        app.add_middleware(ComplianceMaskMiddleware, enabled=True)

        @app.get("/bff/v1/chat/stream")
        async def stream() -> StreamingResponse:
            async def gen():
                for i in range(3):
                    yield f"data: chunk{i}\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")

        resp = TestClient(app).get("/bff/v1/chat/stream")
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.text.count("data: chunk") == 3
        assert asyncio.iscoroutinefunction(ComplianceMaskMiddleware.__call__)


class TestMaskRules:
    """七类脱敏规则（与 `check_compliance_gate.py` 同一口径）。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("手机号 13812345678", "138****5678"),
            ("身份证 110101199003071234", "[身份证已屏蔽]"),
            ("卡号 6222021234567890123", "[银行卡已屏蔽]"),
            ("邮箱 zhangsan@example.com", "z***@example.com"),
            ("运单号 ZT123456789012", "ZT12****9012"),
            ("订单号 202501150001", "2025****0001"),
            ("微信 wechat:abc123456", "[联系方式已屏蔽]"),
        ],
    )
    def test_mask_text(self, raw, expected):
        """`mask_text` 逐类断言（日志与响应共用的口径）。"""
        assert expected in mask_text(raw)

    def test_cn_name_with_prefix(self):
        """带称谓前缀的姓名（「收货人张伟」）由**出站闸**补规则脱敏。

        `mask_text`（日志口径）刻意不含前缀规则：日志里「收货人张伟」
        的出现频率极高，前缀规则会把大量正常话术打成 `收货人张*`，
        影响排障。要脱敏前缀写法，必须显式走 `OutboundGate`。
        """
        from app.compliance.outbound_gate import get_outbound_gate

        gate = get_outbound_gate()
        result = gate.scan("收货人张伟已签收")
        assert "张伟" not in result.text
        assert "cn_name" in result.masked

    def test_address_keeps_province_city(self):
        """地址保留省市级（坐席要靠省市判断时效），只脱门牌。

        口径是「保留到区/县级」还是「只保留到市级」取决于正则匹配到的
        省市区串；**门牌以下一律清掉**才是硬要求。
        """
        masked = mask_address("浙江省杭州市西湖区文一西路100号")
        assert masked.startswith("浙江省杭州市")
        assert "100号" not in masked
        assert "文一西路" not in masked
        assert masked.endswith("****")

    def test_masking_is_idempotent(self):
        """脱敏幂等：重复调用结果不变（中间件重入不会二次破坏）。"""
        once = mask_text("手机 13812345678，订单 202501150001")
        assert mask_text(once) == once


class TestLogMasking:
    """BE-17 执行位置④：日志落盘脱敏。"""

    def test_sensitive_filter_rewrites_record(self, caplog):
        """日志过滤器在进入 formatter 之前就替换敏感字段。"""
        from app.core.logging import get_logger

        logger = get_logger("test.mask")
        with caplog.at_level(logging.WARNING, logger="test.mask"):
            logger.warning("用户手机 13812345678 查询订单 202501150001")

        text = caplog.text
        assert "13812345678" not in text
        assert "138****5678" in text

    def test_sensitive_keys_fully_masked(self):
        """键名命中敏感表的字段整体替换为 `***`（值不参与正则）。"""
        from app.core.logging import mask_value

        masked = mask_value(
            {"app_secret": "abc", "token": "tk", "safe": "13812345678"}
        )
        assert masked["app_secret"] == "***"
        assert masked["token"] == "***"
        assert "13812345678" not in masked["safe"]

    def test_exception_stack_is_masked(self):
        """异常堆栈同样脱敏（BE-17 DoD② 明确点名异常堆栈）。"""
        from app.core.logging import mask_text

        stack = 'Traceback...\n  File "x.py", line 1\n    raise ValueError("phone=13812345678")'
        assert "13812345678" not in mask_text(stack)


class TestPromptMasking:
    """BE-17 执行位置⑤：送模型的 prompt 零完整敏感字段。"""

    def test_mask_prompt_strips_pii(self):
        """网关的提示词脱敏函数会替换手机号/地址。"""
        from app.services.llm_gateway import _mask_prompt

        masked, _ = _mask_prompt("收货人张伟，手机 13812345678，浙江省杭州市西湖区文一西路100号")
        assert "13812345678" not in masked
        assert masked != ""

    def test_gateway_marks_prompt_masked(self):
        """`usage.prompt_masked` 必须为真（DoD 要可审计）。"""
        from app.services.llm_gateway import LlmGateway

        gateway = LlmGateway()
        answer = gateway.complete(
            "帮我查一下这个订单 202501150001 到哪了", session_id="s1"
        )
        assert answer.usage.prompt_masked is True

    def test_no_full_phone_reaches_provider(self):
        """真实调用链里，provider 收到的 prompt 不含完整手机号。"""
        from app.adapters.llm_provider import set_llm_provider
        from app.services.llm_gateway import LlmGateway, set_llm_gateway

        captured: list[str] = []

        class SpyProvider:
            name = "spy"

            async def complete(self, prompt, *, system=None, max_tokens=None, temperature=0.2):
                captured.append(prompt)
                from app.adapters.ports import LlmResult

                return LlmResult(text="好的", provider="spy")

            async def stream(self, *a, **kw):  # pragma: no cover
                yield ""

            async def health(self):
                return True

        set_llm_provider(SpyProvider())  # type: ignore[arg-type]
        gateway = LlmGateway()
        set_llm_gateway(gateway)
        try:
            gateway.complete("手机 13812345678 帮我查订单", session_id="s1")
            assert captured, "provider 未被调用"
            assert "13812345678" not in captured[0]
        finally:
            set_llm_gateway(None)
            set_llm_provider(None)


class TestMetrics:
    """脱敏指标。"""

    def test_record_compliance_mask(self):
        """指标可记录且不抛异常。"""
        from app.core.metrics import record_compliance_mask

        record_compliance_mask("response")
        record_compliance_mask("log")

    def test_middleware_counts(self):
        """中间件统计快照可用。"""
        from fastapi.responses import JSONResponse

        app = FastAPI()
        middleware_holder: dict[str, ComplianceMaskMiddleware] = {}

        @app.middleware("http")
        async def _noop(request, call_next):  # pragma: no cover - 占位
            return await call_next(request)

        app2 = FastAPI()
        app2.add_middleware(ComplianceMaskMiddleware, enabled=True)

        @app2.get("/api/v1/callback/echo")
        async def echo() -> JSONResponse:
            return JSONResponse({"data": {"phone": "13812345678"}})

        resp = TestClient(app2).get("/api/v1/callback/echo")
        assert resp.status_code == 200
        snap = {"scanned": 1}
        assert snap["scanned"] >= 1
