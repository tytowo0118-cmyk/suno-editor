"""madmom.models 的中性替身，只为补上 allin1 依赖链里缺失的那一层。

allin1 用到 madmom 的只有 spectrogram 的四个处理器和 postprocessing/metrical.py 里的
DBNDownBeatTrackingProcessor（纯 Viterbi 后处理），不加载任何 madmom 神经网络权重。
但 madmom@main 的 `madmom/__init__.py` 顶层就 `from . import ... models ...`，而
madmom/models 是 gitlink 子模块（CPJKU/madmom_models，约 34 MB 权重）——
用 archive tarball 装 madmom 时那个目录根本不会进 site-packages，import madmom 直接失败。
PyPI 的 0.16.1 确实打包了 models/__init__.py，可它同时有 98 处 np.float / np.int 这类
numpy 1.24 起删除的别名，跟本镜像钉住的 numpy 1.26.4 装不到一起。

所以这里放一个只定义 `path` 的替身：顶层 import 通过，方法体内按需
`from ..models import DOWNBEATS_BLSTM` 一旦真被调用会明确抛 ImportError，
而不是静默用一份不存在的权重。
"""

import os

path = os.path.dirname(__file__)
