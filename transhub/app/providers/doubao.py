# -*- coding: utf-8 -*-
"""豆包翻译服务商：DeepLX 协议出口，上游为豆包网页翻译接口（SSE 三事件流）。

协议要点（与本地版 doubao2deeplx.py 一致，实测核对）：
- POST {upstream}/samantha/plugin/stream_article_translate
- 请求体 raw_text(数组) / target_lang / translate_service(字符串) / scene(必须整数)
- 响应 text/event-stream：event:json(增量帧取最长) / event:err(整单失败) / event:done
- 未登录: HTTP 200 + JSON code=710012001
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .. import config, db
from .base import ProviderError, TranslationProvider, register

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
STREAM_PATH = "/samantha/plugin/stream_article_translate"
MAX_CHARS = 9000

LANG_MAP = {
    "ZH": "zh", "ZH-HANS": "zh", "ZH-HANT": "zh-Hant", "ZH-TW": "zh-Hant",
    "EN": "en", "JA": "ja", "KO": "ko", "DE": "de", "FR": "fr", "ES": "es",
    "PT": "pt", "RU": "ru", "IT": "it", "AR": "ar", "ID": "id", "VI": "vi",
    "TH": "th", "MS": "ms", "TL": "fil", "FIL": "fil", "UZ": "uz",
}
KNOWN_LOW = {"en", "ar", "de", "es", "es-es", "fil", "fr", "id", "it", "ja", "ko",
             "ms", "pt", "ru", "th", "uz", "vi", "zh", "zh-hant"}

# 上游调用串行化：避免共享 cookie 状态与并发触发风控
_upstream_lock = asyncio.Lock()


def to_doubao_lang(code) -> str | None:
    if not code:
        return None
    raw = str(code).strip()
    up = raw.upper()
    if up in LANG_MAP:
        return LANG_MAP[up]
    low = raw.lower()
    if low in ("zh-cn", "zh-hans", "zh-sg"):
        return "zh"
    if low in ("zh-tw", "zh-hk", "zh-hant", "zh-mo"):
        return "zh-Hant"
    if low in KNOWN_LOW:
        return "zh-Hant" if low == "zh-hant" else ("es-ES" if low == "es-es" else low)
    return None


def parse_sse_events(raw_text: str):
    events, event_name, data_lines = [], None, []
    for line in raw_text.split("\n"):
        line = line.rstrip("\r")
        if line == "":
            if data_lines:
                events.append((event_name or "message", "\n".join(data_lines)))
            event_name, data_lines = None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        events.append((event_name or "message", "\n".join(data_lines)))
    return events


def read_doubao_stream(raw: bytes, content_type: str) -> dict[int, str]:
    """返回 {index: 译文}；JSON 错误体与 event:err 抛 ProviderError。"""
    head = raw.lstrip()[:1]
    if "application/json" in (content_type or "").lower() or head == b"{":
        try:
            frame = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise ProviderError("响应既不是 SSE 也不是 JSON: " + raw[:120].decode("utf-8", "replace"))
        code = frame.get("code")
        if isinstance(code, int) and code != 0:
            expired = code == 710012001
            raise ProviderError(
                (frame.get("msg") or frame.get("message") or "未知错误"),
                code=code, http_status=401 if expired else 502,
                credential_expired=expired)
        raise ProviderError("返回了非流式 JSON: " + raw[:200].decode("utf-8", "replace"))

    items, saw_done = {}, False
    for name, data in parse_sse_events(raw.decode("utf-8", "replace")):
        if name == "done":
            saw_done = True
            continue
        if name == "err":
            try:
                f = json.loads(data)
                raise ProviderError(f.get("msg") or "流式错误帧", code=f.get("code", 710020702))
            except json.JSONDecodeError:
                raise ProviderError("流式错误帧: " + data[:120], code=710020702)
        if name != "json":
            continue
        try:
            frame = json.loads(data)
        except json.JSONDecodeError:
            continue
        code = frame.get("code")
        if isinstance(code, int) and code != 0:
            expired = code == 710012001
            raise ProviderError(frame.get("msg") or "服务端错误", code=code,
                                http_status=401 if expired else 502,
                                credential_expired=expired)
        fd = frame.get("data")
        if not isinstance(fd, dict):
            continue
        for it in fd.get("items") or []:
            if not isinstance(it, dict):
                continue
            idx, res = it.get("index"), it.get("res")
            if isinstance(idx, int) and isinstance(res, str):
                if idx not in items or len(res) > len(items[idx]):
                    items[idx] = res

    if not items:
        raise ProviderError("翻译流结束但没有返回任何译文" + ("（收到 done）" if saw_done else "（未收到 done）"))
    return items


def split_chunks(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > MAX_CHARS and buf:
            chunks.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        chunks.append(buf)
    return chunks


@register
class DoubaoProvider(TranslationProvider):
    id = "doubao"
    name = "豆包翻译"
    protocols = ["deeplx"]
    login_supported = True
    desc = "豆包网页翻译接口（火山/豆包AI/微软三引擎），需豆包账号 Cookie，后台扫码登录获取。"

    async def status(self) -> dict:
        cred = db.get_credential("doubao")
        if not cred:
            return {"ok": False, "detail": "未配置 Cookie：请到「服务商」页扫码登录豆包",
                    "credential": None}
        data = cred["data"]
        age_days = (time.time() - data.get("captured_at", cred["updated_at"])) / 86400
        ok = cred["status"] == "active"
        return {
            "ok": ok,
            "detail": ("Cookie 正常" if ok else "Cookie 已失效（上游返回登录过期），请重新扫码登录")
                      + f"，更新于 {age_days:.1f} 天前",
            "credential": {"name": cred["name"], "status": cred["status"],
                           "updated_at": cred["updated_at"],
                           "account": data.get("account", "")},
        }

    async def deeplx_translate(self, text: str, source_lang: str, target_lang: str) -> str:
        target = to_doubao_lang(target_lang)
        if not target:
            raise ProviderError("不支持的目标语言: %r（豆包支持 19 种语言码）" % target_lang,
                                code=400, http_status=400)
        cred = db.get_credential("doubao")
        if not cred:
            raise ProviderError("未配置豆包 Cookie：请到后台扫码登录", code=710012001,
                                http_status=401, credential_expired=True)
        cookie = cred["data"].get("cookie", "")
        engine = str(db.get_setting("doubao_engine", "1"))
        scene = int(db.get_setting("doubao_scene", 1))

        out = []
        async with _upstream_lock:
            for chunk in split_chunks(text):
                body = json.dumps({
                    "raw_text": [chunk],
                    "target_lang": target,
                    "translate_service": engine,
                    "scene": scene,
                    "frontend_source": 1,
                }).encode("utf-8")
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                    "User-Agent": UA,
                    "Referer": config.DOUBAO_UPSTREAM + "/",
                    "Cookie": cookie,
                }
                async with httpx.AsyncClient(timeout=httpx.Timeout(180)) as client:
                    try:
                        resp = await client.post(config.DOUBAO_UPSTREAM + STREAM_PATH,
                                                 content=body, headers=headers)
                    except httpx.HTTPError as e:
                        raise ProviderError("网络错误: %s" % e, code=-1, http_status=504)
                try:
                    items = read_doubao_stream(resp.content, resp.headers.get("content-type", ""))
                except ProviderError as e:
                    if e.credential_expired:
                        db.set_credential_status("doubao", "expired", cred["name"])
                    raise
                if 0 not in items:
                    raise ProviderError("该批没有 index=0 的译文，收到下标: %s" % sorted(items))
                out.append(items[0])
        return "\n".join(out)

    async def test(self) -> dict:
        s = await self.status()
        if not s["ok"]:
            return {"ok": False, "detail": s["detail"]}
        try:
            t0 = time.time()
            res = await self.deeplx_translate("Hello world", "EN", "ZH")
            return {"ok": True, "detail": f"翻译成功（{time.time()-t0:.1f}s）: {res[:60]}"}
        except ProviderError as e:
            return {"ok": False, "detail": str(e)}
