# -*- coding: utf-8 -*-
"""服务商抽象与注册表。

新增一个翻译服务商只需两步：
1. 在 app/providers/ 下新建模块，继承 TranslationProvider 并实现对应协议方法；
2. 用 @register 装饰类。模块会被 pkgutil 自动发现，路由/后台页面自动出现。

协议方法按需实现（声明在 protocols 里）：
- deeplx_translate(text, source_lang, target_lang) -> str   （DeepLX 协议）
- openai_proxy(method, path, query, headers, body) -> ProxyResult （OpenAI 兼容透传）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, ClassVar


class ProviderError(Exception):
    """服务商错误。code 为上游错误码，http_status 为返回给客户端的状态码。"""

    def __init__(self, msg: str, *, code=None, http_status: int = 502,
                 credential_expired: bool = False):
        super().__init__(msg)
        self.msg = msg
        self.code = code
        self.http_status = http_status
        self.credential_expired = credential_expired


class ProxyResult:
    """OpenAI 兼容透传的返回：body 可以是 bytes 或异步字节迭代器（流式）。"""

    def __init__(self, status: int, headers: list[tuple[str, str]],
                 body: bytes | AsyncIterator[bytes]):
        self.status = status
        self.headers = headers
        self.body = body


class TranslationProvider(ABC):
    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    protocols: ClassVar[list[str]] = []      # 支持的对外协议: "deeplx" / "openai"
    login_supported: ClassVar[bool] = False  # 是否有登录流（自动取 Cookie）
    desc: ClassVar[str] = ""

    @abstractmethod
    async def status(self) -> dict:
        """{ok: bool, detail: str, credential: {...} | None}"""

    async def deeplx_translate(self, text: str, source_lang: str, target_lang: str) -> str:
        raise NotImplementedError("该服务商不支持 DeepLX 协议")

    async def openai_proxy(self, method: str, path: str, query: str,
                           headers: dict[str, str], body: bytes | None) -> ProxyResult:
        raise NotImplementedError("该服务商不支持 OpenAI 兼容协议")

    async def test(self) -> dict:
        """后台“测试”按钮：默认只看状态。"""
        s = await self.status()
        return {"ok": s.get("ok"), "detail": s.get("detail")}


REGISTRY: dict[str, TranslationProvider] = {}


def register(cls):
    REGISTRY[cls.id] = cls()
    return cls


def get(provider_id: str) -> TranslationProvider | None:
    return REGISTRY.get(provider_id)
