# -*- coding: utf-8 -*-
"""登录流抽象：自动获取 Cookie 的“类 OAuth”体验。

实现方在 login/ 包下新建模块，继承 LoginFlow 并用 @register_flow("provider_id")
装饰。后台页面会自动出现对应服务商的登录入口。

生命周期：start() -> (前端轮询 poll()) -> confirmed(凭据已入库) / expired / error
"""
from __future__ import annotations

from abc import ABC, abstractmethod

FLOWS: dict[str, "LoginFlow"] = {}


def register_flow(provider_id: str):
    def deco(cls):
        FLOWS[provider_id] = cls()
        return cls
    return deco


def get_flow(provider_id: str) -> LoginFlow | None:
    return FLOWS.get(provider_id)


class LoginFlow(ABC):
    #: 返回给前端展示的模式
    mode: str = "qr_svg"          # qr_svg: 直接渲染 SVG 二维码 | frame: 浏览器截图帧

    @abstractmethod
    async def start(self) -> dict:
        """发起登录。返回 {ok, mode, qr_svg?, detail}；失败 {ok: False, detail}。"""

    @abstractmethod
    async def poll(self) -> dict:
        """轮询状态。返回 {status: waiting|scanned|confirmed|expired|error, detail}。
        confirmed 时凭据必须已经写入 db.put_credential。"""

    async def frame(self) -> tuple[int, bytes] | None:
        """mode == frame 时提供 (seq, png_bytes)；qr_svg 模式返回 None。"""
        return None

    async def cancel(self) -> None:
        pass
