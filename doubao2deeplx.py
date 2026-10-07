#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""豆包翻译 → DeepLX 协议本地适配器（给官方陪读蛙 Read Frog 的火狐版用）。

原理：
    官方陪读蛙原生支持「DeepLX」纯翻译服务商（自定义 baseURL），
    本适配器把 DeepLX 的请求翻译成豆包原生接口
    POST https://www.doubao.com/samantha/plugin/stream_article_translate
    （逆向协议来自 linux.do 帖子作者发布的 Read Frog 改版源码，已核对），
    并附带你的豆包登录 Cookie。

陪读蛙里的配置（选项 → API 服务商 → 新增 → 纯翻译服务商 DeepLX）：
    Base URL : http://127.0.0.1:1188/translate
    API Key  : 不用填（适配器不需要）

Cookie 获取（二选一）：
    1. 环境变量：  set DOUBAO_COOKIE=sessionid=xxx; sid_tt=xxx; uid_tt=xxx
    2. 文件：      把整串 Cookie 粘进本脚本同目录的 doubao-cookie.txt
                   （F12 → 网络 → 任一 www.doubao.com 请求 → 请求头 → Cookie 整行复制）
    文件每次请求都会重新读取，Cookie 过期后直接改文件即可，不用重启。

可选环境变量：
    DOUBAO_PORT=1188        监听端口
    DOUBAO_ENGINE=1         翻译引擎：0 火山引擎 / 1 豆包AI / 3 微软（默认 1）
    DOUBAO_SCENE=1          场景：1 整页 / 2 AI阅读器 / 3 划词 / 6 悬停（默认 1）
    DOUBAO_UPSTREAM=...     覆盖上游地址（调试用）

仅依赖 Python 3 标准库。仅供个人学习研究，勿用于商业或大规模并发。
"""
import http.server
import json
import os
import sys
import urllib.error
import urllib.request

# ----------------------------------------------------------------- 配置
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("DOUBAO_PORT", "1188"))
UPSTREAM = os.environ.get("DOUBAO_UPSTREAM", "https://www.doubao.com").rstrip("/")
ENGINE = os.environ.get("DOUBAO_ENGINE", "1")
SCENE = int(os.environ.get("DOUBAO_SCENE", "1"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.environ.get("DOUBAO_COOKIE_FILE", os.path.join(SCRIPT_DIR, "doubao-cookie.txt"))
STREAM_PATH = "/samantha/plugin/stream_article_translate"

# 豆包单批上限：≤100 段、≤10000 字符（帖子与源码一致）；适配器留安全余量
MAX_CHARS = 9000

# DeepLX 大写语言码（陪读蛙发的格式）→ 豆包语言码
LANG_MAP = {
    "ZH": "zh", "ZH-HANS": "zh", "ZH-HANT": "zh-Hant", "ZH-TW": "zh-Hant",
    "EN": "en", "JA": "ja", "KO": "ko", "DE": "de", "FR": "fr", "ES": "es",
    "PT": "pt", "RU": "ru", "IT": "it", "AR": "ar", "ID": "id", "VI": "vi",
    "TH": "th", "MS": "ms", "TL": "fil", "FIL": "fil", "UZ": "uz",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0")


class DoubaoError(Exception):
    def __init__(self, code, msg):
        super().__init__("[%s] %s" % (code, msg))
        self.code = code
        self.msg = msg


def log(*a):
    sys.stderr.write("[adapter] " + " ".join(str(x) for x in a) + "\n")


def load_cookie():
    env = os.environ.get("DOUBAO_COOKIE", "").strip()
    if env:
        return env
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    except OSError:
        return ""


def to_doubao_lang(code):
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
    # 已经是豆包码（zh / zh-Hant / es-ES ...）就直接用
    known = {"en", "ar", "de", "es", "es-es", "fil", "fr", "id", "it", "ja", "ko",
             "ms", "pt", "ru", "th", "uz", "vi", "zh", "zh-hant"}
    if low in known:
        return "zh-Hant" if low == "zh-hant" else ("es-ES" if low == "es-es" else low)
    return None


# ----------------------------------------------------------------- SSE 解析
def parse_sse_events(raw_text):
    """标准 SSE：按空行分帧，多行 data 用 \\n 拼接，`:` 开头是注释。"""
    events, event_name, data_lines = [], None, []
    lines = raw_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        i += 1
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
    if data_lines:  # 流末尾没有空行收尾的兜底
        events.append((event_name or "message", "\n".join(data_lines)))
    return events


def read_doubao_stream(raw_bytes, content_type):
    """返回 {index: 译文}。JSON 错误体与 event:err 都抛 DoubaoError。"""
    head = raw_bytes.lstrip()[:1]
    if "application/json" in (content_type or "").lower() or head == b"{":
        try:
            frame = json.loads(raw_bytes.decode("utf-8", "replace"))
            code = frame.get("code")
            if isinstance(code, int) and code != 0:
                raise DoubaoError(code, frame.get("msg") or frame.get("message") or "未知错误")
            raise DoubaoError(-1, "返回了非流式 JSON：" + raw_bytes[:200].decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise DoubaoError(-1, "响应既不是 SSE 也不是 JSON：" + raw_bytes[:120].decode("utf-8", "replace"))

    items, saw_done = {}, False
    for name, data in parse_sse_events(raw_bytes.decode("utf-8", "replace")):
        if name == "done":
            saw_done = True
            continue
        if name == "err":
            try:
                f = json.loads(data)
                raise DoubaoError(f.get("code", 710020702), f.get("msg") or "流式错误帧")
            except json.JSONDecodeError:
                raise DoubaoError(710020702, data[:120])
        if name != "json":
            log("忽略未知 SSE 事件:", name)
            continue
        try:
            frame = json.loads(data)
        except json.JSONDecodeError:
            log("忽略无法解析的 json 帧")
            continue
        code = frame.get("code")
        if isinstance(code, int) and code != 0:
            raise DoubaoError(code, frame.get("msg") or "服务端错误")
        fd = frame.get("data")
        if not isinstance(fd, dict):
            continue
        for it in fd.get("items") or []:
            if not isinstance(it, dict):
                continue
            idx, res = it.get("index"), it.get("res")
            if not isinstance(idx, int) or not isinstance(res, str):
                continue
            # 同一 index 可能多次出现（增量帧），保留最长的那条最稳
            if idx not in items or len(res) > len(items[idx]):
                items[idx] = res

    if not items:
        raise DoubaoError(-1, "翻译流结束但没有返回任何译文" + ("（收到 done）" if saw_done else "（未收到 done，流可能被截断）"))
    if not saw_done:
        log("警告：未收到 event:done，结果可能不完整")
    return items


# ----------------------------------------------------------------- 翻译
def split_chunks(text):
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


def translate(text, target_lang, engine, scene, cookie):
    out = []
    for chunk in split_chunks(text):
        body = json.dumps({
            "raw_text": [chunk],
            "target_lang": target_lang,
            "translate_service": str(engine),
            "scene": int(scene),
            "frontend_source": 1,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "User-Agent": UA,
            "Referer": UPSTREAM + "/",
        }
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(
            UPSTREAM + STREAM_PATH, data=body, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=180)
            raw = resp.read()
            ctype = resp.headers.get("content-type", "")
        except urllib.error.HTTPError as e:
            raise DoubaoError(e.code, "HTTP %s %s" % (e.code, e.read()[:200].decode("utf-8", "replace")))
        except Exception as e:
            raise DoubaoError(-1, "网络错误: %s" % e)

        items = read_doubao_stream(raw, ctype)
        if 0 not in items:
            raise DoubaoError(-1, "该批没有 index=0 的译文，收到下标: %s" % sorted(items))
        out.append(items[0])
    return "\n".join(out)


# ----------------------------------------------------------------- HTTP 服务
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def _json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        cookie = load_cookie()
        names = sorted({p.split("=")[0].strip() for p in cookie.split(";") if "=" in p})
        self._json(200, {
            "service": "doubao→deeplx adapter",
            "upstream": UPSTREAM,
            "engine": ENGINE,
            "scene": SCENE,
            "cookie_loaded": bool(cookie),
            "cookie_names": names,
            "usage": "陪读蛙 DeepLX baseURL 填 http://127.0.0.1:%d/translate" % PORT,
        })

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json(400, {"code": 400, "message": "请求体不是合法 JSON"})
            return

        text = req.get("text")
        if not isinstance(text, str) or text.strip() == "":
            self._json(400, {"code": 400, "message": "缺少 text 字段"})
            return

        target = to_doubao_lang(req.get("target_lang"))
        if not target:
            self._json(400, {"code": 400,
                             "message": "不支持的目标语言: %r（豆包支持 19 种语言码）" % req.get("target_lang")})
            return

        # 引擎/场景：环境变量为默认，允许请求体里带 translate_service / scene 临时覆盖
        engine = str(req.get("translate_service", ENGINE))
        try:
            scene = int(req.get("scene", SCENE))
        except (TypeError, ValueError):
            scene = SCENE

        cookie = load_cookie()
        if not cookie:
            self._json(401, {"code": 710012001,
                             "message": "未配置豆包 Cookie：请把 Cookie 整串写入 doubao-cookie.txt 或环境变量 DOUBAO_COOKIE"})
            return

        try:
            result = translate(text, target, engine, scene, cookie)
        except DoubaoError as e:
            status = 401 if e.code == 710012001 else 502
            log("翻译失败", str(e))
            self._json(status, {"code": e.code, "message": "豆包: %s" % e})
            return
        except Exception as e:
            self._json(500, {"code": 500, "message": "适配器内部错误: %s" % e})
            return

        log("OK  %d 字符 → %s  (engine=%s scene=%s)" % (len(text), target, engine, scene))
        self._json(200, {
            "code": 200,
            "id": int(__import__("time").time() * 1000),
            "data": result,
            "method": "doubao-adapter(engine=%s)" % engine,
            "source_lang": req.get("source_lang", "auto"),
            "target_lang": req.get("target_lang", ""),
        })

    def log_message(self, fmt, *args):
        pass  # 用自定义 log()


if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("* 豆包→DeepLX 适配器: http://127.0.0.1:%d/translate" % PORT)
    print("* 上游 %s | 引擎 %s (%s) | 场景 %s" % (
        UPSTREAM, ENGINE, {"0": "火山引擎", "1": "豆包AI", "3": "微软"}.get(ENGINE, "?"), SCENE))
    ck = load_cookie()
    print("* Cookie: %s" % ("已加载(%d 字节)，来自 %s" % (len(ck), "环境变量" if os.environ.get("DOUBAO_COOKIE") else COOKIE_FILE) if ck
                           else "未配置！请把豆包 Cookie 粘进 " + COOKIE_FILE))
    print("* 陪读蛙 DeepLX baseURL 填: http://127.0.0.1:%d/translate" % PORT)
    print("* Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n* 已退出")
