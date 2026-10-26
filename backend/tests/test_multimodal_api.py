"""多模态预处理接口测试（`/internal/v1/multimodal/*`）。

关注三件事，都在 DoD 里写明：

1. **业务失败返回 200**（`ok=false` + 兜底话术）——渠道侧看到 5xx 会重推，
   而重推同一个坏文件永远不会成功；
2. **请求本身不合法返回 400**（base64 解不开、媒体过大、给了 VOICE 却没给媒体）；
3. **鉴权**：内部令牌缺失/错误一律 403。
"""

from __future__ import annotations

import base64

import pytest

import mm_fixtures as fx


@pytest.fixture
def api(api_client):
    """带内部令牌的客户端（`api_client` 来自 conftest）。"""
    return api_client


def _b64(data: bytes) -> str:
    """字节 → base64 字符串。"""
    return base64.b64encode(data).decode("ascii")


def _data(body: dict) -> dict:
    """取统一响应里的 `data`。"""
    payload = body
    assert payload["code"] == 0, payload
    return payload["data"]


class TestAuth:
    """鉴权。"""

    def test_missing_token_rejected(self, client) -> None:
        """无令牌 → 403。"""
        resp = client.post(
            "/internal/v1/multimodal/preprocess",
            json={"msg_type": "TEXT", "text": "你好"},
        )
        assert resp.status_code == 403

    def test_wrong_token_rejected(self, client) -> None:
        """错令牌 → 403。"""
        resp = client.post(
            "/internal/v1/multimodal/preprocess",
            json={"msg_type": "TEXT", "text": "你好"},
            headers={"X-Internal-Token": "wrong"},
        )
        assert resp.status_code == 403


class TestHealth:
    """健康检查。"""

    def test_health_lists_adapters(self, api) -> None:
        """返回三个适配器的实现名。"""
        data = _data(api.get("/internal/v1/multimodal/health").json())
        assert data["pipeline"] == "ready"
        assert data["asr"]
        assert data["ocr"]
        assert data["media_store"]


class TestText:
    """文本路径（必须和原来的纯文本行为完全一致）。"""

    def test_text_passthrough(self, api) -> None:
        """文本原样返回，来源 ORIGINAL。"""
        data = _data(
            api.post(
                "/internal/v1/multimodal/preprocess",
                json={"msg_type": "TEXT", "text": "这个玩偶多高呀"},
            ).json()
        )
        assert data["ok"] is True
        assert data["text"] == "这个玩偶多高呀"
        assert data["text_source"] == "ORIGINAL"
        assert data["confidence"] == 1.0

    def test_empty_text_is_business_failure_not_400(self, api) -> None:
        """空文本是业务失败（200 + ok=false），不是请求错误。"""
        resp = api.post(
            "/internal/v1/multimodal/preprocess",
            json={"msg_type": "TEXT", "text": "   "},
        )
        assert resp.status_code == 200
        data = _data(resp.json())
        assert data["ok"] is False
        assert "EMPTY_TEXT" in data["degraded"]


class TestVoice:
    """语音路径。"""

    def test_voice_with_base64(self, api) -> None:
        """base64 语音 → 归一文本（无 ASR 引擎时是兜底话术）。"""
        wav = fx.wav_bytes(seconds=0.3)
        data = _data(
            api.post(
                "/internal/v1/multimodal/preprocess",
                json={
                    "msg_type": "VOICE",
                    "media_base64": _b64(wav),
                    "filename": "a.wav",
                    "content_type": "audio/wav",
                },
            ).json()
        )
        # 本机没有 ASR 引擎 → 空文本 + 「语音没听清」；关键是**结构完整且 200**。
        assert data["msg_type"] == "VOICE"
        assert data["ok"] is False
        assert data["fallback_key"] == "VOICE_UNHEARD"
        assert data["fallback_text"]

    def test_voice_without_media_is_400(self, api) -> None:
        """声明是语音却没给媒体：调用方编程错误，必须 400。"""
        resp = api.post(
            "/internal/v1/multimodal/preprocess",
            json={"msg_type": "VOICE", "text": "我说的"},
        )
        assert resp.status_code == 400

    def test_bad_audio_is_business_failure(self, api) -> None:
        """坏音频 → 200 + 格式不支持话术（BE-10 ③ 不抛 500）。"""
        resp = api.post(
            "/internal/v1/multimodal/preprocess",
            json={
                "msg_type": "VOICE",
                "media_base64": _b64(b"\x00garbage-audio"),
                "filename": "a.amr",
            },
        )
        assert resp.status_code == 200
        data = _data(resp.json())
        assert data["ok"] is False
        assert any(d.startswith("ASR_TRANSCODE_FAILED") for d in data["degraded"])


class TestImage:
    """图片路径（BE-11 / BE-12）。"""

    def test_order_screenshot_extracts_number(self, api) -> None:
        """订单截图 → 抠出 12 位订单号并生成查单文本（BE-12 ①）。"""
        order_no = "626771284449"
        data = _data(
            api.post(
                "/internal/v1/multimodal/preprocess",
                json={
                    "msg_type": "IMAGE",
                    "media_base64": _b64(fx.order_shot(order_no, 0, 30)),
                    "filename": "order.png",
                },
            ).json()
        )
        assert data["ok"] is True
        assert data["route"] == "订单截图"
        assert data["order_no"] == order_no
        assert data["text"] == f"帮我查一下订单 {order_no}"
        assert data["text_source"] == "OCR"

    def test_product_image(self, api) -> None:
        """商品图 → 商品图路由。"""
        data = _data(
            api.post(
                "/internal/v1/multimodal/preprocess",
                json={
                    "msg_type": "IMAGE",
                    "media_base64": _b64(fx.product(0)),
                    "text": "这个还有货吗",
                },
            ).json()
        )
        assert data["ok"] is True
        assert data["route"] == "商品图"
        assert data["text"] == "这个还有货吗"

    def test_meme_gets_gentle_guide(self, api) -> None:
        """表情包不进编排，直接轻引导。"""
        data = _data(
            api.post(
                "/internal/v1/multimodal/preprocess",
                json={"msg_type": "IMAGE", "media_base64": _b64(fx.meme(0))},
            ).json()
        )
        assert data["ok"] is False
        assert data["fallback_key"] == "IMAGE_IRRELEVANT"

    def test_broken_image_is_business_failure(self, api) -> None:
        """坏图片 → 200 + 「图片没能加载出来」（BE-11 ③）。"""
        resp = api.post(
            "/internal/v1/multimodal/preprocess",
            json={
                "msg_type": "IMAGE",
                "media_base64": _b64(b"definitely not an image"),
                "filename": "a.png",
            },
        )
        assert resp.status_code == 200
        data = _data(resp.json())
        assert data["ok"] is False
        assert data["fallback_key"] == "IMAGE_UNREADABLE"

    def test_unsupported_format(self, api) -> None:
        """gif 不在白名单。"""
        data = _data(
            api.post(
                "/internal/v1/multimodal/preprocess",
                json={
                    "msg_type": "IMAGE",
                    "media_base64": _b64(fx.product(0)),
                    "filename": "a.gif",
                },
            ).json()
        )
        assert data["ok"] is False
        assert "IMAGE_FORMAT_UNSUPPORTED" in data["degraded"]

    def test_image_without_media_is_400(self, api) -> None:
        """声明是图片却没给媒体 → 400。"""
        resp = api.post(
            "/internal/v1/multimodal/preprocess", json={"msg_type": "IMAGE"}
        )
        assert resp.status_code == 400


class TestMediaDecoding:
    """媒体来源解析。"""

    def test_data_url_prefix_stripped(self, api) -> None:
        """前端常直接粘 `data:image/png;base64,...`，必须能解。"""
        png = fx.product(0)
        data = _data(
            api.post(
                "/internal/v1/multimodal/preprocess",
                json={
                    "msg_type": "IMAGE",
                    "media_base64": f"data:image/png;base64,{_b64(png)}",
                },
            ).json()
        )
        assert data["msg_type"] == "IMAGE"
        assert data["route"] == "商品图"

    def test_invalid_base64_is_400(self, api) -> None:
        """base64 解不开 → 400。"""
        resp = api.post(
            "/internal/v1/multimodal/preprocess",
            json={"msg_type": "IMAGE", "media_base64": "!!!!not-base64!!!!"},
        )
        assert resp.status_code == 400

    def test_oversize_base64_is_400(
        self, api, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超限的 base64 在解码前就被拒（不能先吃满内存再报错）。"""
        from app.api.v1 import multimodal as mm_api

        monkeypatch.setattr(mm_api, "MAX_BASE64_CHARS", 16)
        resp = api.post(
            "/internal/v1/multimodal/preprocess",
            json={"msg_type": "IMAGE", "media_base64": "A" * 100},
        )
        assert resp.status_code == 400

    def test_file_url_reads_local_file(self, api, tmp_path) -> None:
        """`file://` 地址可读（渠道回调已落盘时用）。"""
        path = tmp_path / "order.png"
        path.write_bytes(fx.order_shot("626771284449", 0, 30))
        data = _data(
            api.post(
                "/internal/v1/multimodal/preprocess",
                json={"msg_type": "IMAGE", "media_url": f"file://{path.as_posix()}"},
            ).json()
        )
        assert data["ok"] is True
        assert data["order_no"] == "626771284449"

    def test_file_url_with_triple_slash(self, api, tmp_path) -> None:
        """标准 `file:///D:/...` 写法也要能读（Windows 盘符路径）。"""
        path = tmp_path / "order.png"
        path.write_bytes(fx.order_shot("626771284449", 0, 30))
        data = _data(
            api.post(
                "/internal/v1/multimodal/preprocess",
                json={"msg_type": "IMAGE", "media_url": f"file:///{path.as_posix()}"},
            ).json()
        )
        assert data["ok"] is True
        assert data["order_no"] == "626771284449"

    def test_missing_file_is_400(self, api, tmp_path) -> None:
        """文件不存在 → 400（给了但读不到，属请求错误）。"""
        missing = tmp_path / "nope.png"
        resp = api.post(
            "/internal/v1/multimodal/preprocess",
            json={"msg_type": "IMAGE", "media_url": f"file://{missing.as_posix()}"},
        )
        assert resp.status_code == 400

    def test_unsupported_scheme_is_400(self, api) -> None:
        """不支持的协议 → 400。"""
        resp = api.post(
            "/internal/v1/multimodal/preprocess",
            json={"msg_type": "IMAGE", "media_url": "ftp://example.com/a.png"},
        )
        assert resp.status_code == 400


class TestResponseShape:
    """响应字段形状（供 BFF 与前端对接）。"""

    def test_all_contract_fields_present(self, api) -> None:
        """契约字段一个都不能少。"""
        data = _data(
            api.post(
                "/internal/v1/multimodal/preprocess",
                json={"msg_type": "TEXT", "text": "你好"},
            ).json()
        )
        assert set(data) == {
            "ok",
            "msg_type",
            "text",
            "text_source",
            "confidence",
            "route",
            "order_no",
            "degraded",
            "latency_ms",
            "fallback_key",
            "fallback_text",
            "media_url",
            "attachments",
            "extra",
        }

    def test_latency_reported(self, api) -> None:
        """耗时字段用于排障，必须有 total。"""
        data = _data(
            api.post(
                "/internal/v1/multimodal/preprocess",
                json={"msg_type": "TEXT", "text": "你好"},
            ).json()
        )
        assert "total" in data["latency_ms"]
        assert isinstance(data["latency_ms"]["total"], int)

    def test_save_media_false_skips_store(self, api) -> None:
        """`save_media=false` 时不落盘（批量回放历史消息时用）。"""
        data = _data(
            api.post(
                "/internal/v1/multimodal/preprocess",
                json={
                    "msg_type": "IMAGE",
                    "media_base64": _b64(fx.product(0)),
                    "save_media": False,
                },
            ).json()
        )
        assert data["media_url"] == ""
