"""同业务键对齐：双边金额比对，落对齐差 + 悬账单。

多源左轨形态（复合/合并）与独占锁主链落在 modules/多源左轨串扰；
本服务保持对外签名不变，基线 happy path 默认口径不变。
"""
from __future__ import annotations
from app.modules.多源左轨串扰 import 跑对齐  # noqa: F401
