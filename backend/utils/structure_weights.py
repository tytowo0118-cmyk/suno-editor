"""模型权重预下载与自检。

只在 analyzer 容器里运行（主后端容器没有 torch/allin1）；镜像构建后由 CMD 先跑一次，
把 HF 上的 .pth 和 demucs 的 htdemucs bag 落进挂载卷，避免第一个真实请求替我们等下载。

实测体积：allin1 单个 fold = 1.40 MB（taejunkim/allinone，未 gated），
demucs htdemucs = 80.2 MB（dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th），
torch.hub 的缓存目录由 TORCH_HOME 决定（本镜像里 TORCH_HOME=/data/cache/torch）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


def _fold_files(model: str) -> List[str]:
    """模型名 → 需要哪些 .pth；直接读 allin1 的映射表，不在这里抄第二份。"""
    from allin1.models.loaders import ENSEMBLE_MODELS, NAME_TO_FILE

    if model in ENSEMBLE_MODELS:
        return [NAME_TO_FILE[name] for name in ENSEMBLE_MODELS[model]]
    if model not in NAME_TO_FILE:
        raise ValueError(f"未知模型名: {model}（可用: {sorted(NAME_TO_FILE)} + {sorted(ENSEMBLE_MODELS)}）")
    return [NAME_TO_FILE[model]]


def _download_allin1(model: str, local_files_only: bool) -> List[Dict[str, Any]]:
    from huggingface_hub import hf_hub_download, try_to_load_from_cache

    entries = []
    for filename in _fold_files(model):
        if local_files_only:
            path = try_to_load_from_cache("taejunkim/allinone", filename)
            if not isinstance(path, str):
                raise FileNotFoundError(f"权重未预下载: {filename}（cache={path}）")
        else:
            path = hf_hub_download(repo_id="taejunkim/allinone", filename=filename)
        entries.append(_entry("allin1/" + filename, Path(path)))
    return entries


def _download_demucs(local_files_only: bool) -> List[Dict[str, Any]]:
    model_name = os.getenv("DEMUCS_MODEL", "htdemucs")
    if local_files_only:
        root = Path(os.getenv("TORCH_HOME", str(Path.home() / ".cache" / "torch")))
        files = sorted((root / "hub" / "checkpoints").glob("*.th"))
        if not files:
            raise FileNotFoundError(f"demucs 权重未预下载（查找 {root}/hub/checkpoints/*.th）")
        return [_entry("demucs/" + f.name, f) for f in files]

    # get_model 会走 torch.hub.load_state_dict_from_url(check_hash=True)，顺带校验哈希
    from demucs.pretrained import get_model

    get_model(model_name)
    root = Path(os.getenv("TORCH_HOME", str(Path.home() / ".cache" / "torch")))
    files = sorted((root / "hub" / "checkpoints").glob("*.th"))
    return [_entry("demucs/" + f.name, f) for f in files]


def _entry(name: str, path: Path) -> Dict[str, Any]:
    return {"name": name, "path": str(path), "bytes": path.stat().st_size if path.is_file() else 0}


def ensure_weights(model: str, check_only: bool = False) -> Dict[str, Any]:
    entries = _download_allin1(model, check_only) + _download_demucs(check_only)
    manifest = {
        "model": model,
        "files": entries,
        "total_bytes": sum(entry["bytes"] for entry in entries),
        "check_only": check_only,
    }
    return manifest


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预下载 / 校验音乐结构分析权重")
    parser.add_argument("--check", action="store_true", help="只校验本地缓存，不下载")
    parser.add_argument("--model", default=os.getenv("AIO_MODEL", "harmonix-fold0"))
    parser.add_argument("--manifest", default="", help="把清单写到这个 JSON 路径")
    args = parser.parse_args(argv)

    manifest = ensure_weights(args.model, check_only=args.check)
    print(
        f"weights ok: model={manifest['model']} files={len(manifest['files'])} "
        f"total={manifest['total_bytes'] / 1048576:.1f} MB"
    )
    for entry in manifest["files"]:
        print(f"  {entry['bytes'] / 1048576:8.2f} MB  {entry['name']}")
    if args.manifest:
        Path(args.manifest).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
