# -*- coding: utf-8 -*-
"""B站扫码登录：passport.bilibili.com 公开 QR API，无风控，纯服务端实现。

流程：generate 拿 url+qrcode_key -> 渲染 SVG 二维码 -> 前端展示 ->
轮询 poll -> code=0 确认 -> 从回调 URL 参数提取 SESSDATA/bili_jct/DedeUserID 存库。
"""
from __future__ import annotations

import io
import time
from urllib.parse import parse_qs, urlparse

import httpx
import qrcode
import qrcode.image.svg

from .. import db
from .base import LoginFlow, register_flow

GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"


@register_flow("bilibili")
class BilibiliQRFlow(LoginFlow):
    mode = "qr_svg"

    def __init__(self):
        self.qrcode_key: str | None = None
        self.created_at = 0.0
        self._status = "idle"
        self._detail = ""

    async def start(self) -> dict:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as client:
            r = await client.get(GEN_URL, params={"source": "transhub"})
        try:
            data = r.json().get("data") or {}
        except Exception:
            return {"ok": False, "detail": "generate 返回异常: " + r.text[:120]}
        url, key = data.get("url"), data.get("qrcode_key")
        if not url or not key:
            return {"ok": False, "detail": "generate 未返回二维码: " + r.text[:120]}
        self.qrcode_key = key
        self.created_at = time.time()
        self._status = "waiting"
        self._detail = ""

        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=12, border=2)
        buf = io.BytesIO()
        img.save(buf)
        svg = buf.getvalue().decode("utf-8")
        return {"ok": True, "mode": self.mode, "qr_svg": svg, "detail": "请用B站 App 扫码"}

    async def poll(self) -> dict:
        if not self.qrcode_key:
            return {"status": "error", "detail": "尚未发起登录"}
        if time.time() - self.created_at > 180:
            self._status = "expired"
            return {"status": "expired", "detail": "二维码已过期，请重新发起"}

        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as client:
            r = await client.get(POLL_URL, params={"qrcode_key": self.qrcode_key,
                                                   "source": "transhub"})
        try:
            data = r.json().get("data") or {}
        except Exception:
            return {"status": self._status or "waiting", "detail": "poll 返回异常"}

        code = data.get("code")
        if code == 86101:
            self._status = "waiting"
            return {"status": "waiting", "detail": "等待扫码"}
        if code == 86090:
            self._status = "scanned"
            return {"status": "scanned", "detail": "已扫码，请在手机上确认"}
        if code == 0 and data.get("url"):
            qs = parse_qs(urlparse(data["url"]).query)
            cookie = "; ".join(
                f"{k}={v[0]}" for k, v in qs.items()
                if k in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "Expires"))
            uid = (qs.get("DedeUserID") or [""])[0]
            db.put_credential("bilibili", {
                "cookie": cookie, "uid": uid, "captured_at": int(time.time()),
            })
            self._status = "confirmed"
            self.qrcode_key = None
            return {"status": "confirmed", "detail": f"登录成功（uid={uid}），Cookie 已自动保存"}
        self._status = "expired"
        return {"status": "expired", "detail": data.get("message") or "二维码已失效，请重新发起"}

    async def cancel(self) -> None:
        self.qrcode_key = None
        self._status = "idle"
