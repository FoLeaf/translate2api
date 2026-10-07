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

# 共享连接池：上游串行调用，连接常驻复用，省去每个请求、每个分块重建 TCP+TLS
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=180, write=60, pool=15),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4,
                                keepalive_expiry=120),
        )
    return _client


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

# ---------------- 凑批发上游：整页翻译是多段小请求，上游 raw_text 本身是数组，
# 实测一次带 8 段与带 1 段耗时几乎相同（2.2 秒对 1.9 秒），按 index 全量返回译文
BATCH_WINDOW = 0.3       # 凑批窗口（秒）：首个请求到达后等这么久凑同批
BATCH_MAX_ITEMS = 8      # 单次上游调用最多段数
BATCH_MAX_CHARS = 12000  # 单次上游调用 raw_text 总字符上限

_batch_lock = asyncio.Lock()
_pending: dict[str, list[dict]] = {}  # 按目标语言分组的待凑批队列


async def _upstream_call(texts: list[str], target: str) -> dict[int, str]:
    """串行调用上游一次带多段 raw_text，返回 {index: 译文}。"""
    cred = db.get_credential("doubao")
    if not cred:
        raise ProviderError("未配置豆包 Cookie：请到后台扫码登录", code=710012001,
                            http_status=401, credential_expired=True)
    cookie = cred["data"].get("cookie", "")
    engine = str(db.get_setting("doubao_engine", "1"))
    scene = int(db.get_setting("doubao_scene", 1))
    body = json.dumps({
        "raw_text": texts,
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
    async with _upstream_lock:
        try:
            resp = await _get_client().post(config.DOUBAO_UPSTREAM + STREAM_PATH,
                                            content=body, headers=headers)
        except httpx.HTTPError as e:
            raise ProviderError("网络错误: %s" % e, code=-1, http_status=504)
    try:
        items = read_doubao_stream(resp.content, resp.headers.get("content-type", ""))
    except ProviderError as e:
        if e.credential_expired:
            db.set_credential_status("doubao", "expired", cred["name"])
        raise
    return items


def _schedule_flush(target: str) -> None:
    loop = asyncio.get_running_loop()
    loop.call_later(BATCH_WINDOW, lambda: asyncio.ensure_future(_flush(target)))


async def _translate_batched(text: str, target: str) -> str:
    """普通段落走凑批：等待窗口内同语言的段落合成一次上游调用。"""
    fut = asyncio.get_running_loop().create_future()
    entry = {"text": text, "fut": fut}
    async with _batch_lock:
        lst = _pending.setdefault(target, [])
        was_empty = not lst
        lst.append(entry)
        if was_empty:
            _schedule_flush(target)
    return await fut


async def _flush(target: str) -> None:
    async with _batch_lock:
        lst = _pending.get(target) or []
        batch: list[dict] = []
        chars = 0
        while lst and len(batch) < BATCH_MAX_ITEMS:
            chars += len(lst[0]["text"]) + 1
            if batch and chars > BATCH_MAX_CHARS:
                break
            batch.append(lst.pop(0))
        if lst:
            _schedule_flush(target)
    if not batch:
        return
    try:
        items = await _upstream_call([e["text"] for e in batch], target)
        for i, e in enumerate(batch):
            if e["fut"].done():
                continue
            if i in items:
                e["fut"].set_result(items[i])
            else:
                e["fut"].set_exception(
                    ProviderError("该批缺少 index=%d 的译文，收到下标: %s" % (i, sorted(items))))
    except Exception as ex:
        for e in batch:
            if not e["fut"].done():
                e["fut"].set_exception(ex)


    async def deeplx_translate(self, text: str, source_lang: str, target_lang: str) -> str:
        target = to_doubao_lang(target_lang)
        if not target:
            raise ProviderError("不支持的目标语言: %r（豆包支持 19 种语言码）" % target_lang,
                                code=400, http_status=400)
        chunks = split_chunks(text)
        if len(chunks) == 1:
            # 普通段落（整页翻译的主体）走凑批，多段合一次上游调用
            return await _translate_batched(chunks[0], target)
        # 超长文本按行切块后逐块直发，不凑批，避免单次调用过大
        out = []
        for chunk in chunks:
            items = await _upstream_call([chunk], target)
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
