"""音频转码（BE-10 第 ② 步）：任意格式 → 16kHz / 16bit / 单声道 wav。

为什么必须统一到 16k/16bit/mono：
    ASR 服务对输入格式敏感，采样率或声道数不对会直接识别失败；把归一化放在
    转码层，ASR 适配器就只需要处理一种输入（`app/adapters/asr.py` 的契约）。

**环境事实（重要）**：本机 ffmpeg 支持 `amr`（libopencore_amrnb）与 `mp3`，
**不支持 `silk`**（微信/QQ 语音格式，ffmpeg 无对应 demuxer）。所以 silk 走
「可配置外部解码器」这条路：配了 `WK_SILK_DECODER_CMD` 就用它，没配就如实
返回 `unsupported_format`，由 BE-10 的兜底话术接住。这条差异已写进交接文档。
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import uuid
import wave
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 转码失败原因（对外只暴露这几种，不泄漏 ffmpeg 细节）
REASON_UNSUPPORTED = "unsupported_format"
REASON_EMPTY = "empty_input"
REASON_TOOL_MISSING = "ffmpeg_missing"
REASON_TIMEOUT = "transcode_timeout"
REASON_FAILED = "transcode_failed"
REASON_OUTPUT_INVALID = "output_invalid"

#: 允许直接透传的格式（已经是目标格式，省一次转码）
_PASSTHROUGH = {"wav"}

#: 各格式的临时文件后缀（ffmpeg 靠后缀猜容器，后缀错了会解析失败）
_SUFFIX = {
    "amr": ".amr",
    "silk": ".silk",
    "mp3": ".mp3",
    "wav": ".wav",
    "m4a": ".m4a",
    "aac": ".aac",
    "ogg": ".ogg",
    "opus": ".opus",
    "pcm": ".pcm",
}


class TranscodeError(Exception):
    """转码失败（携带稳定的 reason，供上层映射话术与降级标识）。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}:{detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _ffmpeg_bin() -> str:
    """ffmpeg 可执行路径（配置优先，其次 PATH）。"""
    settings = get_settings()
    configured = (getattr(settings, "ffmpeg_path", "") or "").strip()
    if configured:
        return configured
    return "ffmpeg"


def _mkwork(prefix: str) -> Path:
    """建一个**确认可写**的临时工作目录。

    ⚠️ 两个真实的坑（都在 Windows + DSH 文件沙箱下踩到）：
    1. `tempfile.mkdtemp()` 能建出目录，但往里写文件报
       `PermissionError: in.mp3`——沙箱给新建目录套的 ACL 不允许写入；
       同样目录改用普通 `mkdir` 创建就正常。
    2. 系统 TEMP 目录本身也可能不可写。

    所以这里**不用 mkdtemp**：自己用 `mkdir` + uuid 建目录，并真写一次探针文件
    确认可写；不可写就退到 `local_media_dir/.tmp`。
    """
    settings = get_settings()
    media_base = Path(getattr(settings, "local_media_dir", Path.cwd()))
    candidates = [
        Path(tempfile.gettempdir()) / prefix.rstrip("_"),
        media_base / ".tmp",
    ]
    last_error = ""
    for base in candidates:
        work = base / f"{prefix}{uuid.uuid4().hex[:12]}"
        try:
            work.mkdir(parents=True, exist_ok=False)
            probe = work / ".write_probe"
            probe.write_bytes(b"ok")
            probe.unlink()
            return work
        except Exception as exc:  # noqa: BLE001 - 换下一个候选
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("临时目录不可写（%s），换下一个候选", type(exc).__name__)
    raise TranscodeError(REASON_TOOL_MISSING, f"无可写临时目录（{last_error}）")


def wav_params(data: bytes) -> tuple[int, int, int]:
    """读 wav 的 (采样率, 采样位宽字节, 声道数)；非 wav 抛 `TranscodeError`。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            return wf.getframerate(), wf.getsampwidth(), wf.getnchannels()
    except Exception as exc:  # noqa: BLE001 - 非 wav / 头损坏都归这一类
        raise TranscodeError(REASON_OUTPUT_INVALID, type(exc).__name__) from exc


def wav_duration_seconds(data: bytes) -> float:
    """读 wav 时长（秒）。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            return frames / rate
    except Exception:  # noqa: BLE001 - 时长只用于日志/上限判断，读不出就当 0
        return 0.0


def _run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    """执行外部命令（统一捕获 stderr，失败时把尾部写进 detail 便于排障）。"""
    return subprocess.run(  # noqa: S603 - 参数全部由本模块拼装，无 shell
        cmd,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _transcode_with_ffmpeg(
    data: bytes, fmt: str, *, sample_rate: int, channels: int, timeout: float
) -> bytes:
    """调 ffmpeg 转成目标 wav。"""
    ffmpeg = _ffmpeg_bin()
    if not (Path(ffmpeg).exists() or shutil.which(ffmpeg)):
        raise TranscodeError(REASON_TOOL_MISSING, ffmpeg)
    suffix = _SUFFIX.get(fmt, f".{fmt}" if fmt else ".bin")
    work = _mkwork("wk_audio_")
    src = work / f"in{suffix}"
    dst = work / "out.wav"
    try:
        src.write_bytes(data)
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-acodec",
            "pcm_s16le",
            "-f",
            "wav",
            str(dst),
        ]
        try:
            proc = _run(cmd, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TranscodeError(REASON_TIMEOUT, f"{timeout}s") from exc
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size <= 44:
            detail = (proc.stderr or b"").decode("utf-8", "ignore").strip()[-300:]
            # ffmpeg 对未知容器一律报「Invalid data found / could not find codec」，
            # 这类错误在业务上就是「格式不支持」，不要笼统报 transcode_failed。
            lowered = detail.lower()
            if "invalid data" in lowered or "could not find codec" in lowered:
                raise TranscodeError(REASON_UNSUPPORTED, detail)
            raise TranscodeError(REASON_FAILED, detail)
        return dst.read_bytes()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _transcode_with_external_silk(
    data: bytes, *, sample_rate: int, channels: int, timeout: float
) -> bytes:
    """用外部 silk 解码器（如 silk_v3_decoder）转 wav。

    约定：命令模板里用 `{in}` / `{out}` 占位，例如
    `silk_v3_decoder {in} {out} -Fs_API 16000`。
    """
    settings = get_settings()
    template = (getattr(settings, "silk_decoder_cmd", "") or "").strip()
    if not template:
        raise TranscodeError(REASON_UNSUPPORTED, "silk 需要配置 WK_SILK_DECODER_CMD")
    work = _mkwork("wk_silk_")
    src = work / "in.silk"
    mid = work / "mid.wav"
    dst = work / "out.wav"
    try:
        src.write_bytes(data)
        cmd = template.format(**{"in": str(src), "out": str(mid)}).split()
        try:
            proc = _run(cmd, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TranscodeError(REASON_TIMEOUT, f"{timeout}s") from exc
        if proc.returncode != 0 or not mid.exists() or mid.stat().st_size <= 44:
            detail = (proc.stderr or b"").decode("utf-8", "ignore").strip()[-300:]
            raise TranscodeError(REASON_FAILED, detail)
        # 外部解码器输出的采样率/声道不可信，再走一次 ffmpeg 归一
        return _transcode_with_ffmpeg(
            mid.read_bytes(),
            "wav",
            sample_rate=sample_rate,
            channels=channels,
            timeout=timeout,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def transcode_to_wav(
    data: bytes,
    fmt: str | None,
    *,
    sample_rate: int | None = None,
    channels: int | None = None,
    timeout: float = 20.0,
) -> bytes:
    """把任意语音字节转成 16kHz/16bit/单声道 wav。

    Args:
        data: 原始语音字节。
        fmt: 源格式（`amr` / `silk` / `mp3` / `wav` …），未知格式也允许，
            由 ffmpeg 自己探测容器。
        sample_rate: 目标采样率，默认取配置（16000）。
        channels: 目标声道数，默认取配置（1）。
        timeout: 单次转码超时（秒）。

    Returns:
        目标 wav 字节。**已校验** 采样率/位宽/声道数与目标一致。

    Raises:
        TranscodeError: 任何失败，`reason` 为稳定标识（见模块常量）。
    """
    if not data:
        raise TranscodeError(REASON_EMPTY)
    settings = get_settings()
    rate = int(sample_rate or getattr(settings, "audio_target_sample_rate", 16000))
    chans = int(channels or getattr(settings, "audio_target_channels", 1))
    normalized = (fmt or "").strip().lower().lstrip(".")

    if normalized in _PASSTHROUGH:
        # 已经是 wav：只在参数不符时才转，省掉一次进程开销
        try:
            got_rate, got_width, got_chans = wav_params(data)
        except TranscodeError:
            got_rate = 0
        if got_rate == rate and got_chans == chans:
            return data

    if normalized == "silk":
        wav = _transcode_with_external_silk(
            data, sample_rate=rate, channels=chans, timeout=timeout
        )
    else:
        wav = _transcode_with_ffmpeg(
            data, normalized, sample_rate=rate, channels=chans, timeout=timeout
        )

    got_rate, got_width, got_chans = wav_params(wav)
    if got_rate != rate or got_chans != chans or got_width != 2:
        raise TranscodeError(
            REASON_OUTPUT_INVALID,
            f"got {got_rate}Hz/{got_width * 8}bit/{got_chans}ch",
        )
    return wav


def cleanup_temp_root() -> None:
    """清理本模块遗留的临时目录（测试/运维用；正常路径在 finally 里已删）。"""
    root = Path(tempfile.gettempdir())
    for path in root.glob("wk_audio_*"):
        shutil.rmtree(path, ignore_errors=True)
    for path in root.glob("wk_silk_*"):
        shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "REASON_EMPTY",
    "REASON_FAILED",
    "REASON_OUTPUT_INVALID",
    "REASON_TIMEOUT",
    "REASON_TOOL_MISSING",
    "REASON_UNSUPPORTED",
    "TranscodeError",
    "cleanup_temp_root",
    "transcode_to_wav",
    "wav_duration_seconds",
    "wav_params",
]

