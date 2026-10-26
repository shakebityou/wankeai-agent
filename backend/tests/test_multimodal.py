"""多模态预处理单元测试（BE-10 / BE-11 / BE-12）。

覆盖四层，从底往上：

1. **转码 / ASR / OCR / 订单号**：纯函数与适配器，输入字节输出结构化结果；
2. **图片校验与五路分流**：真图（PIL 现场合成）过真实分类器，不 mock；
3. **流水线**：三条路径归一成同一个 `NormalizedMessage`；
4. **话术**：所有兜底话术零外链（HC-1）。

刻意**不 mock 分类器与 OCR**：这两块是本地启发式实现，mock 掉等于什么都没测
（「五路分流准确率 100%」这类 DoD 只能靠真图跑出来）。真依赖不可用的部分
（无 ffmpeg、无 silk 解码器）用 `pytest.mark.skipif` 显式跳过，不假装通过。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.enums import ImageRoute
from app.services.multimodal import messages as mm_messages
from app.services.multimodal.audio import (
    REASON_EMPTY as AUDIO_REASON_EMPTY,
)
from app.services.multimodal.audio import (
    REASON_UNSUPPORTED,
    TranscodeError,
    transcode_to_wav,
    wav_duration_seconds,
    wav_params,
)
from app.services.multimodal.image import (
    REASON_DECODE,
    REASON_EMPTY,
    REASON_FORMAT,
    REASON_TOO_LARGE,
    ImageRejected,
    route_image,
    validate_image,
)
from app.services.multimodal.order_screenshot import (
    DEGRADED_AMBIGUOUS,
    ORDER_NO_LENGTH,
    find_order_no,
    mask_order_no,
)
from app.services.multimodal.pipeline import (
    SOURCE_ASR,
    SOURCE_OCR,
    SOURCE_ORIGINAL,
    SOURCE_VLM,
    MultimodalPipeline,
    NormalizedMessage,
)
import mm_fixtures as fx

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="本机无 ffmpeg，转码用例跳过")


def _run(coro):
    """在同步测试里跑协程。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 音频转码（BE-10 ①）
# ---------------------------------------------------------------------------
class TestAudioTranscode:
    """`transcode_to_wav` 的行为。"""

    def test_wav_passthrough_is_byte_identical(self) -> None:
        """wav 是目标格式，必须原样返回（不重复转码，避免二次损伤）。"""
        wav = fx.wav_bytes(seconds=0.3)
        assert transcode_to_wav(wav, "wav") == wav

    def test_wav_params_target_shape(self) -> None:
        """`wav_params` 返回 `(采样率, 采样位宽字节, 声道数)`。"""
        wav = fx.wav_bytes(seconds=0.2, sample_rate=16000)
        assert wav_params(wav) == (16000, 2, 1)

    def test_wav_duration(self) -> None:
        """时长按帧数换算，误差小于 0.05s。"""
        wav = fx.wav_bytes(seconds=0.5, sample_rate=16000)
        assert abs(wav_duration_seconds(wav) - 0.5) < 0.05

    def test_empty_input_raises(self) -> None:
        """空字节是明确错误，不是「格式不支持」。"""
        with pytest.raises(TranscodeError) as exc:
            transcode_to_wav(b"", "amr")
        assert exc.value.reason == AUDIO_REASON_EMPTY

    def test_garbage_raises_without_crashing(self) -> None:
        """坏字节必须收敛成 `TranscodeError`，绝不能把异常抛到调用方。"""
        with pytest.raises(TranscodeError) as exc:
            transcode_to_wav(b"\x00\x01\x02\x03garbage", "amr")
        assert exc.value.reason in {REASON_UNSUPPORTED, "transcode_failed", "output_invalid"}

    def test_silk_without_decoder_is_unsupported(self) -> None:
        """无外部 silk 解码器时明确报「格式不支持」，不静默失败。"""
        with pytest.raises(TranscodeError) as exc:
            transcode_to_wav(b"SILK" + b"\x00" * 64, "silk")
        assert exc.value.reason == REASON_UNSUPPORTED

    @needs_ffmpeg
    def test_mp3_transcodes_to_16k_mono(self, tmp_path: Path) -> None:
        """mp3 → 16k/单声道 wav（BE-10 ① 的 mp3 分项）。"""
        mp3 = _make_audio(tmp_path, fx.wav_bytes(seconds=0.6), "mp3")
        wav = transcode_to_wav(mp3, "mp3")
        assert wav_params(wav) == (16000, 2, 1)
        assert abs(wav_duration_seconds(wav) - 0.6) < 0.2

    @needs_ffmpeg
    def test_amr_transcodes_to_16k_mono(self, tmp_path: Path) -> None:
        """amr → 16k/单声道 wav（平台语音消息最常见格式）。"""
        amr = _make_audio(tmp_path, fx.wav_bytes(seconds=0.6), "amr")
        wav = transcode_to_wav(amr, "amr")
        assert wav_params(wav) == (16000, 2, 1)

    @needs_ffmpeg
    def test_wrong_format_hint_falls_back_to_probe(self, tmp_path: Path) -> None:
        """声明成 amr 实际是 mp3 时仍能转（不迷信扩展名）。"""
        mp3 = _make_audio(tmp_path, fx.wav_bytes(seconds=0.4), "mp3")
        wav = transcode_to_wav(mp3, "amr")
        assert wav_params(wav) == (16000, 2, 1)


def _make_audio(tmp_path: Path, wav: bytes, fmt: str) -> bytes:
    """用 ffmpeg 把 wav 转成指定格式的字节（仅测试用）。

    amr-nb 必须显式指定 8000Hz/单声道和编码器：ffmpeg 的 `libopencore_amrnb`
    不接受默认的 16k 立体声输入，否则退出码非零（实测 4294967256）。
    """
    src = tmp_path / "src.wav"
    dst = tmp_path / f"out.{fmt}"
    src.write_bytes(wav)
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(src)]
    if fmt == "amr":
        cmd += ["-ar", "8000", "-ac", "1", "-c:a", "libopencore_amrnb"]
    cmd.append(str(dst))
    subprocess.run(cmd, check=True, capture_output=True)  # noqa: S603
    return dst.read_bytes()


# ---------------------------------------------------------------------------
# ASR（BE-10 ②）
# ---------------------------------------------------------------------------
class TestLocalAsr:
    """`LocalAsr` 指纹表行为。"""

    def test_fixture_hit_returns_text_and_confidence(self) -> None:
        """命中指纹表时返回文本与置信度。"""
        import hashlib

        from app.adapters.asr import LocalAsr

        wav = fx.wav_bytes(seconds=0.3)
        key = hashlib.sha256(wav).hexdigest()
        asr = LocalAsr({key: {"text": "这个玩偶多高呀", "confidence": 0.92}})
        result = _run(asr.transcribe(wav))
        assert result.text == "这个玩偶多高呀"
        assert result.confidence == pytest.approx(0.92)
        assert result.degraded == []

    def test_string_fixture_uses_default_confidence(self) -> None:
        """指纹表值是字符串时用默认置信度。"""
        import hashlib

        from app.adapters.asr import LocalAsr

        wav = fx.wav_bytes(seconds=0.2)
        asr = LocalAsr({hashlib.sha256(wav).hexdigest(): "有货吗"})
        result = _run(asr.transcribe(wav))
        assert result.text == "有货吗"
        assert 0.9 <= result.confidence <= 1.0

    def test_miss_reports_no_engine_not_fabricated_text(self) -> None:
        """没命中就如实说没有引擎——**绝不编造文本**（编造会污染检索与查单）。"""
        from app.adapters.asr import LocalAsr

        result = _run(LocalAsr({}).transcribe(fx.wav_bytes(seconds=0.2)))
        assert result.text == ""
        assert result.confidence == 0.0
        assert "ASR_NO_ENGINE" in result.degraded


# ---------------------------------------------------------------------------
# OCR（BE-12 前置）
# ---------------------------------------------------------------------------
class TestLocalOcr:
    """`LocalOcr` 只认 0-9，但对订单号足够。"""

    def test_reads_order_number_digits(self) -> None:
        """订单截图里 12 位订单号必须能被读出来。"""
        from app.adapters.ocr import LocalOcr

        order_no = "626771284449"
        result = _run(LocalOcr().recognize(fx.order_shot(order_no, 0, 30)))
        assert order_no in result.text.replace(" ", "")

    def test_blocks_carry_geometry(self) -> None:
        """字符块必须带 `box`，否则订单号的分行滑窗无从下手。"""
        from app.adapters.ocr import LocalOcr

        result = _run(LocalOcr().recognize(fx.order_shot("123456789012", 0, 30)))
        assert result.blocks
        for block in result.blocks[:20]:
            assert set(block) >= {"text", "confidence", "box"}
            assert len(block["box"]) == 4

    def test_blank_image_returns_empty_text(self) -> None:
        """纯白图不该凭空造出数字。"""
        from PIL import Image

        from app.adapters.ocr import LocalOcr

        blank = fx.to_png(Image.new("RGB", (400, 400), "white"))
        result = _run(LocalOcr().recognize(blank))
        assert result.text.strip() == ""


# ---------------------------------------------------------------------------
# 订单号提取（BE-12 ①）
# ---------------------------------------------------------------------------
class TestOrderNumber:
    """`find_order_no` 的三档正则与几何路径。"""

    def test_clean_contiguous_digits(self) -> None:
        """干净连号：置信 0.99。"""
        result = find_order_no("订单号：626771284449 待发货")
        assert result.order_no == "626771284449"
        assert result.confidence == pytest.approx(0.99)

    def test_separated_groups(self) -> None:
        """带空格/横线的三段拼起来算。"""
        result = find_order_no("订单号 6267 7128 4449")
        assert result.order_no == "626771284449"
        assert result.confidence <= 0.95

    def test_lookalike_correction(self) -> None:
        """形近字（O→0、l→1）在无干净候选时才纠正。"""
        result = find_order_no("单号 62677128444O")
        assert result.order_no == "626771284440"
        assert result.corrected is True
        assert "ORDER_NO_CORRECTED" in result.degraded

    def test_no_number_returns_not_found(self) -> None:
        """没有号码就是没有，不给假阳性。"""
        result = find_order_no("你好，请问什么时候发货")
        assert result.found is False
        assert result.order_no == ""

    def test_multiple_candidates_lower_confidence(self) -> None:
        """多个候选时降置信度并打歧义标记，让上层先跟用户确认。"""
        result = find_order_no("626771284449 和 111222333444")
        assert len(result.candidates) >= 2
        assert result.confidence <= 0.6
        assert DEGRADED_AMBIGUOUS in result.degraded

    def test_blocks_path_prefers_geometry_over_noisy_text(self) -> None:
        """有几何信息时以块路径为准：噪声文本里的假号码不该赢。"""
        blocks = [
            {"text": "1", "confidence": 0.99, "box": [10, 100, 18, 24]},
            {"text": "2", "confidence": 0.99, "box": [30, 100, 18, 24]},
            {"text": "3", "confidence": 0.99, "box": [50, 100, 18, 24]},
            {"text": "4", "confidence": 0.99, "box": [70, 100, 18, 24]},
            {"text": "5", "confidence": 0.99, "box": [90, 100, 18, 24]},
            {"text": "6", "confidence": 0.99, "box": [110, 100, 18, 24]},
            {"text": "7", "confidence": 0.99, "box": [130, 100, 18, 24]},
            {"text": "8", "confidence": 0.99, "box": [150, 100, 18, 24]},
            {"text": "9", "confidence": 0.99, "box": [170, 100, 18, 24]},
            {"text": "0", "confidence": 0.99, "box": [190, 100, 18, 24]},
            {"text": "1", "confidence": 0.99, "box": [210, 100, 18, 24]},
            {"text": "2", "confidence": 0.99, "box": [230, 100, 18, 24]},
        ]
        result = find_order_no("999888777666", blocks=blocks)
        assert result.order_no == "123456789012"
        assert result.confidence >= 0.8

    def test_blocks_path_rejects_low_score_glyphs(self) -> None:
        """字形分数太低的块（中文偏旁）不能拼成号码。"""
        blocks = [
            {"text": str(i % 10), "confidence": 0.2, "box": [10 + i * 20, 100, 18, 24]}
            for i in range(12)
        ]
        result = find_order_no("", blocks=blocks)
        assert result.found is False

    def test_mask(self) -> None:
        """打码：前 4 后 4，中间 4 星。"""
        assert mask_order_no("626771284449") == "6267****4449"
        assert mask_order_no("123") == "123"
        assert mask_order_no("") == ""


# ---------------------------------------------------------------------------
# 图片校验（BE-11 ③）
# ---------------------------------------------------------------------------
class TestImageValidate:
    """`validate_image` 的三类拒绝。"""

    def test_png_ok(self) -> None:
        """正常 png：返回宽高与格式。"""
        info = validate_image(fx.order_shot("123456789012"))
        assert info.width == 600
        assert info.height == 900
        assert info.format == "PNG"
        assert info.ext == "png"

    def test_empty_rejected(self) -> None:
        """空字节。"""
        with pytest.raises(ImageRejected) as exc:
            validate_image(b"")
        assert exc.value.reason == REASON_EMPTY

    def test_too_large_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """超过 `image_max_bytes`。"""
        from app.services.multimodal import image as image_mod

        monkeypatch.setattr(image_mod, "_max_bytes", lambda: 10)
        with pytest.raises(ImageRejected) as exc:
            validate_image(fx.order_shot("123456789012"))
        assert exc.value.reason == REASON_TOO_LARGE

    def test_format_not_in_whitelist(self) -> None:
        """声明的扩展名不在白名单（gif 不支持）。"""
        with pytest.raises(ImageRejected) as exc:
            validate_image(fx.order_shot("123456789012"), filename="a.gif")
        assert exc.value.reason == REASON_FORMAT

    def test_decode_failed(self) -> None:
        """声明是 png 但内容不是图片。"""
        with pytest.raises(ImageRejected) as exc:
            validate_image(b"not an image at all", filename="a.png")
        assert exc.value.reason == REASON_DECODE

    def test_actual_format_wins_over_extension(self) -> None:
        """png 改名成 jpg 仍能过（以实际解码格式为准）。"""
        info = validate_image(fx.order_shot("123456789012"), filename="a.jpg")
        assert info.format == "PNG"


# ---------------------------------------------------------------------------
# 五路分流（BE-11 ②）
# ---------------------------------------------------------------------------
class TestRouteImage:
    """真图过真分类器。

    订单截图**必须走流水线**而不是直接调 `route_image`：本地 OCR 只认 0-9，
    读不出「订单号」这三个字，流水线的做法是先把 12 位号码抠出来，
    再把它当作「这是订单截图」的强信号喂给分类器。直接调分类器等于绕过了
    这条信号链，测出来的不是真实行为。
    """

    def test_order_screenshot(self) -> None:
        """订单截图 → 订单截图路由（经流水线）。"""
        result = _pipeline().preprocess(
            msg_type="IMAGE", media=fx.order_shot("626771284449", 0, 30)
        )
        assert result.route == ImageRoute.ORDER_SCREENSHOT.value

    def test_size_chart(self) -> None:
        """尺码表 → 尺码表路由（最容易被误判成订单截图的一路）。"""
        result = _pipeline().preprocess(msg_type="IMAGE", media=fx.size_chart(0))
        assert result.route == ImageRoute.SIZE_CHART.value

    def test_product_image(self) -> None:
        """商品图 → 商品图路由。"""
        result = _pipeline().preprocess(msg_type="IMAGE", media=fx.product(0))
        assert result.route == ImageRoute.PRODUCT_IMAGE.value

    def test_defect_image_with_hint(self) -> None:
        """瑕疵图配文字提示 → 瑕疵图路由。"""
        result = _pipeline().preprocess(
            msg_type="IMAGE", media=fx.defect(0), text="收到就是这样，有破损"
        )
        assert result.route == ImageRoute.DEFECT_IMAGE.value

    def test_meme(self) -> None:
        """小尺寸低文字密度 → 表情包路由。"""
        result = _pipeline().preprocess(msg_type="IMAGE", media=fx.meme(0))
        assert result.route == ImageRoute.MEME_IRRELEVANT.value

    def test_classifier_always_returns_a_route(self) -> None:
        """契约：分类器必须给出一个合法路由，不能返回 None。"""
        valid = {r.value for r in ImageRoute}
        for data in (fx.meme(1), fx.product(1), b"\x89PNG\r\n\x1a\n" + b"\x00" * 32):
            result = _run(route_image(data))
            assert result.route in valid

    def test_five_routes_accuracy_is_100_percent(self) -> None:
        """五路 × 4 张 = 20 张合成图，走完整流水线，准确率必须 100%。"""
        cases = fx.make_cases(n=4, size=30)
        pipe = _pipeline()
        hits = 0
        misses: list[str] = []
        for expected, hint, _order_no, data in cases:
            got = pipe.preprocess(msg_type="IMAGE", media=data, text=hint).route
            if got == expected:
                hits += 1
            else:
                misses.append(f"{expected}→{got}")
        assert hits == len(cases), f"分流错误：{misses}"
        assert len(cases) == 20


# ---------------------------------------------------------------------------
# 流水线（BE-11 ① 结构同构）
# ---------------------------------------------------------------------------
def _pipeline(**kwargs) -> MultimodalPipeline:
    """构造一个注入了可控 ASR 的流水线。"""
    from app.adapters.asr import LocalAsr
    from app.adapters.ocr import LocalOcr

    kwargs.setdefault("asr", LocalAsr({}))
    kwargs.setdefault("ocr", LocalOcr())
    return MultimodalPipeline(**kwargs)


class _FakeOcr:
    """返回固定文本、无字符块的 OCR 替身。

    模拟「能读中文的远端 OCR」：本地 `LocalOcr` 只认 0-9，读不出「订单号」标签，
    所以「订单截图但没号码」这个分支只能靠替身覆盖。
    """

    def __init__(self, text: str) -> None:
        self.text = text

    @property
    def name(self) -> str:
        """实现名。"""
        return "fake-ocr"

    async def recognize(self, image: bytes, *, content_type: str = "image/png"):
        """固定返回构造时的文本。"""
        from app.adapters.ports import OcrResult

        return OcrResult(text=self.text, confidence=0.9, blocks=[])


def _asr_for(wav: bytes, text: str, confidence: float = 0.92):
    """构造一个能命中指定 wav 的 LocalAsr。"""
    import hashlib

    from app.adapters.asr import LocalAsr

    return LocalAsr(
        {hashlib.sha256(wav).hexdigest(): {"text": text, "confidence": confidence}}
    )


class TestPipelineText:
    """文本路径。"""

    def test_passthrough(self) -> None:
        """文本原样透传，置信 1.0。"""
        result = _pipeline().preprocess(msg_type="TEXT", text="  这个玩偶多高  ")
        assert result.ok is True
        assert result.text == "这个玩偶多高"
        assert result.text_source == SOURCE_ORIGINAL
        assert result.confidence == 1.0

    def test_empty_text_not_ok(self) -> None:
        """空文本不进编排。"""
        result = _pipeline().preprocess(msg_type="TEXT", text="   ")
        assert result.ok is False
        assert "EMPTY_TEXT" in result.degraded

    def test_unknown_type_treated_as_text(self) -> None:
        """未知类型退化成文本路径，不抛异常。"""
        result = _pipeline().preprocess(msg_type="CARD", text="商品卡片")
        assert result.ok is True
        assert result.text == "商品卡片"


class TestPipelineVoice:
    """语音路径。"""

    def test_success(self) -> None:
        """转码 + ASR 成功：文本来自 ASR。"""
        wav = fx.wav_bytes(seconds=0.3)
        pipe = _pipeline(asr=_asr_for(wav, "这个玩偶多高呀", 0.92))
        result = pipe.preprocess(msg_type="VOICE", media=wav, filename="a.wav")
        assert result.ok is True
        assert result.text == "这个玩偶多高呀"
        assert result.text_source == SOURCE_ASR
        assert result.confidence == pytest.approx(0.92)
        assert result.attachments[0]["type"] == "voice"

    def test_empty_media(self) -> None:
        """没给媒体：明确不是「没听清」，是「没收到」。"""
        result = _pipeline().preprocess(msg_type="VOICE", media=b"")
        assert result.ok is False
        assert "VOICE_EMPTY" in result.degraded

    def test_transcode_failed_uses_unsupported_text(self) -> None:
        """坏字节 → 格式不支持话术，且不抛异常（BE-10 ③）。"""
        result = _pipeline().preprocess(
            msg_type="VOICE", media=b"\x00garbage", filename="a.amr"
        )
        assert result.ok is False
        assert any(d.startswith("ASR_TRANSCODE_FAILED") for d in result.degraded)
        assert result.fallback_text == mm_messages.VOICE_UNSUPPORTED

    def test_asr_empty_uses_unheard_text(self) -> None:
        """ASR 没出文本 → 「语音没听清」话术。"""
        wav = fx.wav_bytes(seconds=0.3)
        pipe = _pipeline(asr=_asr_for(wav, "", 0.0))
        result = pipe.preprocess(msg_type="VOICE", media=wav, filename="a.wav")
        assert result.ok is False
        assert "ASR_EMPTY" in result.degraded
        assert result.fallback_text == mm_messages.VOICE_UNHEARD

    def test_low_confidence_refuses_to_proceed(self) -> None:
        """置信度低于 0.6 不拿去检索/查单，宁可让用户再说一遍。"""
        wav = fx.wav_bytes(seconds=0.3)
        pipe = _pipeline(asr=_asr_for(wav, "这个玩偶", 0.35))
        result = pipe.preprocess(msg_type="VOICE", media=wav, filename="a.wav")
        assert result.ok is False
        assert "ASR_LOW_CONFIDENCE" in result.degraded

    def test_too_long(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """超过时长上限：不送 ASR，直接提示说重点。"""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "voice_max_duration_seconds", 1)
        wav = fx.wav_bytes(seconds=2.0)
        pipe = _pipeline(asr=_asr_for(wav, "很长的一段话"))
        result = pipe.preprocess(msg_type="VOICE", media=wav, filename="a.wav")
        assert result.ok is False
        assert "VOICE_TOO_LONG" in result.degraded
        assert result.fallback_text == mm_messages.VOICE_TOO_LONG

    def test_media_saved_for_lifecycle(self) -> None:
        """语音落盘，供 7 天生命周期清理（BE-10 ④）。"""
        wav = fx.wav_bytes(seconds=0.3)
        pipe = _pipeline(asr=_asr_for(wav, "有货吗"))
        result = pipe.preprocess(
            msg_type="VOICE", media=wav, filename="a.wav", save_media=True
        )
        assert result.ok is True
        assert result.media_url


class TestPipelineImage:
    """图片路径。"""

    def test_order_screenshot_produces_order_query_text(self) -> None:
        """订单截图 → 「帮我查一下订单 {号}」，来源 OCR（BE-12 ①）。"""
        order_no = "626771284449"
        result = _pipeline().preprocess(
            msg_type="IMAGE", media=fx.order_shot(order_no, 0, 30)
        )
        assert result.ok is True
        assert result.route == ImageRoute.ORDER_SCREENSHOT.value
        assert result.order_no == order_no
        assert result.text == f"帮我查一下订单 {order_no}"
        assert result.text_source == SOURCE_OCR

    def test_order_screenshot_without_number_asks_for_it(self) -> None:
        """订单截图但抠不到号 → 明确要订单号，不猜（BE-12 ③）。"""
        pipe = _pipeline(ocr=_FakeOcr("订单号：待发货"))
        result = pipe.preprocess(msg_type="IMAGE", media=fx.order_shot("1", 0, 30))
        assert result.route == ImageRoute.ORDER_SCREENSHOT.value
        assert result.ok is False
        assert result.fallback_text == mm_messages.ORDER_NO_NOT_FOUND

    def test_defect_image(self) -> None:
        """瑕疵图 → 售后反馈文本。"""
        result = _pipeline().preprocess(
            msg_type="IMAGE", media=fx.defect(0), text="收到就是这样，有破损"
        )
        assert result.ok is True
        assert result.route == ImageRoute.DEFECT_IMAGE.value
        assert result.text_source == SOURCE_VLM
        assert "破损" in result.text

    def test_size_chart(self) -> None:
        """尺码表 → 问尺码文本。"""
        result = _pipeline().preprocess(msg_type="IMAGE", media=fx.size_chart(0))
        assert result.ok is True
        assert result.route == ImageRoute.SIZE_CHART.value
        assert "尺码" in result.text

    def test_product_image(self) -> None:
        """商品图 → 咨询文本（有附带文字时优先用附带文字）。"""
        result = _pipeline().preprocess(
            msg_type="IMAGE", media=fx.product(0), text="这个还有货吗"
        )
        assert result.ok is True
        assert result.route == ImageRoute.PRODUCT_IMAGE.value
        assert result.text == "这个还有货吗"

    def test_meme_not_entering_orchestration(self) -> None:
        """表情包不进编排，直接轻引导。"""
        result = _pipeline().preprocess(msg_type="IMAGE", media=fx.meme(0))
        assert result.ok is False
        assert "IMAGE_IRRELEVANT" in result.degraded
        assert result.fallback_text == mm_messages.IMAGE_IRRELEVANT

    def test_empty_media(self) -> None:
        """没给图片。"""
        result = _pipeline().preprocess(msg_type="IMAGE", media=b"")
        assert result.ok is False
        assert "IMAGE_EMPTY" in result.degraded

    def test_undecodable_image_uses_unreadable_text(self) -> None:
        """坏图片 → 「图片没能加载出来」（BE-11 ③）。"""
        result = _pipeline().preprocess(
            msg_type="IMAGE", media=b"not an image", filename="a.png"
        )
        assert result.ok is False
        assert result.fallback_text == mm_messages.IMAGE_UNREADABLE

    def test_oversize_image_uses_unreadable_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超大图 → 同一个兜底话术，不 500。"""
        from app.services.multimodal import image as image_mod

        monkeypatch.setattr(image_mod, "_max_bytes", lambda: 10)
        result = _pipeline().preprocess(msg_type="IMAGE", media=fx.product(0))
        assert result.ok is False
        assert result.fallback_text == mm_messages.IMAGE_UNREADABLE


class TestPipelineIsomorphism:
    """BE-11 ①：三条路径输出字段**完全一致**。"""

    def test_same_keys_across_paths(self) -> None:
        """文本 / 语音 / 图片的 `to_dict()` 键集合必须相同。"""
        wav = fx.wav_bytes(seconds=0.3)
        text_dict = _pipeline().preprocess(msg_type="TEXT", text="你好").to_dict()
        voice_dict = _pipeline(asr=_asr_for(wav, "你好")).preprocess(
            msg_type="VOICE", media=wav, filename="a.wav"
        ).to_dict()
        image_dict = _pipeline().preprocess(
            msg_type="IMAGE", media=fx.product(0)
        ).to_dict()
        assert set(text_dict) == set(voice_dict) == set(image_dict)

    def test_dataclass_fields_are_stable(self) -> None:
        """字段名是契约：改了会打断 chat API 的映射。"""
        import dataclasses

        names = {f.name for f in dataclasses.fields(NormalizedMessage)}
        assert names == {
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
            "media_url",
            "attachments",
            "extra",
        }

    def test_never_raises_on_weird_input(self) -> None:
        """流水线对外承诺不抛异常：任何脏输入都收敛成 ok=False。"""
        pipe = _pipeline()
        for kwargs in (
            {"msg_type": "VOICE", "media": b"\xff" * 32},
            {"msg_type": "IMAGE", "media": b"\xff" * 32},
            {"msg_type": "IMAGE", "media": b"\x89PNG\r\n\x1a\n" + b"\x00" * 64},
            {"msg_type": "", "text": ""},
        ):
            result = pipe.preprocess(**kwargs)
            assert isinstance(result, NormalizedMessage)
            assert isinstance(result.ok, bool)


# ---------------------------------------------------------------------------
# 兜底话术（HC-1 零外链）
# ---------------------------------------------------------------------------
class TestMessages:
    """话术表。"""

    def test_all_fallback_texts_present(self) -> None:
        """每条话术都在注册表里且非空。"""
        assert set(mm_messages.FALLBACK_TEXTS) == {
            "VOICE_UNHEARD",
            "VOICE_UNSUPPORTED",
            "VOICE_TOO_LONG",
            "IMAGE_UNREADABLE",
            "IMAGE_IRRELEVANT",
            "ORDER_NO_NOT_FOUND",
        }
        for key, text in mm_messages.FALLBACK_TEXTS.items():
            assert text.strip(), key

    def test_no_links_anywhere(self) -> None:
        """HC-1：话术里不能出现链接/跳转字样。"""
        for key, text in mm_messages.FALLBACK_TEXTS.items():
            low = text.lower()
            for word in mm_messages.FORBIDDEN_LINK_WORDS:
                assert word.lower() not in low, f"{key} 含禁用词 {word}"

    def test_unknown_key_returns_default(self) -> None:
        """未知 key 不抛异常，返回通用引导。"""
        assert mm_messages.fallback_text("NOT_A_KEY").strip()
        assert mm_messages.fallback_text("").strip()

    def test_order_no_length_constant(self) -> None:
        """订单号长度是 12，写死在各处会不一致，这里锁死。"""
        assert ORDER_NO_LENGTH == 12
