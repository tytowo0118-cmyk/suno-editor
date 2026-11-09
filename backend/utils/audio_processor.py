"""音频拼接与交叉淡化。

核心目标：把多段由 AI 生成的音频拼成一首连贯的作品，同时避免两类常见问题：
1. 拼接点爆音（click / pop）：波形在剪切点不连续、存在直流偏移、增益突变；
2. 拼接点"音量塌陷"：线性淡入淡出会让两段能量叠加后中间变小声。

实现方案：
- 等功率交叉淡化（equal-power crossfade），增益曲线为 cos/sin 曲线，
  两段不相关信号叠加时总功率恒定，听感上不会出现中间凹陷；
- 首尾余弦淡入淡出（默认 12ms），消除文件边界突变；
- 逐段去直流偏移，避免 DC 阶跃产生低频"噗"声；
- 统一采样率 / 声道 / 位深后再混音；
- 非首段先做首部静音裁切（默认 -50 dBFS 帧能量阈值）：续写音频头部带约 270ms 数字静音，
  直接交叉淡化会在交接点形成听感真空；
- 可选的按段时长上限（`max_durations`）：Suno 不吃时长参数，纯器乐段会写满一整首，
  超长部分在这里物理截掉，切点吸附到结构边界候选而不是硬停在秒数上；
- 终混仅做峰值衰减（默认 -1 dBFS），不软削波、不压限，保留动态。
"""

from __future__ import annotations

import logging
import os
import tempfile
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np
from pydub import AudioSegment

from utils import structure_detector

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
OUTPUT_DIR = STATIC_DIR / "output"

SAMPLE_RATE = 44_100
CHANNELS = 2
SAMPLE_WIDTH = 2  # 16-bit

DEFAULT_CROSSFADE_MS = 200
DEFAULT_EDGE_FADE_MS = 12
DEFAULT_PEAK_DBFS = -1.0
# Suno 续写音频头部带约 270ms 数字静音，直接交叉淡化会听感断层，按帧能量剔除
HEAD_TRIM_THRESHOLD_DBFS = -50.0
# 按时长裁段时，切点允许在目标秒数前后各吸附的窗口；与前端吸附窗同一口径
CUT_SNAP_SEC = 0.5
MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024

DEFAULT_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}


class AudioSourceError(ValueError):
    """音频来源不合法（协议 / 域名 / 大小 / 路径越界）。"""


def _allowed_hosts() -> set:
    extra = os.getenv("AUDIO_ALLOWED_HOSTS", "")
    return DEFAULT_ALLOWED_HOSTS | {h.strip() for h in extra.split(",") if h.strip()}


def _resolve_local_path(source: str) -> Path:
    """支持绝对路径、相对路径以及后端自己的 /static/xxx 路径。"""
    if source.startswith("/static/"):
        candidate = (STATIC_DIR.parent / source.lstrip("/")).resolve()
    else:
        candidate = Path(source).expanduser()
        if not candidate.is_absolute():
            candidate = (BASE_DIR / candidate).resolve()
        else:
            candidate = candidate.resolve()
    if source.startswith("/static/"):
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError as exc:
            raise AudioSourceError("静态资源路径越界") from exc
    return candidate


def _download(source: str) -> Path:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme not in ("http", "https"):
        raise AudioSourceError(f"不支持的协议: {parsed.scheme}")
    if parsed.hostname not in _allowed_hosts():
        raise AudioSourceError(
            f"音频域名 {parsed.hostname!r} 不在白名单内；可通过 AUDIO_ALLOWED_HOSTS 配置"
        )

    suffix = Path(parsed.path).suffix or ".wav"
    handle = tempfile.NamedTemporaryFile(prefix="muse_", suffix=suffix, delete=False)
    temp_path = Path(handle.name)
    total = 0
    try:
        request = urllib.request.Request(source, headers={"User-Agent": "museblocks/0.1"})
        with urllib.request.urlopen(request, timeout=15) as response:
            # 重定向后再次校验，防止跳转到非白名单地址
            final_host = urllib.parse.urlparse(response.geturl()).hostname
            if final_host not in _allowed_hosts():
                raise AudioSourceError("音频重定向到了非白名单域名")
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise AudioSourceError("音频文件超过下载大小限制")
                handle.write(chunk)
    except Exception:
        handle.close()
        temp_path.unlink(missing_ok=True)
        raise
    handle.close()
    return temp_path


def _load_audio(source: str) -> Tuple[np.ndarray, int, Optional[Path]]:
    """读取音频并统一成 float32 [n, 2] / 44.1kHz。返回 (数组, 采样率, 临时文件)。"""
    temp_path: Optional[Path] = None
    if source.startswith(("http://", "https://")):
        temp_path = _download(source)
        path = temp_path
    else:
        path = _resolve_local_path(source)

    if not path.exists():
        raise AudioSourceError(f"音频文件不存在: {source}")

    try:
        if path.suffix.lower() == ".wav":
            segment = AudioSegment.from_wav(str(path))
        else:
            # mp3 / flac 等需要系统安装 ffmpeg
            segment = AudioSegment.from_file(str(path))
        segment = (
            segment.set_frame_rate(SAMPLE_RATE)
            .set_sample_width(SAMPLE_WIDTH)
            .set_channels(CHANNELS)
        )
        samples = np.asarray(segment.get_array_of_samples(), dtype=np.float32)
        samples = samples.reshape((-1, segment.channels)) / 32768.0
        return samples.astype(np.float32, copy=False), segment.frame_rate, temp_path
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def read_audio(source: str) -> Tuple[np.ndarray, int]:
    """对外暴露的读取入口：统一成 float32 [n, 2] 后立即释放下载得到的临时文件。"""
    samples, frame_rate, temp_path = _load_audio(source)
    if temp_path is not None:
        temp_path.unlink(missing_ok=True)
    return samples, frame_rate


def _equal_power_curves(length: int) -> Tuple[np.ndarray, np.ndarray]:
    """等功率交叉淡化曲线。

    t 取每个采样点的中点，避免首尾恰好落在 0/1 造成的边界不连续。
    gain_out = cos(t * pi/2), gain_in = sin(t * pi/2)，满足 g_out² + g_in² = 1。
    """
    t = (np.arange(length, dtype=np.float32) + 0.5) / max(length, 1)
    theta = t * (np.pi / 2.0)
    return np.cos(theta)[:, None], np.sin(theta)[:, None]


def _crossfade(a: np.ndarray, b: np.ndarray, crossfade_samples: int) -> np.ndarray:
    if crossfade_samples <= 0:
        return np.concatenate([a, b], axis=0)
    a_gain, b_gain = _equal_power_curves(crossfade_samples)
    overlap = a[-crossfade_samples:] * a_gain + b[:crossfade_samples] * b_gain
    return np.concatenate([a[:-crossfade_samples], overlap, b[crossfade_samples:]], axis=0)


def _trim_head_silence(current: np.ndarray, threshold_dbfs: float) -> Tuple[np.ndarray, int]:
    """物理剔除起始的静音帧，返回 (裁剪后数组, 剔除的帧数)。

    按帧取声道最大绝对值，第一个超过阈值的帧即新起点；整段都不超过阈值时不裁，
    否则会交出空数组。
    """
    if current.shape[0] == 0:
        return current, 0
    threshold = 10.0 ** (threshold_dbfs / 20.0)
    frame_peak = np.max(np.abs(current), axis=1)
    above = np.nonzero(frame_peak >= threshold)[0]
    if above.size == 0:
        return current, 0
    start = int(above[0])
    if start == 0:
        return current, 0
    return current[start:], start


def _trim_tail_silence(current: np.ndarray, threshold_dbfs: float) -> Tuple[np.ndarray, int]:
    """物理剔除结尾的静音帧，返回 (裁剪后数组, 剔除的帧数)。

    判据与 `_trim_head_silence` 完全一致（同一个阈值），只是从后往前找最后一个超过阈值的帧。
    Suno 成品自带 0.06~1.96s 尾部数字静音，留在接缝前会在两段之间造成听感真空；
    整段都不超过阈值时不裁，避免交出空数组。
    """
    if current.shape[0] == 0:
        return current, 0
    threshold = 10.0 ** (threshold_dbfs / 20.0)
    frame_peak = np.max(np.abs(current), axis=1)
    above = np.nonzero(frame_peak >= threshold)[0]
    if above.size == 0:
        return current, 0
    end = int(above[-1]) + 1
    if end >= current.shape[0]:
        return current, 0
    return current[:end], current.shape[0] - end


def _truncate_to_length(
    samples: np.ndarray,
    sample_rate: int,
    max_sec: Optional[float],
    snap_sec: float = CUT_SNAP_SEC,
) -> Tuple[np.ndarray, Optional[float], bool]:
    """超长素材物理截到 max_sec，切点优先吸附到结构边界候选。

    「这一段要 8 秒」没别的地方能兑现：Suno 载荷里根本没有时长字段（New API 的
    `/suno/submit/music` 只认 prompt / mv / title / tags / make_instrumental /
    task_id / continue_at / continue_clip_id / gpt_description_prompt / notify_hook），
    纯器乐 + 空歌词上游一律写满一整首，实测 177~193s。

    切点不硬停在 max_sec 上：先在该点前后 `snap_sec` 内取最近的谱形状新颖度候选 ——
    候选的定位精度实测 MAE 0.062s（floor 0.09 / gap 1.0s，见 README 检测器标定），
    比按秒表瞎切可靠；窗内没有候选就回到 max_sec 本身。代价是切点可能比目标长 ≤0.5s。

    只探测 `max_sec + snap_sec + max(WINDOW_SEC, GUARD_SEC)` 这一段：候选出不了这个范围，
    全曲扫描是白花时间（检测器实测 182.378s 整首要 787.5ms，9.5s 探针只要 13.1ms）。
    探针右边界必须让出 WINDOW_SEC：`novelty_curve` 的 `index <= total - window_frames`
    要求切点右侧凑满一整个 2s 块，只让 GUARD_SEC 会把窗口右半边的候选全吃掉 ——
    实测 27.5s 目标配 29.0s 探针时 27.777s 的候选消失，探针改 30.0s 才回来。

    Returns:
        (裁后音频, 保留的秒数 —— 没裁就是 None, 切点是否吸到了候选)
    """
    if max_sec is None or max_sec <= 0:
        return samples, None, False
    limit_samples = int(round(max_sec * sample_rate))
    if samples.shape[0] <= limit_samples:
        return samples, None, False

    probe_end = min(
        samples.shape[0],
        limit_samples
        + int(
            round(
                (snap_sec + max(structure_detector.WINDOW_SEC, structure_detector.GUARD_SEC))
                * sample_rate
            )
        ),
    )
    candidates = structure_detector.boundary_candidates(samples[:probe_end], sample_rate)
    nearby = [moment for moment, _ in candidates if abs(moment - max_sec) <= snap_sec]
    snapped = bool(nearby)
    cut_sec = min(nearby, key=lambda moment: abs(moment - max_sec)) if snapped else max_sec
    kept = min(max(int(round(cut_sec * sample_rate)), 1), samples.shape[0])
    if kept >= samples.shape[0]:
        return samples, None, False
    logger.info(
        "duration_truncated: kept=%.3fs target=%.3fs cut=%.3fs snapped=%s discarded=%.3fs "
        "(probe=%.3fs candidates=%d)",
        kept / float(sample_rate),
        max_sec,
        cut_sec,
        snapped,
        (samples.shape[0] - kept) / float(sample_rate),
        probe_end / float(sample_rate),
        len(candidates),
    )
    return samples[:kept], round(kept / float(sample_rate), 3), snapped


def _apply_edge_fade(samples: np.ndarray, frame_rate: int, edge_fade_ms: int) -> np.ndarray:
    """终混首尾余弦淡变，防止播放器起停时出现 click。"""
    n = min(int(frame_rate * max(0, edge_fade_ms) / 1000.0), samples.shape[0] // 2)
    if n <= 1:
        return samples
    fade_in = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n, dtype=np.float32))
    samples[:n] *= fade_in[:, None]
    samples[-n:] *= fade_in[::-1, None]
    return samples


def _limit_peak(samples: np.ndarray, peak_dbfs: float) -> np.ndarray:
    """只衰减不提升，保证交叉淡化时 1.414 倍能量叠加不削波。"""
    ceiling = 10.0 ** (peak_dbfs / 20.0)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > ceiling > 0.0:
        samples = samples * (ceiling / peak)
    return np.clip(samples, -1.0, 1.0)


def _write_wav(path: Path, samples: np.ndarray, frame_rate: int) -> None:
    """直接写 16-bit PCM WAV，避免在无 ffmpeg 环境下依赖 pydub 导出。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(np.round(samples * 32767.0), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(samples.shape[1])
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(frame_rate)
        handle.writeframes(pcm.tobytes())


def trim_clip_to_seconds(
    source: str,
    max_sec: float,
    *,
    output_dir: Optional[Path] = None,
) -> Tuple[Path, float, float, bool]:
    """把单段音频裁到 `max_sec` 以内并落盘，用于器乐候选生成后的「只留本段」。

    上游 `make_instrumental` 不吃任何时长参数，空歌词的前奏会写满一整首
    （实测 205.560 / 189.504 s）；不裁的话候选卡与时间轴都按整首展开，
    用户看到的「前奏」其实是整首歌，直到拼接时才被 `max_duration_sec` 砍到 8 s。

    与拼接侧同一套裁切口径：先剔尾部数字静音，再在目标秒数 ±`CUT_SNAP_SEC`
    内吸附结构候选；窗内没有候选就硬停在目标秒数。2026-11-09 对两条前奏素材实测：
    前 90 s 内检测器只给出 83.4 s 一个候选，8 s 目标窗内没有，裁出就是 8.000 s。

    Returns:
        (输出路径, 原始时长, 保留时长, 切点是否吸附)；
        原片本来就没超限时不写新文件，返回原路径与原始时长。
    """
    samples, frame_rate = read_audio(source)
    source_sec = samples.shape[0] / float(frame_rate)
    trimmed, _ = _trim_tail_silence(samples, HEAD_TRIM_THRESHOLD_DBFS)
    kept, kept_sec, snapped = _truncate_to_length(trimmed, frame_rate, max_sec)
    if kept_sec is None:
        return _resolve_local_path(source), round(source_sec, 3), round(source_sec, 3), False

    target_dir = output_dir or OUTPUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{Path(source).stem}_trim{int(round(max_sec * 1000))}ms.wav"
    output_path = target_dir / filename
    # 先写临时文件再原子替换：同一源被并发裁两次时不会读到写了一半的 WAV
    temp_path = target_dir / f".{filename}.{os.urandom(4).hex()}.tmp"
    try:
        _write_wav(temp_path, kept, frame_rate)
        os.replace(temp_path, output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    logger.info(
        "clip_trimmed: source=%.3fs kept=%.3fs target=%.3fs snapped=%s file=%s",
        source_sec,
        kept_sec,
        max_sec,
        snapped,
        output_path.name,
    )
    return output_path, round(source_sec, 3), kept_sec, snapped


def _output_path(output_path: Optional[str]) -> Path:
    if output_path:
        path = Path(output_path).expanduser()
        if not path.is_absolute():
            path = OUTPUT_DIR / path
        return path
    return OUTPUT_DIR / f"stitch_{os.urandom(5).hex()}.wav"


def stitch_audio(
    file1: str,
    file2: str,
    crossfade_ms: int = DEFAULT_CROSSFADE_MS,
    output_path: Optional[str] = None,
    *,
    edge_fade_ms: int = DEFAULT_EDGE_FADE_MS,
    peak_dbfs: float = DEFAULT_PEAK_DBFS,
) -> str:
    """把两段音频拼接为一段，交接处使用等功率交叉淡化。

    Args:
        file1: 前一段音频（本地路径 / /static/xxx / http(s) URL）。
        file2: 后一段音频。
        crossfade_ms: 交叉淡化长度，自动限制在两段音频长度以内。
        output_path: 输出文件；默认写入 static/output/stitch_xxx.wav。
        edge_fade_ms: 成品首尾淡变，防爆音。
        peak_dbfs: 成品峰值上限，默认 -1 dBFS。

    Returns:
        合成后的本地文件路径（字符串）。
    """
    segment_a, frame_rate_a, temp_a = _load_audio(str(file1))
    try:
        segment_b, frame_rate_b, temp_b = _load_audio(str(file2))
        try:
            if frame_rate_a != frame_rate_b:
                raise AudioSourceError("两段音频采样率未能统一")
            if segment_a.size == 0 or segment_b.size == 0:
                raise AudioSourceError("输入音频为空")

            max_crossfade = max(0, min(segment_a.shape[0], segment_b.shape[0]) - 1)
            requested = int(max(0, crossfade_ms) * frame_rate_a / 1000.0)
            crossfade_samples = min(requested, max_crossfade)

            mixed = _crossfade(segment_a, segment_b, crossfade_samples)
            mixed -= mixed.mean(axis=0, keepdims=True)
            mixed = _apply_edge_fade(mixed, frame_rate_a, edge_fade_ms)
            mixed = _limit_peak(mixed, peak_dbfs)

            path = _output_path(output_path)
            _write_wav(path, mixed, frame_rate_a)
            logger.info(
                "stitched %s + %s -> %s (crossfade=%d samples)",
                file1, file2, path.name, crossfade_samples,
            )
            return str(path)
        finally:
            if temp_b is not None:
                temp_b.unlink(missing_ok=True)
    finally:
        if temp_a is not None:
            temp_a.unlink(missing_ok=True)


def stitch_sequence(
    sources: Sequence[Tuple[str, int]],
    output_path: Optional[str] = None,
    *,
    edge_fade_ms: int = DEFAULT_EDGE_FADE_MS,
    peak_dbfs: float = DEFAULT_PEAK_DBFS,
    head_trim_threshold_dbfs: float = HEAD_TRIM_THRESHOLD_DBFS,
    max_durations: Optional[Sequence[Optional[float]]] = None,
) -> Tuple[str, float, float, List[float], List[float], List[float], List[Optional[float]]]:
    """按顺序拼接任意多段音频，每段可指定到下一段的交叉淡化时长。

    Args:
        sources: [(音频路径或 URL, 与下一段的 crossfade_ms), ...]
        output_path: 输出路径；默认 static/output/stitch_xxx.wav
        head_trim_threshold_dbfs: 首尾静音裁切的帧能量阈值，默认 -50 dBFS（首段不裁头部片头）
        max_durations: 与 sources 等长的每段保留秒数上限，None 表示不裁；
            切点会吸附到结构边界候选，见 `_truncate_to_length`

    Returns:
        (输出路径, 成品时长秒, 成品峰值 dBFS, 每段在成品中的起点秒数,
         每段被剔除的头部秒数, 每段被剔除的尾部秒数, 每段被截断后的秒数（未截断为 None）)
    """
    if not sources:
        raise AudioSourceError("至少需要一段音频")
    if len(sources) == 1:
        raise AudioSourceError("至少需要两段音频才能拼接")
    if max_durations is not None and len(max_durations) != len(sources):
        raise AudioSourceError("max_durations 必须与 segments 等长")

    temps: List[Path] = []
    frame_rate = SAMPLE_RATE
    limits: List[Optional[float]] = list(max_durations) if max_durations else [None] * len(sources)
    try:
        mixed: Optional[np.ndarray] = None
        pending_crossfade = 0
        segment_starts: List[float] = []
        head_trim: List[float] = []
        tail_trim: List[float] = []
        truncate_to: List[Optional[float]] = []
        truncate_snapped: List[bool] = []
        for index, (source, crossfade_to_next) in enumerate(sources):
            current, rate, temp = _load_audio(source)
            if temp is not None:
                temps.append(temp)
            if mixed is None:
                # 首段保留原始片头留白，但尾部静音一样要剔除，否则接缝前留着整段死寂
                current, trimmed_tail = _trim_tail_silence(current, head_trim_threshold_dbfs)
                frame_rate = rate
                current, kept_sec, snapped = _truncate_to_length(current, frame_rate, limits[index])
                tail_trim.append(trimmed_tail / float(frame_rate))
                truncate_to.append(kept_sec)
                truncate_snapped.append(snapped)
                mixed = current
                pending_crossfade = crossfade_to_next
                segment_starts.append(0.0)
                head_trim.append(0.0)
                logger.info(
                    "silence_trimmed: tail=%d frames (%.3fms) at threshold %.1fdBFS "
                    "(stitch step 0, remaining=%.3fms)",
                    trimmed_tail,
                    trimmed_tail * 1000.0 / frame_rate,
                    head_trim_threshold_dbfs,
                    current.shape[0] * 1000.0 / frame_rate,
                )
                continue
            if rate != frame_rate:
                raise AudioSourceError("音频采样率未能统一")
            if current.size == 0:
                raise AudioSourceError("输入音频为空")
            if mixed.size == 0:
                raise AudioSourceError("输入音频为空")
            # 首段保留原始片头留白；后续段落先剔除垫片再做交叉淡化，避免交接处听感真空
            current, trimmed = _trim_head_silence(current, head_trim_threshold_dbfs)
            head_trim.append(trimmed / float(frame_rate))
            current, trimmed_tail = _trim_tail_silence(current, head_trim_threshold_dbfs)
            tail_trim.append(trimmed_tail / float(frame_rate))
            current, kept_sec, snapped = _truncate_to_length(current, frame_rate, limits[index])
            truncate_to.append(kept_sec)
            truncate_snapped.append(snapped)
            if current.shape[0] == 0:
                raise AudioSourceError("输入音频裁剪后为空")
            logger.info(
                "silence_trimmed: head=%d frames (%.3fms) tail=%d frames (%.3fms) "
                "at threshold %.1fdBFS (stitch step %d, remaining=%.3fms)",
                trimmed,
                trimmed * 1000.0 / frame_rate,
                trimmed_tail,
                trimmed_tail * 1000.0 / frame_rate,
                head_trim_threshold_dbfs,
                index,
                current.shape[0] * 1000.0 / frame_rate,
            )
            max_crossfade = max(0, min(mixed.shape[0], current.shape[0]) - 1)
            requested = int(max(0, pending_crossfade) * frame_rate / 1000.0)
            applied = min(requested, max_crossfade)
            prev_end = mixed.shape[0]
            # 淡化区是两段共用的样本，所以本段起点要往前挪一个重叠长度
            segment_starts.append((prev_end - applied) / float(frame_rate))
            mixed = _crossfade(mixed, current, applied)
            logger.info(
                "stitch step %d junction audit: prev_end=%.3fms overlap=%.3fms "
                "segment_start=%.3fms next_len=%.3fms head_trim=%.3fms samples=%d/%d",
                index,
                prev_end * 1000.0 / frame_rate,
                applied * 1000.0 / frame_rate,
                segment_starts[-1] * 1000.0,
                current.shape[0] * 1000.0 / frame_rate,
                trimmed * 1000.0 / frame_rate,
                applied,
                max_crossfade,
            )
            pending_crossfade = crossfade_to_next

        if mixed is None or mixed.size == 0:
            raise AudioSourceError("未产生有效音频")

        mixed -= mixed.mean(axis=0, keepdims=True)
        mixed = _apply_edge_fade(mixed, frame_rate, edge_fade_ms)
        mixed = _limit_peak(mixed, peak_dbfs)

        path = _output_path(output_path)
        _write_wav(path, mixed, frame_rate)
        duration = mixed.shape[0] / float(frame_rate)
        peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
        peak_dbfs_actual = 20.0 * np.log10(peak) if peak > 0 else -120.0
        logger.info(
            "stitch done: frames=%d rate=%d duration=%.3fs segment_start_ms=%s head_trim_ms=%s "
            "tail_trim_ms=%s truncate_to_sec=%s truncate_snapped=%s",
            mixed.shape[0],
            frame_rate,
            duration,
            [round(start * 1000.0, 3) for start in segment_starts],
            [round(sec * 1000.0, 3) for sec in head_trim],
            [round(sec * 1000.0, 3) for sec in tail_trim],
            truncate_to,
            truncate_snapped,
        )
        return (
            str(path),
            duration,
            float(peak_dbfs_actual),
            segment_starts,
            head_trim,
            tail_trim,
            truncate_to,
            truncate_snapped,
        )
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)
