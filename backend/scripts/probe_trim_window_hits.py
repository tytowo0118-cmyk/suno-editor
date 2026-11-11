"""器乐裁短放宽窗的真实命中率扩样：22 条已扣费 clip 一次扫完，零成本。

为什么要有这个脚本：`/api/trim` 的切点从「只看 ±0.5 s」放宽成「规划秒数的 25%（封顶 4 s）、
只往更短一侧」之后，README 里只有 2 条素材的证据，说明不了这机制在真实素材上到底有没有活干。
这里把 `static/output` 里所有已落盘的 clip 按**与生产代码同一口径**离线扫一遍：

    probe_end = target + after(0) + max(WINDOW_SEC, GUARD_SEC)
    window    = [target − trim_search_sec(target), target]
    两档选取  = ±0.5 s 内最近 → 否则窗内谱新颖度最强 → 否则硬停 target

同时给出旧口径（只看 ±0.5 s）的结果，用来量化放宽到底多切了几刀。
只读本地 WAV 与 numpy，不打 Suno、不建单、不花钱。

用法（宿主机没装 librosa/soundfile 那套，必须在后端容器里跑；backend/ 已挂到 /app）：

    docker exec -i museblocks-backend python /app/scripts/probe_trim_window_hits.py
    docker exec -i museblocks-backend python /app/scripts/probe_trim_window_hits.py --positions

2026-11-11 实测（22 条 clip，35.2–205.6 s，含账本里 `instrumental: true` 的那两条前奏）：

    target=8 s   放宽命中 2/22 = 9.1%（near 1 + wide 1），旧口径 1/22 = 4.5%
                 窗内候选数 median 0 / max 1，命中时提前量 median 0.616 s / max 1.237 s
    target=12 s  放宽命中 1/22 = 4.5%（wide 1），旧口径 0/22 = 0%
                 窗内候选数 median 0 / max 1

    放宽才多切到的两条：suno_1793677292_700f18_1.wav 8 s → 6.763 s（wide 0.117）、
                        suno_1793586494_72c3c7.wav 12 s → 11.384 s（wide 0.156）；
                        suno_1793678664_a4f0be.wav 8 s → 7.703 s 两档都会切（near 0.112）。
    账本里唯一的器乐素材（两条前奏 205.560 / 189.504 s）窗内 0 候选 ⇒ 这条路径真正面向的
    人群实测命中率 0/2。结论：放宽窗是「零风险、低频次的改进」，不触发就原样裁到规划秒数；
    真正会命中的是素材内部本来就有段落切换的场景（裁已拼好的多段成品，62.748 s 那条
    目标 18.5 s → 16.945 s，旧口径会硬停 18.500 s）。

`--positions` 回答的是另一个问题：左边界还能不能再放宽？答案是不能。
实测 `suno_1793678609_3932bd.wav` 在 3.280/0.114、4.290/0.228、5.300/0.099 有三条候选，
最强那条落在 4.290 s —— 窗放宽到规划的 50%（`[4, 8]`）就会把 8 s 前奏切成 4.29 s，
砍掉近一半内容。另有一批候选正好卡在 1.991 s，那是 `novelty_curve` 第一个可取块的位置
（探测分辨率下限，不是乐句边界）；它们左右 RMS 差值有 +4.89 dB 也有 −6.15 dB，方向不一致，
说明这类点没有音乐语义。判据就是这里打印的 Δ(left−right)：正=切点右侧更安静（像乐句收尾），
负=右侧更响（像进入下一段），接近 0 且强度低的多半是噪声。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "/app")

from utils import structure_detector  # noqa: E402
from utils.audio_processor import (  # noqa: E402
    CUT_SNAP_SEC,
    read_audio,
    trim_search_sec,
)

OUT = Path("/app/static/output")
TARGETS = (8.0, 12.0)
GUARD = max(structure_detector.WINDOW_SEC, structure_detector.GUARD_SEC)


def clip_files() -> list[Path]:
    """只收真实生成的 clip：裁切产物与拼接成品不算素材。"""
    return sorted(
        p for p in OUT.glob("*.wav")
        if "_trim" not in p.stem and not p.stem.startswith("museblocks-mix")
    )


def pick(cands: list[tuple[float, float]], target: float, search: float) -> tuple[float, str]:
    """与 `_truncate_to_length` 同口径的三档选取，返回 (切点秒, near/wide/none)。"""
    low = target - search - 1e-9
    high = target + 1e-9
    in_window = [(sec, strength) for sec, strength in cands if low <= sec <= high]
    nearby = [(sec, strength) for sec, strength in in_window if abs(sec - target) <= CUT_SNAP_SEC]
    if nearby:
        return min(nearby, key=lambda item: abs(item[0] - target))[0], "near"
    if in_window:
        return max(in_window, key=lambda item: (item[1], -abs(item[0] - target)))[0], "wide"
    return target, "none"


def band_dbfs(samples: np.ndarray, sr: int, a: float, b: float) -> float:
    lo, hi = max(0, int(a * sr)), max(0, int(b * sr))
    chunk = samples[lo:hi]
    if chunk.size == 0:
        return -120.0
    mono = chunk.mean(axis=1) if chunk.ndim > 1 else chunk
    rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))
    return 20 * np.log10(rms) if rms > 1e-9 else -120.0


def probe(samples: np.ndarray, sr: int, target: float) -> dict:
    probe_end = min(samples.shape[0], int(round((target + GUARD) * sr)))
    cands = structure_detector.boundary_candidates(samples[:probe_end], sr)
    search = trim_search_sec(target)
    wide_kept, wide_tier = pick(cands, target, search)
    near_kept, near_tier = pick(cands, target, CUT_SNAP_SEC)
    in_window = [(s, v) for s, v in cands if target - search - 1e-9 <= s <= target + 1e-9]
    return {
        "probe_sec": probe_end / float(sr),
        "search": search,
        "cands_probe": len(cands),
        "cands_window": len(in_window),
        "wide_kept": wide_kept,
        "wide_tier": wide_tier,
        "near_kept": near_kept,
        "near_tier": near_tier,
        "strength": max([v for _, v in in_window], default=0.0),
    }


def run_hits() -> None:
    rows = []
    for path in clip_files():
        samples, sr = read_audio(str(path))
        total = samples.shape[0] / float(sr)
        rows.append((path.name, total, sr, samples, {t: probe(samples, sr, t) for t in TARGETS}))

    for name, total, _sr, _s, per in rows:
        print(f"\n=== {name}  dur={total:.3f}s")
        if total <= TARGETS[-1] + GUARD:
            print("    跳过：短于最长目标的探测窗")
            continue
        for target in TARGETS:
            d = per[target]
            print(
                f"    target={target:5.1f}s search={d['search']:.2f}s probe={d['probe_sec']:.2f}s "
                f"cand_probe={d['cands_probe']} cand_win={d['cands_window']} "
                f"| 放宽: {d['wide_kept']:.3f}s ({d['wide_tier']}, 提前{target - d['wide_kept']:.3f}s, "
                f"最强{d['strength']:.3f}) | 旧口径: {d['near_kept']:.3f}s ({d['near_tier']})"
            )

    print("\n\n##### 汇总 #####")
    usable = [r for r in rows if r[1] > TARGETS[-1] + GUARD]
    print(f"可用素材（>{TARGETS[-1] + GUARD:.1f}s）：{len(usable)} / {len(rows)}")
    for target in TARGETS:
        tiers = [r[4][target]["wide_tier"] for r in usable]
        old = [r[4][target]["near_tier"] for r in usable]
        hits, ohits = sum(t != "none" for t in tiers), sum(o != "none" for o in old)
        only_wide = sum(1 for r in usable
                        if r[4][target]["wide_tier"] != "none" and r[4][target]["near_tier"] == "none")
        deltas = sorted(target - r[4][target]["wide_kept"] for r in usable
                        if r[4][target]["wide_tier"] != "none")
        win_c = sorted(r[4][target]["cands_window"] for r in usable)
        probe_c = sorted(r[4][target]["cands_probe"] for r in usable)
        print(
            f"\ntarget={target:.0f}s：放宽命中 {hits}/{len(usable)} = {hits / len(usable) * 100:.1f}%，"
            f"旧口径 {ohits}/{len(usable)} = {ohits / len(usable) * 100:.1f}%"
        )
        print(f"  只有放宽才切到边界、旧口径会硬停：{only_wide} 条")
        print(f"  tier 分布：near={tiers.count('near')} wide={tiers.count('wide')} none={tiers.count('none')}")
        print(f"  窗内候选数 median={win_c[len(win_c) // 2]} max={win_c[-1]}；"
              f"探测窗内 median={probe_c[len(probe_c) // 2]} max={probe_c[-1]}")
        if deltas:
            print(f"  命中时提前量 median={deltas[len(deltas) // 2]:.3f}s "
                  f"min={deltas[0]:.3f}s max={deltas[-1]:.3f}s")


def run_positions(probe_sec: float = 14.0) -> None:
    print(f"{'file':38s} {'cand(s)':>8s} {'str':>6s} {'left dB':>8s} {'right dB':>9s} {'Δ(L-R)':>7s}")
    for path in clip_files():
        samples, sr = read_audio(str(path))
        total = samples.shape[0] / float(sr)
        window = min(samples.shape[0], int(round(probe_sec * sr)))
        for sec, strength in structure_detector.boundary_candidates(samples[:window], sr):
            left = band_dbfs(samples, sr, sec - 1.0, sec)
            right = band_dbfs(samples, sr, sec, sec + 1.0)
            edge = "  <- 探测分辨率下限" if abs(sec - structure_detector.WINDOW_SEC) < 0.05 else ""
            print(f"{path.name:38s} {sec:8.3f} {strength:6.3f} {left:8.2f} {right:9.2f} "
                  f"{left - right:7.2f}{edge}")
        if total < probe_sec:
            print(f"{path.name:38s} (素材仅 {total:.1f}s，短于探测窗 {probe_sec:.1f}s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--positions", action="store_true",
                        help="打印探测窗内每条候选的位置/强度/左右 RMS 对比，判断左边界能否再放宽")
    parser.add_argument("--probe-sec", type=float, default=14.0,
                        help="--positions 的探测秒数，默认 14.0")
    args = parser.parse_args()
    if args.positions:
        run_positions(args.probe_sec)
    else:
        run_hits()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
