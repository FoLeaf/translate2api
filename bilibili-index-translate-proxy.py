#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B站 Index-Translate 免费 API 本地代理：剥掉 Origin 头，规避火狐扩展直连的 412。

用法：
    python bilibili-index-translate-proxy.py [端口]     # 默认 8787，仅监听 127.0.0.1

然后 ReadFrog → 选项 → API 服务商 → OpenAI 兼容：
    Base URL : http://127.0.0.1:8787/v1
    API Key  : sk-free（随便填非空）
    模型     : Index-Translate-35B-A3B

仅依赖 Python 3 标准库。
"""
import sys
import http.server
import urllib.request
import urllib.error

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
UPSTREAM = "https://index-translate.bilibili.com"

# 逐跳头/会触发上游风控的头，一概不转发（origin 是关键）
HOP = {
    "origin", "referer", "host", "content-length", "connection",
    "keep-alive", "transfer-encoding", "te", "upgrade",
    "proxy-authorization", "proxy-connection", "accept-encoding",
}


class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None

        req = urllib.request.Request(UPSTREAM + self.path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in HOP:
                req.add_header(k, v)

        try:
            up = urllib.request.urlopen(req, timeout=300)
            status = up.status
            headers = up.headers
        except urllib.error.HTTPError as e:      # 4xx/5xx 也原样带回
            up = e
            status = e.code
            headers = e.headers
        except Exception as e:
            msg = ("proxy error: %s" % e).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        self.send_response(status)
        for k, v in headers.items():
            lk = k.lower()
            if lk in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Access-Control-Allow-Origin", "*")
        # 不回 Content-Length、直接关连接 => 天然支持 SSE 流式透传
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = up.read(256)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            pass
        self.close_connection = True

    do_GET = do_POST = do_HEAD = do_OPTIONS = _proxy

    def log_message(self, fmt, *args):
        sys.stderr.write("[proxy] %s %s\n" % (self.command, self.path))


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Proxy)
    print("* Index-Translate 本地代理已启动: http://127.0.0.1:%d  (上游 %s)" % (PORT, UPSTREAM))
    print("* ReadFrog Base URL 请填:        http://127.0.0.1:%d/v1" % PORT)
    print("* Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n* 已退出")
