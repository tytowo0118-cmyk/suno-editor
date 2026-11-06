"""单条音频的结构边界检测：块余弦谱形状新颖度（纯 numpy，无外部依赖）。

用途限定在「给播放头提供吸附候选」，不是自动分段。判据：
`nov = 1 − cos(左 2s 块均值谱形状, 右 2s 块均值谱形状)`，谱形状是逐帧去带均值后 L2 归一化的
log-mel，所以这个量本身是绝对量纲 —— 阈值必须用**固定绝对下限**，不能用 mean+kσ：后者在同一段
音频「单独看」和「嵌进长混合里」给出的候选数量完全不同，短素材的结构会被自己的统计量吃掉。

在 4 组探测混合（1084.106s / 7 条毫秒级已知接缝，接缝真值来自 `/api/stitch` 的
`segment_start_sec`）上实测：floor 0.09 + 最小间隔 1.0s → 召回 7/7、有符号偏差 −0.026s、
MAE 0.062s；代价是每 15s 约 0.80 个假候选（精确率 10.8%，全曲 ±1s 窗口覆盖 11.1% 的时间轴）。
因此候选只配当吸附目标：clip 级段边界才是毫秒真值，见 README 的段落拼接一节。
"""

from __future__ import annotations

import logging
from typing import Tuple

import numpy as np

logger = logging.getLogger(__name__)

N_FFT = 2048
HOP = 512
N_MEL = 32
MEL_FMIN = 50.0
MEL_FMAX = 8000.0
# 左右各取多长的块均值做对比；太短会跟着乐句抖，太长会把相邻两处变化糊成一团
WINDOW_SEC = 2.0
# 候选之间的最小间距。3.0s 会让一条真边界被 3s 内更强的假峰挤掉（召回 6/7），
# 1.0s 才能做到 7/7 —— 吸附场景允许可选点靠得近，不必强行拉开
DEFAULT_GAP_SEC = 1.0
DEFAULT_FLOOR = 0.09
# 两端各留这段距离不检测：块不完整时 novelty 会被零垫片污染
GUARD_SEC = 1.0

_MELBANK_CACHE: dict[Tuple[int, int], np.ndarray] = {}


def _h_to_mel(freqs: np.ndarray) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + freqs / 700.0)


def _mel_to_hz(mels: np.ndarray) -> np.ndarray:
    return 700.0 * (10.0 ** (mels / 2595.0) - 1.0)


def _mel_filterbank(sample_rate: int, n_fft: int) -> np.ndarray:
    """Slaney 风格三角 mel 滤波器组，形状 (N_MEL, n_fft//2+1)，按频点带宽归一化。"""
    key = (sample_rate, n_fft)
    cached = _MELBANK_CACHE.get(key)
    if cached is not None:
        return cached
    edges = _mel_to_hz(np.linspace(_h_to_mel(MEL_FMIN), _h_to_mel(MEL_FMAX), N_MEL + 2))
    freqs = np.linspace(0.0, sample_rate / 2.0, n_fft // 2 + 1)
    bank = np.zeros((N_MEL, freqs.size), dtype=np.float32)
    for i in range(N_MEL):
        low, mid, high = edges[i], edges[i + 1], edges[i + 2]
        up = np.clip((freqs - low) / max(mid - low, 1e-9), 0.0, 1.0)
        down = np.clip((high - freqs) / max(high - mid, 1e-9), 0.0, 1.0)
        bank[i] = np.minimum(up, down)
    bank *= np.clip(2.0 / (edges[2:] - edges[:-2]), 0.0, None)[:, None]
    _MELBANK_CACHE[key] = bank
    return bank


def _spectral_shape(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """逐帧谱形状，返回 (帧数, N_MEL)：去带均值的 log-mel 再做 L2 归一化。

    去掉逐帧均值再归一化，等于只保留「能量在频带间怎么分布」，丢掉响度维度。
    留着响度会让同一素材在不同混音上下文里给出不同候选，实测偏差也从 ±0.22s 退化到 ±0.51s。
    """
    if samples.size < N_FFT:
        samples = np.pad(samples, (0, N_FFT - samples.size))
    frames = np.lib.stride_tricks.sliding_window_view(samples, N_FFT)[::HOP]
    window = np.hanning(N_FFT).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames * window, axis=1))
    logmel = np.log((spec @ _mel_filterbank(sample_rate, N_FFT).T.astype(np.float32)) + 1e-10)
    logmel -= logmel.mean(axis=1, keepdims=True)
    norm = np.linalg.norm(logmel, axis=1, keepdims=True)
    return logmel / np.clip(norm, 1e-9, None)


def novelty_curve(samples: np.ndarray, sample_rate: int) -> Tuple[np.ndarray, np.ndarray]:
    """返回 (分界时刻数组, 新颖度数组)，两端块不完整或落在守护区内的位置记为 nan。

    第 i 个点的左块覆盖 frame[i+1-K .. i]、右块覆盖 frame[i+1 .. i+K]，
    所以分界时刻是两帧之间的 (i + 0.5) * HOP / sample_rate，而不是右块首帧。
    """
    mono = samples.mean(axis=1) if samples.ndim == 2 else samples
    shape = _spectral_shape(mono.astype(np.float32), sample_rate)
    total = shape.shape[0]
    window_frames = int(round(WINDOW_SEC * sample_rate / HOP))
    if total <= 2 * window_frames:
        return np.zeros(0), np.zeros(0)

    padded = np.pad(shape, ((window_frames, window_frames), (0, 0)))
    csum = np.cumsum(padded, axis=0)
    left = (csum[window_frames : total + window_frames] - csum[:total]) / window_frames
    right = (
        csum[2 * window_frames : total + 2 * window_frames]
        - csum[window_frames : total + window_frames]
    ) / window_frames
    dot = np.einsum("ij,ij->i", left, right)
    norms = np.linalg.norm(left, axis=1) * np.linalg.norm(right, axis=1)
    nov = 1.0 - np.clip(dot / np.clip(norms, 1e-12, None), -1.0, 1.0)

    times = (np.arange(total) + 0.5) * HOP / sample_rate
    duration = total * HOP / sample_rate
    index = np.arange(total)
    keep = (
        (index >= window_frames - 1)
        & (index <= total - window_frames)
        & (times > GUARD_SEC)
        & (times < duration - GUARD_SEC)
    )
    curve = np.full(total, np.nan, dtype=np.float32)
    curve[keep] = nov[keep]
    return times, curve


def boundary_candidates(
    samples: np.ndarray,
    sample_rate: int,
    floor: float = DEFAULT_FLOOR,
    gap_sec: float = DEFAULT_GAP_SEC,
) -> list[Tuple[float, float]]:
    """按固定绝对下限取峰，返回按时间升序的 [(秒, 新颖度)]。

    取峰顺序是「高新颖度优先 + 与已选点保持 gap_sec」，不是先出现先要，
    否则一个弱峰会把紧随其后的强边界挤掉。
    """
    times, nov = novelty_curve(samples, sample_rate)
    if times.size == 0:
        return []
    above = np.nonzero(nov >= floor)[0]
    if above.size == 0:
        return []
    ordered = above[np.argsort(-nov[above])]
    chosen: list[Tuple[float, float]] = []
    for i in ordered:
        moment = float(times[i])
        if all(abs(moment - picked[0]) >= gap_sec for picked in chosen):
            chosen.append((moment, float(nov[i])))
    chosen.sort(key=lambda item: item[0])
    logger.info(
        "structure_candidates: %d points floor=%.3f gap=%.1fs analyzed=%.3fs",
        len(chosen),
        floor,
        gap_sec,
        times.size * HOP / sample_rate,
    )
    return chosen
