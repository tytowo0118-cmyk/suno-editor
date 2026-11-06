"""段落边界检测精度的零成本标定：DL 结构分段 vs 谱形状检测器。

真值不靠人工标注：探测混合是本地用 `/api/stitch`（8 ms 微交叉淡化）把已付费落盘的成品重拼出来的，
接缝时刻由响应里的 `segment_start_sec` 给出，那是采样点级的毫秒真值。整个过程不打 Suno，不花钱。

用法（宿主机或后端容器内都能跑，只用标准库）：

    python3 backend/scripts/calibrate_structure.py --build-probes   # 重拼探测混合并记真值
    python3 backend/scripts/calibrate_structure.py                  # 两个接口对着同一批真值打分
    python3 backend/scripts/calibrate_structure.py --rss            # 顺带采样分析容器的峰值内存

`--build-probes` 写出的 `backend/data/structure_probe.json` 是标定输入清单（文件名 / 时长 / 接缝），
后续跑都读它，所以同一批数字可以反复复现；素材换了就重跑一次。
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

API_BASE = "http://127.0.0.1:8000"
REPO_BACKEND = Path(__file__).resolve().parent.parent
MANIFEST = REPO_BACKEND / "data" / "structure_probe.json"

# 交叉淡化时长：与前端成品拼接保持一致（8 ms ≈ 352 采样，听感上是硬切）
PROBE_CROSSFADE_MS = 8
# 判定窗口：0.5 s 是产品实际用的吸附窗，1.0 s 是历史标定口径，两个都算出来才看得出差别
WINDOWS: Tuple[float, ...] = (0.5, 1.0)

# 探测混合的原料：全部是 backend/static/output 里已存在的成品，时长各不相同，
# 让接缝落在 30–405 s 的宽区间里（短前奏、中间段、超长成品都覆盖到）
PROBE_SETS: List[Tuple[str, List[str]]] = [
    ("dlp-A", ["suno_1793678392_03ddbf.wav", "suno_1793678405_ea3715.wav", "suno_1793678438_b37b93.wav"]),
    ("dlp-B", ["suno_1793677292_700f18.wav", "suno_1793677292_700f18_1.wav", "suno_1793678609_3932bd.wav"]),
    ("dlp-C", ["suno_1793758427_7d0507.wav", "suno_1793678664_a4f0be.wav", "suno_1793584223_f06929.wav", "suno_1793589182_4ac98b.wav"]),
    ("dlp-D", ["suno_1793671935_ec163b.wav", "suno_1793671937_aeb167.wav", "suno_1793671917_bfd475.wav"]),
]


def request_json(path: str, payload: Optional[dict] = None, timeout: float = 3600.0) -> Tuple[dict, float]:
    """POST/GET 并回 (body, 墙钟秒)。

    500 也要把 body 读出来 —— 降级契约就写在那里（`degraded` + 兜底段），
    按 HTTPError 丢掉的话，标定就变成「悄悄少了一条数据」。
    """
    url = f"{API_BASE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read()), time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return json.loads(body), time.perf_counter() - started
        except Exception:
            raise RuntimeError(f"{path} -> HTTP {exc.code}: {body[:200]!r}") from exc


def build_probes() -> List[Dict[str, Any]]:
    entries = []
    for name, files in PROBE_SETS:
        segments = [
            {"id": f[:-4], "audio_url": f"/static/output/{f}", "crossfade_ms": PROBE_CROSSFADE_MS}
            for f in files
        ]
        body, _wall = request_json(
            "/api/stitch", {"segments": segments, "output_name": name}, timeout=600.0
        )
        seams = [round(sec, 3) for sec in body["segment_start_sec"][1:]]
        entries.append(
            {
                "filename": body["filename"],
                "duration": body["duration"],
                "source_clips": files,
                "crossfade_ms": PROBE_CROSSFADE_MS,
                "seams": seams,
                # 恒等式自检：Σ(段长 − 头裁 − 尾裁) − 重叠 = 成品时长，残差应在毫秒舍入量级
                "head_trim_sec": body["head_trim_sec"],
                "tail_trim_sec": body["tail_trim_sec"],
            }
        )
        print(f"{body['filename']:38s} dur={body['duration']:8.3f}s seams={seams}")
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n清单写入 {MANIFEST.relative_to(REPO_BACKEND.parent)}")
    return entries


def score(candidates: List[float], seams: List[float], duration: float) -> Dict[str, Any]:
    """同一套打分函数用在两个检测器上，数字才可直接对比。"""
    per_window: Dict[str, Any] = {}
    matched_by_seam: List[Tuple[float, Optional[float]]] = []
    for win in WINDOWS:
        hit = 0
        errors: List[float] = []
        for seam in seams:
            nearest = min(candidates, key=lambda c: abs(c - seam), default=None)
            err = None if nearest is None else nearest - seam
            if err is not None and abs(err) <= win:
                hit += 1
                errors.append(err)
            if win == WINDOWS[-1]:
                matched_by_seam.append((seam, nearest))
        per_window[f"recall@{win}"] = f"{hit}/{len(seams)}"
        per_window[f"mae@{win}"] = round(statistics.fmean(abs(e) for e in errors), 3) if errors else None
        per_window[f"bias@{win}"] = round(statistics.fmean(errors), 3) if errors else None
        # 覆盖率按「窗口并集」算，先排序再合并，避免相邻候选重复计入
        per_window[f"coverage@{win}"] = round(
            100.0 * _covered_seconds(candidates, win, duration) / duration, 1
        )

    # 精确率：一个候选只认一条接缝（贪心按时间序，避免两个候选挤同一条接缝刷命中率）
    claimed: List[float] = []
    for seam in seams:
        pool = [c for c in candidates if c not in claimed and abs(c - seam) <= WINDOWS[-1]]
        if pool:
            claimed.append(min(pool, key=lambda c: abs(c - seam)))
    precision = round(100.0 * len(claimed) / len(candidates), 1) if candidates else None
    false_positives = len(candidates) - len(claimed)
    return {
        "candidates": len(candidates),
        "precision@1.0": precision,
        "fp_per_15s": round(false_positives / (duration / 15.0), 2) if candidates else None,
        "per_seam": [(s, None if n is None else round(n, 3),
                      None if n is None else round(n - s, 3)) for s, n in matched_by_seam],
        **per_window,
    }


NARROW_WINDOWS: Tuple[float, ...] = (0.5, 1.0, 2.0, 5.0)
# 产品实际用的吸附窗（frontend/src/lib/sections.ts 的 SNAP_WINDOW_SEC）：
# 收窄是在「已经能吸上的候选」上做减法，所以打分必须以它为准
SNAP_WIN = 0.5


def _covered_seconds(points: Iterable[float], win: float, duration: float) -> float:
    """±win 落在至少一个点上的时间总长（区间先排序再合并，相邻点不重复计）。"""
    spans = sorted((max(0.0, p - win), min(duration, p + win)) for p in points)
    total = 0.0
    cursor = -1.0
    for lo, hi in spans:
        if lo > cursor:
            total += hi - lo
            cursor = hi
        elif hi > cursor:
            total += hi - cursor
            cursor = hi
    return total


def narrow_score(det: List[float], dl: List[float], seams: List[float]) -> Dict[str, Any]:
    """把「用 DL 段边界过滤检测候选」当成一次裁剪来打分 —— 这是**已被否决**的方案，
    留着是为了让否决它有数可依，而不是凭感觉。

    关心的不是 DL 自己准不准（score() 已经量过），而是过滤之后**检测器本来吸得上的真接缝
    还剩几条** —— 收窄的全部风险都在这里：DL 的定位误差可以大到把真边界挪开好几秒
    （dlp-B 上模型第二个边界 96.99 vs 真值 92.567，差 4.42 s），窗口一窄就把真候选连坐删掉。
    前端因此改成并集（mergeSnapPoints），过滤只在候选多到看不过来时才考虑。
    """
    coverable = [s for s in seams if any(abs(c - s) <= SNAP_WIN for c in det)]
    out: Dict[str, Any] = {
        "det_candidates": len(det),
        "dl_boundaries": len(dl),
        "coverable_seams": len(coverable),
        "survivable_without_dl": len(seams),
    }
    for w in NARROW_WINDOWS:
        kept = [c for c in det if any(abs(c - b) <= w for b in dl)]
        if not kept:
            kept = det  # 一个不剩就退回全量（当时的兜底，现在前端已经不用这条路径）
        survivors = sum(1 for s in coverable if any(abs(c - s) <= SNAP_WIN for c in kept))
        key = f"@{w}"
        out[key] = {
            "kept": len(kept),
            "kept_pct": round(100.0 * len(kept) / len(det), 1) if det else None,
            # 一条接缝只要还剩一个 ±0.5 s 内的候选就算活；贪心认领不会多算，
            # 因为两条相邻接缝各自有候选时才会同时计两条
            "seam_survival": f"{survivors}/{len(coverable)}",
            "precision": round(100.0 * survivors / len(kept), 1) if kept else None,
        }
    return out


def union_score(
    det: List[float], dl: List[float], seams: List[float], duration: float
) -> Dict[str, Any]:
    """前端实际采用的并集：吸附目标 = 检测候选 ∪ 模型段边界。

    量的是「±0.5 s 内有可吸点的接缝」从几条变成几条 —— 这才是用户能不能点中真接缝的数，
    以及为了这个提升多付了几个刻度、多盖了多长的时间轴（coverage 就是误吸的概率，
    加刻度不是免费的，这两个数要一起报）。
    """
    added = [b for b in dl if all(abs(b - c) > 0.05 for c in det)]
    before = sum(1 for s in seams if any(abs(c - s) <= SNAP_WIN for c in det))
    after = sum(1 for s in seams if any(abs(c - s) <= SNAP_WIN for c in det + added))
    return {
        "seams": len(seams),
        "coverable_before": before,
        "coverable_after": after,
        "points_before": len(det),
        "points_added": len(added),
        "covered_before_sec": round(_covered_seconds(det, SNAP_WIN, duration), 3),
        "covered_after_sec": round(_covered_seconds(det + added, SNAP_WIN, duration), 3),
    }


class RssSampler(threading.Thread):
    """轮询 `docker stats` 取分析容器的内存峰值；容器不在就静默跳过。"""

    def __init__(self, container: str = "museblocks-analyzer", interval: float = 2.0):
        super().__init__(daemon=True)
        self.container = container
        self.interval = interval
        self.peak_bytes = 0
        self._stop = threading.Event()

    def run(self) -> None:
        import subprocess

        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", self.container],
                    capture_output=True, text=True, timeout=10,
                ).stdout.strip()
            except Exception:
                return
            if out:
                used = out.split("/")[0].strip()
                self.peak_bytes = max(self.peak_bytes, _to_bytes(used))
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


_UNITS = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}


def _to_bytes(text: str) -> int:
    for suffix, factor in _UNITS.items():
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)].strip()) * factor)
    return int(float(text))


def run(manifest: List[Dict[str, Any]], rss: bool) -> Dict[str, Any]:
    sampler: Optional[RssSampler] = None
    if rss:
        sampler = RssSampler()
        sampler.start()

    rows = []
    for entry in manifest:
        name = entry["filename"]
        stem = name[:-4]
        seams = [float(s) for s in entry["seams"]]

        dl, dl_wall = request_json(f"/api/analyze_structure?clip_id={stem}")
        det, det_wall = request_json(
            "/api/structure/boundaries",
            {"audio_url": f"/static/output/{name}", "floor": 0.09, "gap_ms": 1000},
        )
        duration = float(dl.get("duration_sec") or entry["duration"])
        dl_bounds = [float(seg["start"]) for seg in dl.get("segments", [])[1:]]
        det_bounds = [float(c) for c in det.get("candidates_sec", [])]

        row = {
            "file": name,
            "duration_sec": duration,
            "seams": seams,
            "dl": {
                "degraded": dl.get("degraded"),
                "detail": dl.get("detail"),
                "model": dl.get("model"),
                "route": (dl.get("backend") or {}).get("route"),
                "analyze_ms": dl.get("analyze_ms"),
                "http_wall_s": round(dl_wall, 1),
                "labels": [seg["label"] for seg in dl.get("segments", [])],
                "segments": dl.get("segments", []),
                "metrics": score(dl_bounds, seams, duration),
            },
            "det": {
                "compute_ms": det.get("compute_ms"),
                "http_wall_s": round(det_wall, 2),
                "metrics": score(det_bounds, seams, duration),
            },
            "narrow": narrow_score(det_bounds, dl_bounds, seams),
            "union": union_score(det_bounds, dl_bounds, seams, duration),
        }
        if duration and abs(duration - float(entry["duration"])) > 0.01:
            row["duration_mismatch"] = [entry["duration"], duration]
        rows.append(row)
        print(
            f"{name:38s} dur={duration:8.3f}s "
            f"dl: cand={row['dl']['metrics']['candidates']:2d} "
            f"recall@0.5={row['dl']['metrics']['recall@0.5']} "
            f"mae@1.0={row['dl']['metrics']['mae@1.0']} "
            f"ms={row['dl']['analyze_ms']} | "
            f"det: cand={row['det']['metrics']['candidates']:2d} "
            f"recall@0.5={row['det']['metrics']['recall@0.5']} "
            f"mae@1.0={row['det']['metrics']['mae@1.0']}"
        )
        if dl.get("degraded"):
            print(f"   ↳ 降级：{dl.get('detail')}")
        print(
            "   收窄 "
            + " ".join(
                f"±{w}s→{row['narrow'][f'@{w}']['kept']}个/缝{row['narrow'][f'@{w}']['seam_survival']}"
                for w in NARROW_WINDOWS
            )
        )

    if sampler:
        sampler.stop()
        if sampler.peak_bytes:
            print(f"\nanalyzer 峰值内存（采样自 docker stats）≈ {sampler.peak_bytes / 1048576:.1f} MB")
    return aggregate(rows, sampler.peak_bytes if sampler else 0)


def aggregate(rows: List[Dict[str, Any]], peak_bytes: int) -> Dict[str, Any]:
    """把每条接缝摊平再统计：按文件平均会把「接缝多的长成品」和「接缝少的短成品」同权看待。"""
    seams_total = sum(len(row["seams"]) for row in rows)
    duration_total = round(sum(row["duration_sec"] for row in rows), 3)
    out: Dict[str, Any] = {
        "files": len(rows),
        "seams": seams_total,
        "duration_total_sec": duration_total,
        "analyzer_peak_mb": round(peak_bytes / 1048576, 1) if peak_bytes else None,
        "rows": rows,
    }
    for side in ("dl", "det"):
        merged: Dict[str, Any] = {}
        for win in WINDOWS:
            hits = sum(int(row[side]["metrics"][f"recall@{win}"].split("/")[0]) for row in rows)
            merged[f"recall@{win}"] = f"{hits}/{seams_total}"
        mae_files = [row[side]["metrics"]["mae@1.0"] for row in rows
                     if row[side]["metrics"]["mae@1.0"] is not None]
        bias_files = [row[side]["metrics"]["bias@1.0"] for row in rows
                      if row[side]["metrics"]["bias@1.0"] is not None]
        cand_total = sum(row[side]["metrics"]["candidates"] for row in rows)
        # 逐文件已按「一个候选只认一条接缝」算过精确率，汇总时按候选数加权还原分子，
        # 不能直接把 per_seam 里命中的候选相加 —— 那会把两条接缝抢同一个候选的情况重复计
        claimed_total = round(
            sum(
                row[side]["metrics"]["precision@1.0"] * row[side]["metrics"]["candidates"] / 100.0
                for row in rows
                if row[side]["metrics"]["precision@1.0"] is not None
            )
        )
        merged["candidates_total"] = cand_total
        merged["recall@1.0_files"] = f"{len(mae_files)}/{len(rows)}"
        merged["mae@1.0"] = round(statistics.fmean(mae_files), 3) if mae_files else None
        merged["bias@1.0"] = round(statistics.fmean(bias_files), 3) if bias_files else None
        merged["precision@1.0"] = round(100.0 * claimed_total / cand_total, 1) if cand_total else None
        merged["fp_per_15s"] = round(
            (cand_total - claimed_total) / (duration_total / 15.0), 2
        ) if cand_total else None
        if side == "dl":
            merged["analyze_ms_total"] = sum(row["dl"]["analyze_ms"] or 0 for row in rows)
            merged["degraded_files"] = sum(1 for row in rows if row["dl"]["degraded"])
        out[side] = merged

    # 收窄汇总：候选减了多少 vs 真接缝掉了几条，两个数必须一起看
    det_cands = sum(row["narrow"]["det_candidates"] for row in rows)
    coverable = sum(row["narrow"]["coverable_seams"] for row in rows)
    narrow: Dict[str, Any] = {"det_candidates": det_cands, "coverable_seams": coverable}
    for w in NARROW_WINDOWS:
        kept = sum(row["narrow"][f"@{w}"]["kept"] for row in rows)
        survivors = sum(int(row["narrow"][f"@{w}"]["seam_survival"].split("/")[0]) for row in rows)
        narrow[f"@{w}"] = {
            "kept": f"{kept}/{det_cands}",
            "kept_pct": round(100.0 * kept / det_cands, 1) if det_cands else None,
            "seam_survival": f"{survivors}/{coverable}",
            "precision": round(100.0 * survivors / kept, 1) if kept else None,
        }
    out["narrow"] = narrow
    out["union"] = {
        "points_before": sum(row["union"]["points_before"] for row in rows),
        "points_added": sum(row["union"]["points_added"] for row in rows),
        "coverable_before": sum(row["union"]["coverable_before"] for row in rows),
        "coverable_after": sum(row["union"]["coverable_after"] for row in rows),
        "seams": seams_total,
        # 覆盖按秒汇总再除以总时长，等价于「随手一点被吸附的概率」
        "coverage_before_pct": round(
            100.0 * sum(row["union"]["covered_before_sec"] for row in rows) / duration_total, 1
        ),
        "coverage_after_pct": round(
            100.0 * sum(row["union"]["covered_after_sec"] for row in rows) / duration_total, 1
        ),
    }
    return out


def markdown(out: Dict[str, Any]) -> str:
    lines = [
        "| 检测器 | 候选数 | 召回@0.5s | 召回@1.0s | MAE@1.0s | 偏差@1.0s | 精确率@1.0s | 假候选/15s |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for side, label in (("dl", "allin1 结构分段"), ("det", "谱形状新颖度")):
        m = out[side]
        lines.append(
            f"| {label} | {m['candidates_total']} | {m['recall@0.5']} | {m['recall@1.0']} | "
            f"{m['mae@1.0']} s | {m['bias@1.0']} s | {m['precision@1.0']}% | {m['fp_per_15s']} |"
        )
    lines.append(
        f"\n（{out['files']} 条成品 / {out['seams']} 条毫秒真值接缝 / 共 {out['duration_total_sec']} s；"
        f"DL 合计 {out['dl'].get('analyze_ms_total')} ms，降级文件 {out['dl'].get('degraded_files')} 个）"
    )
    lines.append(
        "\n| DL 收窄窗 | 保留候选 | 真接缝存活（±0.5 s 吸附窗内） | 收窄后精确率 |\n| --- | --- | --- | --- |"
    )
    for w in NARROW_WINDOWS:
        n = out["narrow"][f"@{w}"]
        lines.append(
            f"| ±{w} s | {n['kept']}（{n['kept_pct']}%） | {n['seam_survival']} | {n['precision']}% |"
        )
    lines.append(
        f"\n（检测器全量 {out['narrow']['det_candidates']} 个候选、"
        f"{out['narrow']['coverable_seams']}/{out['seams']} 条接缝本来就有 ±0.5 s 内的候选可吸）"
    )
    u = out["union"]
    lines.append(
        f"\n并集（前端实采）：±{SNAP_WIN} s 可吸接缝 {u['coverable_before']}/{u['seams']} → "
        f"{u['coverable_after']}/{u['seams']}，刻度数 {u['points_before']} → "
        f"{u['points_before'] + u['points_added']}（+{u['points_added']} 个模型边界），"
        f"代价是 ±{SNAP_WIN} s 覆盖率 {u['coverage_before_pct']}% → {u['coverage_after_pct']}%"
    )
    return "\n".join(lines)


def main() -> int:
    global API_BASE

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=API_BASE, help="后端地址")
    parser.add_argument("--build-probes", action="store_true", help="重拼探测混合并写清单")
    parser.add_argument("--rss", action="store_true", help="采样 analyzer 容器峰值内存")
    parser.add_argument("--out", default="", help="把完整报告写成 JSON")
    parser.add_argument("--markdown", action="store_true", help="额外打印 README 可直接粘的表格")
    args = parser.parse_args()
    API_BASE = args.base.rstrip("/")

    manifest = build_probes() if args.build_probes else json.loads(MANIFEST.read_text(encoding="utf-8"))
    out = run(manifest, rss=args.rss)
    print("\n=== 汇总 ===")
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, ensure_ascii=False, indent=2))
    if args.markdown:
        print("\n" + markdown(out))
    if args.out:
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"报告写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
