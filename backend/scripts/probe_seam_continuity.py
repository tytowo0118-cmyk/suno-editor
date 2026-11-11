"""续写接缝 vs 独立接缝的连续性对照：同一段前缀、同一个切点，只换第二段素材的来源。

为什么要有这个脚本：逐段续写（`continue_clip_id` / `continue_at`）上线后，「接缝不再像换了
一首歌」是定这条链路的主要理由，但它一直是零成本断言（载荷里有没有那两个字段），没人量过
**声学**上到底差多少。这里用已扣费素材本地重拼对照组，把差值摊开成可复查的数字。

口径（三条混音共用第一段素材与切点，唯一变量是第二段）：

    续写组 = 上一段裁后成品 + 由它 extend 出来的下一段
    独立组 = 上一段裁后成品 + 早前**独立整首生成**的下一段（同风格提示、不同歌词）

「同一个切点」不是探针硬编码秒表：探针 `--prev` 喂的是生产链路**先检测再截取**的产物
（对上游整首跑 `boundary_candidates`，窗内取候选吸附；本条素材窗 `[6.000, 8.000]` 内
candidates=0，才回退到规划 8.000s，日志 `clip_trimmed ... snapped=False`）。探针里的
`--prev-max-sec` 对一个已经只有 8.000s 的文件是 no-op（`_truncate_to_length` 早退，不再检测），
所以三条混音的接缝都落在同一处 7.992s。第二段（有词）不裁，三条都一样。

指标（接缝处，全部纯 numpy；后端容器没有 scipy/librosa）：

    nov_at / nov_pct / nov_med / peak15
        `utils.structure_detector.novelty_curve` 的 1−cos 谱形状新颖度，以及它在**同一条成品
        全曲**里的分位。判据直接借用生产门控 `DEFAULT_FLOOR=0.09`：高于它 = 会被自己的边界
        检测器认成一个段边界 = 接缝是结构事件。
    timbre   成品里接缝前后各 200 ms 的 log-mel 形状余弦。
    rms_db   接缝前后各 150 ms 的 RMS 落差，**正数=后段更轻**。
    hole     接缝后到最近一个 ≥−20 dBFS 的 50 ms 帧之间的距离（听感真空）。
    head_cos 素材级：上一段末尾 2 s 对下一段「第一个有声帧起 2 s」的余弦 —— 按各自能量起点对齐，
             所以不受拼接侧静音判据松紧影响，是最干净的音乐连续性。
    replay   上一段前 4 s 在下一段前 14 s 里的最大归一化互相关（FFT 滑窗）。
             ≈1 说明上游把种子重放了一遍，接近 0 说明完全不相干。
    pcp_cos  12 音级强度（PCP，100–2000 Hz 的 FFT 幅值按半音折叠）在「上一段末尾 2 s vs
             下一段开头 2 s」之间的余弦 —— 谱形状量的是音色，PCP 量的是**还在不在同一个调上**。

**刻意不算的指标**：采样级爆点（`x[i0]−x[i0−1]` 与其邻域比值）与起止点节奏网格。
前者在 8 ms 等功率淡化下三条混音逐位相同（实测 0.00847），比值差异全来自分母、不能当判据；
后者谱通量密度实测 158–215 个 / 52–65 s（≈3 个/秒）远超节拍量级，外推相位误差是探测器噪声。

只读本地 WAV + POST 本服务的 `/api/stitch`（纯本地 pydub，不建单、不花钱）；**绝不打
`/api/generate/jobs`**，那是扣费入口。

用法（必须在后端容器里跑，`backend/` 已挂到 /app；素材名去 `static/output/` 里挑）：

    docker exec -i museblocks-backend python /app/scripts/probe_seam_continuity.py \
        --prev suno_1794216778_a667ec_trim8000ms.wav \
        --extend suno_1794369441_d563ed.wav \
        --independent suno_1794366599_fabd72_1.wav \
        --independent suno_1794366593_e3a874.wav \
        --prev-max-sec 8 --crossfade-ms 8

    # 只算素材级指标、一条混音都不新建：
    ... --no-stitch

2026-11-11 实测（1 段真接缝，续写组来自唯一一次真实 extend；独立组是另一句歌词的已扣费素材）：

    接缝处            续写        独立 A      独立 B
    nov_at           0.0166      0.1545      0.1175     ← 独立两条都越过 floor 0.09
    nov_pct          59.6 %      96.3 %      96.7 %     ← 续写接缝在全曲里只是个普通点
    timbre           0.945       −0.309      −0.001
    rms_db           −0.84       +15.30      +15.61
    hole             0.05 s      1.10 s      0.40 s
    head_cos         0.983       0.867       0.895
    pcp_cos          0.671       0.291       0.485
    replay           0.294       0.137       0.137

    同一批素材上 `continue_at=8.0` 与成品接缝 `segment_start_sec[1]=7.992` 是同一个点
    （差 8 ms 交叉淡化），且续写重拼与 UI 成品 sha256 逐位一致（前 14 位 `7a84124f5d6b8c`，
    连跑三遍同样）⇒ 量的就是用户耳朵听到的那条混音。两边同源：`continue_at` 取的就是
    上一步**检测/回退出来的**那个秒数（前端 `continuationSeed` 读 `take.duration`），
    改裁短口径时续写点与刀口一起挪，不会错位。
    样本只有 1 段 1 接缝，独立组还换了歌词，所以 `hole` 里混着 −50 dBFS 首静音判据偏松的成分；
    不受拼接影响的是 `head_cos`。结论按「方向明确、n=1」引用，别当统计显著性。
    `pcp_cos`（12 音级分布余弦）与 `head_cos` 同向；曾经一起算出来的「主音漂移半音数」
    （续写组 7、独立组 1 / 6）与「前 5 音级重合数」（3 / 1 / 4）互相矛盾 —— 无词前奏本身
    没有稳定调中心，单帧 argmax 由一段混响决定，这两个离散量已从探针删掉（见 `key_change`）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from utils import structure_detector  # noqa: E402
from utils.audio_processor import (  # noqa: E402
    HEAD_TRIM_THRESHOLD_DBFS,
    _trim_head_silence,
    _trim_tail_silence,
    read_audio,
)

API_BASE = "http://127.0.0.1:8000"


def mono(source: str) -> Tuple[np.ndarray, int]:
    samples, sr = read_audio(source)
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    return samples.astype(np.float32), sr


def shape_descriptor(samples: np.ndarray, sr: int) -> np.ndarray:
    """一段音频的谱形状描述子：逐帧去带均值 log-mel 取帧均值，(帧数, 32) → 32 维。"""
    return np.asarray(structure_detector._spectral_shape(samples, sr).mean(axis=0), dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def max_norm_corr(ref: np.ndarray, tgt: np.ndarray) -> float:
    """ref 在 tgt 里滑动的最大归一化互相关（FFT；直接 np.correlate 要 1e11 次乘加）。"""
    n_ref, n_tgt = len(ref), len(tgt)
    if n_ref < 64 or n_tgt <= n_ref:
        return 0.0
    size = 1
    while size < n_tgt + n_ref:
        size *= 2
    ref_z = ref - float(np.mean(ref))
    spec_r = np.fft.rfft(ref_z, size)
    spec_t = np.fft.rfft(tgt - float(np.mean(tgt)), size)
    cc = np.fft.irfft(spec_t * np.conjugate(spec_r), size)[: n_tgt - n_ref + 1]
    # 分子只要 ref 去均值（sum(ref_z)=0 让 tgt 的均值项自动消掉）；
    # 分母必须用每个滑窗自己的能量与均值，否则相关系数会偏
    tgt_d = tgt.astype(np.float64)
    sq = np.concatenate([[0.0], np.cumsum(np.square(tgt_d))])
    sm = np.concatenate([[0.0], np.cumsum(tgt_d)])
    win_sq = sq[n_ref:] - sq[: n_tgt - n_ref + 1]
    win_mean = (sm[n_ref:] - sm[: n_tgt - n_ref + 1]) / n_ref
    win_energy = np.maximum(win_sq - n_ref * np.square(win_mean), 1e-12)
    denom = np.sqrt(win_energy) * float(np.linalg.norm(ref_z.astype(np.float64)))
    return float(np.max(np.abs(cc) / denom))


def first_loud(x: np.ndarray, sr: int) -> int:
    """相对判据（该素材自身包络的 60 分位 × 0.7）找第一个有声帧，只看前 10 s。"""
    win = int(0.05 * sr)
    n = min(len(x) // win, int(10 * sr) // win)
    if n <= 1:
        return 0
    env = np.sqrt(np.mean(np.square(x[: n * win].reshape(n, win)), axis=1))
    floor = float(np.percentile(env, 60)) * 0.7
    above = np.nonzero(env > floor)[0]
    return int(above[0] * win) if above.size else 0


def db(x: np.ndarray) -> float:
    return float(20 * np.log10(float(np.sqrt(np.mean(np.square(x), dtype=np.float64))) + 1e-12))


def seam_metrics(mix_source: str, seam: float) -> Dict[str, Any]:
    x, sr = mono(mix_source)
    i0 = int(round(seam * sr))
    times, nov = structure_detector.novelty_curve(x, sr)
    valid = ~np.isnan(nov)
    k = int(np.nanargmin(np.abs(times - seam)))
    near_band = np.abs(times - seam) <= 1.5

    def rms(a: int, b: int) -> float:
        seg = x[max(0, a) : min(len(x), b)]
        return float(np.sqrt(np.mean(np.square(seg), dtype=np.float64)) + 1e-12)

    w150, w200 = int(0.15 * sr), int(0.2 * sr)
    shape_pre = shape_descriptor(x[max(0, i0 - w200) : i0], sr)
    shape_post = shape_descriptor(x[i0 : i0 + w200], sr)

    # 接缝前后各 5 s 内，最近一次「50 ms 帧 RMS ≥ −20 dBFS」离接缝多远
    win = int(0.05 * sr)
    lo, hi = max(0, i0 - 5 * sr), min(len(x), i0 + 5 * sr)
    seg = x[lo:hi]
    frames = len(seg) // win
    env_db = 20 * np.log10(np.sqrt(np.mean(np.square(seg[: frames * win].reshape(frames, win)), axis=1)) + 1e-9)
    loud = np.nonzero(env_db >= -20.0)[0]
    rel = (loud * win + lo) / sr - seam if loud.size else np.zeros(0)
    before = rel[rel < 0]
    after = rel[rel >= 0]

    return {
        "dur": round(len(x) / sr, 3),
        "seam": round(seam, 3),
        "nov_at": round(float(nov[k]), 4),
        "nov_pct": round(float(np.mean(nov[valid] <= nov[k])) * 100, 1),
        "nov_med": round(float(np.nanmedian(nov)), 4),
        "peak15": round(float(np.nanmax(nov[near_band])), 4) if near_band.any() else None,
        "timbre": round(cosine(shape_pre, shape_post), 3),
        "rms_db": round(20.0 * np.log10(rms(i0 - w150, i0) / rms(i0, i0 + w150)), 2),
        "hole_before": round(float(-before[-1]), 3) if before.size else None,
        "hole_after": round(float(after[0]), 3) if after.size else None,
    }


def key_profile(x: np.ndarray, sr: int) -> np.ndarray:
    """12 音级强度（PCP）：把 100–2000 Hz 的 FFT 幅值按半音折到 12 桶，L1 归一化。

    补 `head_cos` 看不见的维度 —— 谱形状是**音色/配器**，音色相近但移了调，cos 会高估连续性；
    PCP 只看音高集合的分布，正是「还是不是同一首歌的调」那条听感。
    零填充到 65536 点：分辨率 0.67 Hz，在 440 Hz 处约 2.6 音分，足够把半音分开。
    """
    size = 1 << 16
    if x.size == 0:
        return np.zeros(12, dtype=np.float32)
    window = np.hanning(min(x.size, size))
    padded = x[: len(window)] * window
    mag = np.abs(np.fft.rfft(padded, size))
    freqs = np.fft.rfftfreq(size, 1.0 / sr)
    band = (freqs >= 100.0) & (freqs <= 2000.0)
    freqs, mag = freqs[band], mag[band]
    midi = 12.0 * np.log2(np.maximum(freqs, 1e-6) / 440.0) + 69.0
    pcs = np.rint(midi).astype(int) % 12
    profile = np.array([mag[pcs == pc].max() if np.any(pcs == pc) else 0.0 for pc in range(12)])
    total = float(profile.sum())
    return profile / total if total > 0 else profile.astype(np.float32)


def key_change(a: np.ndarray, b: np.ndarray) -> Dict[str, Any]:
    """两段音频的音级分布对照，只出连续量。

    刻意不报「主音漂了几个半音」与「前 5 音级重合数」：这批数据里它们给出互相矛盾的读法
    （续写组主音差 7 个半音、独立组只差 1 / 6），原因是无词前奏本身没有稳定调中心，
    argmax 落在哪个桶基本由一段混响决定。离散量当噪声删掉，别留给后面的人误读。
    """
    return {"pcp_cos": round(cosine(a, b), 3)}


def handoff(prev_source: str, next_source: str) -> Dict[str, Any]:
    """素材级交接：绕开拼接侧的静音判据，直接量两段素材本身接不接得上。"""
    a, sr = mono(prev_source)
    b, _ = mono(next_source)
    seg = 2 * sr
    b0 = first_loud(b, sr)
    tail, head = a[max(0, len(a) - seg) :], b[b0 : b0 + seg]
    cos = cosine(shape_descriptor(tail, sr), shape_descriptor(head, sr))
    return {
        "head_cos": round(cos, 3),
        "next_head_silence": round(b0 / sr, 3),
        "replay": round(max_norm_corr(a[: min(len(a), 4 * sr)], b[: min(len(b), 14 * sr)]), 3),
        "prev_tail_db": round(db(a[-int(0.2 * sr) :]), 2),
        "next_head_db": round(db(head[: int(0.2 * sr)]), 2),
        **key_change(key_profile(tail, sr), key_profile(head, sr)),
    }


def clip_trims(source: str) -> Dict[str, float]:
    """拼接侧对非首段会做的首尾静音裁切（帧峰值 ≥ −50 dBFS），按同一判据复算。"""
    x, sr = read_audio(source)
    _, head = _trim_head_silence(x.copy(), HEAD_TRIM_THRESHOLD_DBFS)
    _, tail = _trim_tail_silence(x.copy(), HEAD_TRIM_THRESHOLD_DBFS)
    return {"head_trim_sec": round(head / sr, 3), "tail_trim_sec": round(tail / sr, 3)}


def mix_tag(name: str) -> str:
    """给每条对照混音一个稳定、只含安全字符的尾巴。

    不带素材名的话，同一组里第二条会覆盖第一条的成品（`output_name` 只到组名，同秒建单时间戳
    相同），A/B 试听就少一条。取前 20 个字符：`suno_<落盘秒>_<hash>` 的落盘秒已经足够互异，
    尾巴上的 hash 反而会被截掉一半变成 `no_1794…` 这种难读的形态。
    """
    return re.sub(r"[^A-Za-z0-9_-]", "-", Path(name).stem)[:20]


def stitch(
    prev_url: str, next_url: str, name: str, prev_max_sec: Optional[float], crossfade_ms: int
) -> dict:
    body = json.dumps(
        {
            "output_name": name,
            "segments": [
                {"id": "prev", "audio_url": prev_url, "crossfade_ms": crossfade_ms, "max_duration_sec": prev_max_sec},
                {"id": "next", "audio_url": next_url, "crossfade_ms": crossfade_ms},
            ],
        }
    ).encode()
    request = urllib.request.Request(
        f"{API_BASE}/api/stitch", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prev", required=True, help="第一段素材文件名（static/output 下）")
    parser.add_argument("--extend", action="append", default=[], help="续写组素材，可给多次")
    parser.add_argument("--independent", action="append", default=[], help="独立组素材，可给多次")
    parser.add_argument("--prev-max-sec", type=float, default=None, help="第一段在成品里的上限秒数（裁短段传规划秒数）")
    parser.add_argument("--crossfade-ms", type=int, default=8)
    parser.add_argument("--no-stitch", action="store_true", help="不新建混音，只算素材级指标")
    args = parser.parse_args()

    prev_source = f"/static/output/{args.prev}"
    prev_url = f"{API_BASE}{prev_source}"
    report: Dict[str, Any] = {"prev": args.prev, "prev_trims": None, "cases": []}
    if args.prev_max_sec is not None:
        report["prev_trims"] = clip_trims(prev_source)

    groups: List[Tuple[str, str]] = [("extend", name) for name in args.extend]
    groups += [("independent", name) for name in args.independent]
    for group, name in groups:
        next_source = f"/static/output/{name}"
        entry: Dict[str, Any] = {
            "group": group,
            "next": name,
            "clip_trims": clip_trims(next_source),
            "handoff": handoff(prev_source, next_source),
        }
        if not args.no_stitch:
            tag = f"seamprobe-{group[:3]}-{mix_tag(name)}"
            resp = stitch(prev_url, f"{API_BASE}{next_source}", tag, args.prev_max_sec, args.crossfade_ms)
            entry["mix"] = {
                "filename": resp["filename"],
                "duration": resp["duration"],
                "peak_dbfs": resp["peak_dbfs"],
                "segment_start_sec": resp["segment_start_sec"],
                "head_trim_sec": resp["head_trim_sec"],
                "tail_trim_sec": resp["tail_trim_sec"],
            }
            entry["seam"] = seam_metrics(f"/static/output/{resp['filename']}", float(resp["segment_start_sec"][1]))
        report["cases"].append(entry)

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
