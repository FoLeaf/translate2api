# -*- coding: utf-8 -*-
"""B站 Index-Translate 服务商：OpenAI 兼容协议反向代理。

关键点：B站网关见到非自家域名的 Origin 头会直接 412（浏览器扩展直连必挂）。
云端代理在服务端把 Origin/Referer 等头剥掉再转发，等于把“PS1 补丁”做在了云端，
ReadFrog 直填本服务地址即可，浏览器侧零改动。
"""
from __future__ import annotations

import json
import time

import httpx

from .. import config, db
from .base import ProxyResult, TranslationProvider, register

# 不向上游转发的请求头：origin 是 412 的根因，其余是逐跳头/会暴露代理的头
STRIP_REQ = {
    "origin", "referer", "host", "cookie", "content-length", "connection",
    "keep-alive", "transfer-encoding", "te", "upgrade", "proxy-authorization",
    "proxy-connection", "accept-encoding", "x-forwarded-for", "x-forwarded-host",
    "x-forwarded-proto", "x-real-ip", "true-client-ip", "cdn-loop",
}
STRIP_RESP = {
    "content-length", "transfer-encoding", "connection", "keep-alive",
    "alt-svc", "server", "date",
}
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"

# 共享连接池：避免每个请求都向上游重建 TCP+TLS（整页翻译是并发小请求，握手开销占比高）
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=300, write=60, pool=15),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=20,
                                keepalive_expiry=120),
        )
    return _client


def _err(status: int, msg: str) -> ProxyResult:
    return ProxyResult(status, [("content-type", "application/json; charset=utf-8")],
                       json.dumps({"error": {"message": msg, "type": "proxy_error"}},
                                  ensure_ascii=False).encode())


@register
class BilibiliProvider(TranslationProvider):
    id = "bilibili"
    name = "B站 Index-Translate"
    protocols = ["openai"]
    login_supported = True
    desc = "B站免费开放 API（Index-Translate-35B-A3B，OpenAI 兼容，无需 Key）。代理已自动剥离 Origin 头规避 412。"

    async def status(self) -> dict:
        cred = db.get_credential("bilibili")
        cred_info = None
        if cred:
            cred_info = {"name": cred["name"], "status": cred["status"],
                         "updated_at": cred["updated_at"],
                         "account": cred["data"].get("account", "")}
        return {"ok": True, "detail": "免费 API 无需登录即可用"
                + ("；已绑定B站账号（可选，仅作备用）" if cred else ""),
                "credential": cred_info}

    async def openai_proxy(self, method: str, path: str, query: str,
                           headers: dict[str, str], body: bytes | None) -> ProxyResult:
        url = config.BILIBILI_UPSTREAM + "/" + path.lstrip("/")
        if query:
            url += "?" + query

        fwd = {k: v for k, v in headers.items() if k.lower() not in STRIP_REQ}
        fwd.setdefault("User-Agent", DEFAULT_UA)
        fwd["Accept-Encoding"] = "identity"
        # 可选：带上后台扫码登录得到的B站 Cookie（当前上游不需要，留着以备官方收紧）
        cred = db.get_credential("bilibili")
        if cred and cred["status"] == "active" and cred["data"].get("cookie"):
            fwd["Cookie"] = cred["data"]["cookie"]

        client = _get_client()
        try:
            req = client.build_request(method, url, headers=fwd, content=body)
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            return _err(502, "upstream network error: %s" % e)

        out_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in STRIP_RESP]

        async def stream():
            try:
                async for chunk in resp.aiter_raw():
                    if chunk:
                        yield chunk
            finally:
                await resp.aclose()

        return ProxyResult(resp.status_code, out_headers, stream())

    async def test(self) -> dict:
        try:
            t0 = time.time()
            res = await self.openai_proxy("GET", "v1/models", "",
                                          {"Accept": "application/json"}, None)
            body = b""
            async for chunk in res.body:
                body += chunk
            ok = b"Index-Translate" in body
            return {"ok": ok, "detail": ("上游正常（%.1fs）: " % (time.time() - t0))
                    + body[:80].decode("utf-8", "replace")}
        except Exception as e:
            return {"ok": False, "detail": str(e)}
