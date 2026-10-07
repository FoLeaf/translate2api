# -*- coding: utf-8 -*-
"""login 包：自动发现并注册所有登录流模块。"""
from .base import FLOWS, get_flow, register_flow  # noqa: F401

import importlib
import pkgutil
from . import __path__ as _pkg_path

for _m in pkgutil.iter_modules(_pkg_path):
    if _m.name != "base":
        importlib.import_module(f"{__name__}.{_m.name}")
