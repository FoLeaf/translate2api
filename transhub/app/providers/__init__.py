# -*- coding: utf-8 -*-
"""providers 包：import 时自动发现并注册所有服务商模块。

新增服务商 = 在本目录新建 .py 文件 + @register 装饰类，无需改任何注册代码。
"""
import importlib
import pkgutil

from .base import ProviderError, ProxyResult, TranslationProvider, REGISTRY, register, get  # noqa: F401

for _m in pkgutil.iter_modules(__path__):
    if _m.name != "base":
        importlib.import_module(f"{__name__}.{_m.name}")
