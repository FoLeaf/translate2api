#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B站线路网关压力测试：模拟多个 ReadFrog 用户同时整页翻译。

每个虚拟用户独立模拟 ReadFrog 行为：令牌桶配速（默认 8/秒、突发 20）、
每批 4 段、429 按 Retry-After 头等待（无头部按 5 秒指数退避）、单任务
8 次 429 放弃。统计每用户页面墙钟、全局延迟分位、429/丢段、吞吐。

用法：
  python3 stress_bilibili.py --base http://127.0.0.1:8301 --key th-xxx \
      --users 5 --paragraphs 160 --pages 2
  python3 stress_bilibili.py ... --duration 120   # 持续模式：每用户循环翻页
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

MAX_429 = 8


class Pacer:
    def __init__(self, rate, burst):
        self.rate, self.cap, self.tokens = rate, float(burst), float(burst)
        self.lock = threading.Lock()
        self.last = time.monotonic()

    def take(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(min(wait, 0.2))


def http_post(url, payload, headers, timeout):
    seen429, backoff = 0, 5.0
    t0 = time.monotonic()
    while True:
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json", **headers},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read()
                return {"ok": r.status == 200, "status": r.status,
                        "ms": (time.monotonic() - t0) * 1000, "429": seen429}
        except urllib.error.HTTPError as e:
            try:
                e.read()
            except Exception:
                pass
            if e.code == 429:
                seen429 += 1
                if seen429 >= MAX_429:
                    return {"ok": False, "status": 429, "ms": (time.monotonic() - t0) * 1000,
                            "429": seen429, "dropped": True}
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(ra)
                except (TypeError, ValueError):
                    wait = backoff
                    backoff = min(backoff * 2, 30.0)
                time.sleep(min(wait, 30.0))
                continue
            return {"ok": False, "status": e.code, "ms": (time.monotonic() - t0) * 1000, "429": seen429}
        except Exception as e:
            return {"ok": False, "status": -1, "ms": (time.monotonic() - t0) * 1000,
                    "429": seen429, "err": repr(e)[:80]}


def paragraphs(n):
    return ["Stress paragraph %d: the gateway forwards concurrent batch translations "
            "while we measure queueing, throttling and recovery behavior." % i for i in range(n)]


class User:
    def __init__(self, args, uid):
        self.uid = uid
        self.base = args.base.rstrip("/")
        self.key = args.key
        self.model = args.model
        self.batches = ["\n\n%%\n\n".join(paragraphs(args.paragraphs)[i:i + 4])
                        for i in range(0, args.paragraphs, 4)]
        self.rate, self.burst = args.rate, args.burst
        self.stop_at = None
        self.page_walls = []
        self.reqs = []
        self.lock = threading.Lock()

    def translate_page(self):
        pacer = Pacer(self.rate, self.burst)
        recs = []
        t0 = time.monotonic()

        def work(text):
            pacer.take()
            r = http_post(self.base + "/bilibili/v1/chat/completions",
                          {"model": self.model, "stream": False,
                           "messages": [{"role": "user", "content": text}]},
                          {"Authorization": "Bearer " + self.key},
                          min(20 + 0.015 * len(text), 120))
            with self.lock:
                self.reqs.append(r)
            recs.append(r)

        with ThreadPoolExecutor(max_workers=min(32, len(self.batches))) as ex:
            list(ex.map(work, self.batches))
        with self.lock:
            self.page_walls.append(round((time.monotonic() - t0) * 1000))

    def run(self, pages, deadline):
        n = 0
        while n < pages and (deadline is None or time.monotonic() < deadline):
            self.translate_page()
            n += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--users", type=int, default=5)
    ap.add_argument("--paragraphs", type=int, default=160)
    ap.add_argument("--pages", type=int, default=1, help="每用户页面数（持续模式忽略）")
    ap.add_argument("--duration", type=int, default=0, help=">0 时为持续模式（秒）")
    ap.add_argument("--rate", type=float, default=8.0)
    ap.add_argument("--burst", type=float, default=20.0)
    ap.add_argument("--model", default="Index-Translate-2B")
    ap.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "results"))
    args = ap.parse_args()

    users = [User(args, i) for i in range(args.users)]
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    pages = 10**9 if deadline else args.pages

    t0 = time.monotonic()
    with ThreadPoolExecutor(len(users)) as ex:
        futs = [ex.submit(u.run, pages, deadline) for u in users]
        for f in futs:
            f.result()
    wall = time.monotonic() - t0

    all_reqs = [r for u in users for r in u.reqs]
    oks = sorted(r["ms"] for r in all_reqs if r["ok"])

    def pct(p):
        return round(oks[min(len(oks) - 1, int(len(oks) * p))]) if oks else None

    summary = {
        "users": args.users, "paragraphs_per_page": args.paragraphs,
        "pages_done": sum(len(u.page_walls) for u in users),
        "wall_s": round(wall, 1),
        "requests": len(all_reqs),
        "ok": len(oks),
        "failed": len(all_reqs) - len(oks),
        "dropped": sum(1 for r in all_reqs if r.get("dropped")),
        "seen_429": sum(r["429"] for r in all_reqs),
        "status_other": sorted({r["status"] for r in all_reqs if not r["ok"]}),
        "achieved_rps": round(len(all_reqs) / wall, 1) if wall else None,
        "latency_ms_p50": pct(0.50), "latency_ms_p95": pct(0.95),
        "latency_ms_p99": pct(0.99), "latency_ms_max": round(oks[-1]) if oks else None,
        "page_wall_ms_per_user": {str(u.uid): u.page_walls for u in users},
    }
    print(json.dumps(summary, ensure_ascii=False))
    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, time.strftime("%Y%m%d-%H%M%S") + "-stress.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("saved:", path)


if __name__ == "__main__":
    main()
