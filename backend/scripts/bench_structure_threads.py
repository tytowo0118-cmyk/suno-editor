"""结构分析容器的线程配比标定：把「一首歌要等多久」拆成可归因的两段。

allin1.analyze() 无条件先跑 demucs 分轨，再提谱特征，最后才轮到 DiNAT 推理，
所以决定等待时间的是分轨，不是模型。线程配比也是两套独立开关：

- `OMP_NUM_THREADS` 是**给子进程看的**：allin1 用 subprocess 起 `python -m demucs.separate`，
  父进程里的 `torch.set_num_threads()` 对它完全无效；
- `AIO_THREADS` → `torch.set_num_threads()` 只管父进程内的那次 DiNAT 推理。

numpy 侧的 OpenBLAS 与 torch 的 OpenMP 各自开池会互相踩（容器日志里那一串
`OpenBLAS Warning : Detect OpenMP Loop and this application may hang` 就是这件事），
所以 BLAS 线程钉到 1、把核让给 torch，是本脚本要验证的假设。

用法（在分析容器里跑，宿主机跑不出真实数值）：

    HF_HUB_OFFLINE=1 docker exec -i -e HF_HUB_OFFLINE=1 museblocks-analyzer \
        python /app/scripts/bench_structure_threads.py /app/static/output/<probe>.wav

`HF_HUB_OFFLINE=1` 不是可选项：权重已经在 analyzer_cache 卷里，但 allin1 加载前还要向
huggingface.co 发一次 metadata HEAD，本机代理随机掐 TLS，那一发 HEAD 会以 SSLEOFError
打死整条推理（第一轮实测就死在这里，demucs 都跑完了）。分析容器的 CMD 同样带这个变量。

只读音频、不花钱；每种配置都清掉 allin1 的结果 JSON 缓存，避免测到缓存命中。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any, Dict, List

# 变体：(名字, 传给子进程的线程 env)
VARIANTS: List[Dict[str, Any]] = [
    {"name": "omp4_blas1", "env": {"OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}},
    {"name": "omp8_blas1", "env": {"OMP_NUM_THREADS": "8", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}},
    {"name": "omp2_blas1", "env": {"OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}},
]


def wav_duration_sec(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def _clear(work: Path) -> None:
    for sub in ("results", "demix", "spec", "bench_demucs"):
        target = work / sub
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    # demucs 4.x 会在输出目录里留 meta，命中就跳过重算 —— 清掉才测得到真实分轨耗时
    (work / "bench_demucs").mkdir(parents=True, exist_ok=True)


def bench_demucs(path: Path, env: Dict[str, str], out_dir: Path) -> float:
    """只测 demucs 分轨：allin1 里那一步的等价命令（--device cpu，4 轨）。"""
    cmd = [
        sys.executable,
        "-m",
        "demucs.separate",
        "--name",
        "htdemucs",
        "--device",
        "cpu",
        "-o",
        str(out_dir),
        str(path),
    ]
    started = time.perf_counter()
    proc = subprocess.run(cmd, env={**os.environ, **env}, capture_output=True, text=True)
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        raise RuntimeError(f"demucs 失败 rc={proc.returncode}: {proc.stderr[-300:]}")
    return elapsed


def bench_full(path: Path, env: Dict[str, str], work: Path, model: str) -> float:
    """整条 allin1 流水线（分轨 + 谱 + 推理），父进程线程用 AIO_THREADS 那套。"""
    import allin1
    import torch

    torch.set_num_threads(int(env.get("AIO_THREADS", os.getenv("AIO_THREADS", "4"))))
    for key, value in env.items():
        os.environ[key] = value
    started = time.perf_counter()
    allin1.analyze(
        str(path),
        out_dir=work / "results",
        model=model,
        device="cpu",
        demix_dir=work / "demix",
        spec_dir=work / "spec",
        keep_byproducts=False,
        overwrite=True,
        multiprocess=False,
        visualize=False,
        sonify=False,
    )
    return time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser(description="结构分析容器线程配比标定")
    parser.add_argument("audio", help="探测 WAV（建议先跑最短的一条，一轮要十几分钟）")
    parser.add_argument(
        # 默认落在 bench 子树里：_clear() 会 rmtree 它的 results/demix/spec，
        # 指到生产目录就会把标定跑出来的结果缓存一起删掉
        "--work",
        default=os.getenv("STRUCTURE_WORK_DIR", "/data/cache/work") + "/bench",
    )
    parser.add_argument("--model", default=os.getenv("AIO_MODEL", "harmonix-fold0"))
    parser.add_argument("--skip-full", action="store_true", help="只测分轨，不跑整条流水线")
    parser.add_argument(
        "--only",
        default="",
        help="只跑这些变体（逗号分隔的名字），选定配比之后不必再把三个都等一遍",
    )
    args = parser.parse_args()

    path = Path(args.audio).resolve()
    duration = wav_duration_sec(path)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    wanted = {name.strip() for name in args.only.split(",") if name.strip()}
    variants = [v for v in VARIANTS if not wanted or v["name"] in wanted]
    print(f"probe: {path.name} dur={duration:.3f}s model={args.model} work={work}")
    print(f"variants: {[v['name'] for v in variants]}")

    rows: List[Dict[str, Any]] = []
    for variant in variants:
        env = dict(variant["env"])
        entry: Dict[str, Any] = {"name": variant["name"], "env": env}
        _clear(work)
        entry["demucs_s"] = round(bench_demucs(path, env, work / "bench_demucs"), 1)
        entry["demucs_ratio"] = round(entry["demucs_s"] / duration, 2)
        if not args.skip_full:
            _clear(work)
            env["AIO_THREADS"] = env["OMP_NUM_THREADS"]
            entry["full_s"] = round(bench_full(path, env, work, args.model), 1)
            entry["full_ratio"] = round(entry["full_s"] / duration, 2)
            entry["rest_s"] = round(entry["full_s"] - entry["demucs_s"], 1)
        rows.append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)

    print("\n=== 汇总（每行一首歌的墙钟秒 / 除以音频时长 = 倍率）===")
    for entry in rows:
        full = f" full={entry['full_s']}s({entry['full_ratio']}x)" if "full_s" in entry else ""
        print(f"{entry['name']:12s} demucs={entry['demucs_s']}s({entry['demucs_ratio']}x){full}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
