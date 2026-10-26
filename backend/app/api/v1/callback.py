"""渠道回调路由（契约 2.3.4，BE-01）。

## 为什么这个路由刻意「什么都不做」

契约 2.3.4 要求「3 秒内返回 200，只入队」。所以本文件里的处理函数
**只有四行实质代码**：验签 → 解析 → 幂等 → 入队。编排（意图识别、
RAG 检索、LLM 生成）全部在队列消费侧，绝不能出现在这里。

反例的后果很具体：一次 RAG 检索 P95 200ms、LLM 生成 1–3 秒，串起来
轻松破 3 秒，渠道侧超时后会重推，形成「重推 → 更慢 → 再重推」的雪崩。

## 为什么用 `await request.body()` 而不是声明 Pydantic 模型

验签必须对**原始字节**做 SHA-256。一旦让 FastAPI 用 Pydantic 解析
请求体，我们拿到的就是重新序列化后的对象，字段顺序、空格、转义都会
变，算出来的摘要与商家侧不一致，验签 100% 失败。这是最容易被写错的
一个点，所以本文件所有回调端点都用 `Request` + `await body()`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.enums import Channel
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/callback", tags=["callback"])


async def _handle(channel: str, request: Request) -> JSONResponse:
    """五个渠道共用的处理逻辑。

    Args:
        channel: 渠道值。
        request: 原始请求。

    Returns:
        **永远 200**（契约 2.3.4），响应体是渠道要求的应答结构。
    """
    from app.services.callback_gateway import get_callback_gateway
    from app.services.channels import get_adapter

    body = await request.body()
    gateway = get_callback_gateway()

    # 回调路由是**纯异步**的，这里同步调用网关（内部用 asyncbridge 驱动）
    outcome = gateway.handle_callback(
        channel=channel,
        headers=request.headers,
        body=body,
    )

    try:
        ack = get_adapter(channel).ack(body)
    except Exception:  # noqa: BLE001 - 应答体构造失败不能让回调返 5xx
        ack = {"errcode": 0, "errmsg": "ok", "success": True}

    payload: dict[str, Any] = dict(ack)
    # 附加我们自己的受理摘要，便于联调阶段核对（渠道侧忽略未知字段）
    payload.setdefault("received", outcome.received)
    payload.setdefault("enqueued", outcome.enqueued)
    payload.setdefault("trace_id", outcome.trace_id)

    return JSONResponse(status_code=200, content=payload)


@router.post("/taobao", summary="淘宝千牛回调")
async def taobao_callback(request: Request) -> JSONResponse:
    """淘宝千牛消息回调。"""
    return await _handle(Channel.TAOBAO.value, request)


@router.post("/tmall", summary="天猫回调")
async def tmall_callback(request: Request) -> JSONResponse:
    """天猫消息回调。"""
    return await _handle(Channel.TMALL.value, request)


@router.post("/pdd", summary="拼多多回调")
async def pdd_callback(request: Request) -> JSONResponse:
    """拼多多消息回调（长连接下发）。"""
    return await _handle(Channel.PDD.value, request)


@router.post("/douyin", summary="抖音回调")
async def douyin_callback(request: Request) -> JSONResponse:
    """抖音 IM 消息回调。"""
    return await _handle(Channel.DOUYIN.value, request)


@router.post("/wechat", summary="企微客服回调")
async def wechat_callback(request: Request) -> JSONResponse:
    """企业微信客服消息回调（JSON 或 XML）。"""
    return await _handle(Channel.WECHAT.value, request)


@router.get("/wechat", summary="企微回调 URL 校验")
async def wechat_verify(echostr: str = "") -> JSONResponse:
    """企业微信配置回调地址时的 URL 有效性校验。

    企微要求原样返回 `echostr` 明文（不是 JSON）。这里必须用
    `Response(media_type="text/plain")`，包装成 `{"code":0,...}` 会校验失败。
    """
    from fastapi import Response

    if not echostr:
        return JSONResponse(status_code=400, content={"error": "missing echostr"})
    return Response(content=echostr, media_type="text/plain")


__all__ = ["router"]
