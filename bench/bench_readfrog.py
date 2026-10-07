#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟 ReadFrog 客户端形态的网关基准脚本（仅标准库，可在服务器直接运行）。

模拟要点（依据 ReadFrog 源码 mengxi-ream/read-frog main 分支核实）：
- 页面翻译队列是令牌桶：默认稳态 8 请求/秒，突发容量 20，按功能全局一个队列
- DeepLX 线路：一段一个 HTTP 请求，无批处理
- OpenAI 兼容线路：自动批处理，默认每批 4 段、约 1000 字符，凑批窗口 100 毫秒
- 429 处理：有 Retry-After 头按头部等待；无头部退避 5 秒起步、连续翻倍；
  单任务累计 8 次 429 后放弃该段
- 单请求超时 20 秒 + 每字符 15 毫秒，上限 120 秒

用法示例：
  python3 bench_readfrog.py --base http://127.0.0.1:8300 --key th-xxx --label before
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

MAX_429_PER_TASK = 8     # ReadFrog：单任务累计 8 次 429 后放弃
NO_HEADER_BACKOFF = 5.0  # ReadFrog：无 Retry-After 头时退避基数 5 秒


class Pacer:
    """令牌桶配速器：burst 个立即放行，之后按 rate 稳态放行。"""

    def __init__(self, rate: float, burst: float):
        self.rate = rate
        self.cap = float(burst)
        self.tokens = float(burst)
        self.lock = threading.Lock()
        self.last = time.monotonic()

    def take(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate
            time.sleep(min(wait, 0.2))


def http_post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    """发一个请求，按 ReadFrog 的 429 重试策略处理，返回观测记录。"""
    seen_429 = 0
    backoff = NO_HEADER_BACKOFF
    t0 = time.monotonic()
    while True:
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json", **headers},
                method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
                return {"status": resp.status, "ms": (time.monotonic() - t0) * 1000,
                        "seen_429": seen_429, "ok": resp.status == 200}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read()[:160].decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 429:
                seen_429 += 1
                if seen_429 >= MAX_429_PER_TASK:
                    return {"status": 429, "ms": (time.monotonic() - t0) * 1000,
                            "seen_429": seen_429, "ok": False,
                            "dropped": True, "body": body}
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(ra)
                except (TypeError, ValueError):
                    wait = backoff
                    backoff = min(backoff * 2, 30.0)
                time.sleep(min(wait, 30.0))
                continue
            return {"status": e.code, "ms": (time.monotonic() - t0) * 1000,
                    "seen_429": seen_429, "ok": False, "body": body}
        except Exception as e:  # 网络错误/超时按失败记，不重试（保守口径）
            return {"status": -1, "ms": (time.monotonic() - t0) * 1000,
                    "seen_429": seen_429, "ok": False, "body": repr(e)[:160]}


def make_paragraphs(n: int) -> list[str]:
    out = []
    for i in range(n):
        out.append(
            "Benchmark paragraph %d: the distributed gateway forwards concurrent "
            "translation requests to upstream providers while the benchmark measures "
            "end-to-end latency, queueing delay and retry behavior." % i)
    return out


def run_page(base: str, key: str, scenario: str, paras: list[str],
             rate: float, burst: float, model: str) -> dict:
    """翻译一页：按 ReadFrog 的队列配速派发请求，返回该页观测。"""
    pacer = Pacer(rate, burst)
    records: list[dict] = []
    lock = threading.Lock()
    page_t0 = time.monotonic()

    if scenario == "deeplx":
        url = base.rstrip("/") + "/doubao/%s/translate" % key
        headers = {}
        tasks = [(p, 20.0 + 0.015 * len(p)) for p in paras]
    elif scenario == "openai":
        url = base.rstrip("/") + "/bilibili/v1/chat/completions"
        headers = {"Authorization": "Bearer " + key}
        batches = ["\n\n%%\n\n".join(paras[i:i + 4]) for i in range(0, len(paras), 4)]
        tasks = [(b, min(20.0 + 0.015 * len(b), 120.0)) for b in batches]
    else:
        raise SystemExit("unknown scenario: " + scenario)

    def worker(text: str, timeout: float) -> None:
        if scenario == "deeplx":
            payload = {"text": text, "source_lang": "EN", "target_lang": "ZH"}
        else:
            payload = {"model": model, "stream": False,
                       "messages": [{"role": "user", "content": text}]}
        pacer.take()
        rec = http_post_json(url, payload, headers, timeout)
        with lock:
            records.append(rec)

    with ThreadPoolExecutor(max_workers=min(32, max(4, len(tasks)))) as ex:
        list(ex.map(lambda t: worker(*t), tasks))

    ok_ms = sorted(r["ms"] for r in records if r["ok"])
    total_429 = sum(r["seen_429"] for r in records)
    dropped = sum(1 for r in records if r.get("dropped"))
    failed = sum(1 for r in records if not r["ok"])
    page_ms = (time.monotonic() - page_t0) * 1000

    def pct(p):
        return round(ok_ms[min(len(ok_ms) - 1, int(len(ok_ms) * p))], 0) if ok_ms else None

    return {
        "scenario": scenario,
        "requests": len(records),
        "ok": len(ok_ms),
        "failed": failed,
        "dropped_after_8x429": dropped,
        "total_429_seen": total_429,
        "page_wall_ms": round(page_ms, 0),
        "latency_ms_p50": pct(0.50),
        "latency_ms_p95": pct(0.95),
        "latency_ms_max": round(ok_ms[-1], 0) if ok_ms else None,
        "_records": records,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="网关地址，如 http://127.0.0.1:8300")
    ap.add_argument("--key", required=True, help="网关 API Key")
    ap.add_argument("--label", default="run", help="结果标签，如 before/after")
    ap.add_argument("--scenarios", default="deeplx,openai")
    ap.add_argument("--paragraphs", type=int, default=40)
    ap.add_argument("--pages", type=int, default=1, help="同时翻译的页面数（模拟多用户）")
    ap.add_argument("--rate", type=float, default=8.0, help="客户端稳态速率（ReadFrog 默认 8/秒）")
    ap.add_argument("--burst", type=float, default=20.0, help="客户端突发容量（ReadFrog 默认 20）")
    ap.add_argument("--model", default="Index-Translate-2B")
    ap.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "results"))
    args = ap.parse_args()

    paras = make_paragraphs(args.paragraphs)
    results = []
    for scenario in [s.strip() for s in args.scenarios.split(",") if s.strip()]:
        pages_t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.pages) as ex:
            futs = [ex.submit(run_page, args.base, args.key, scenario, paras,
                              args.rate, args.burst, args.model)
                    for _ in range(args.pages)]
            page_results = [f.result() for f in futs]
        wall = (time.monotonic() - pages_t0) * 1000

        agg = {
            "scenario": scenario,
            "pages": args.pages,
            "paragraphs_per_page": args.paragraphs,
            "wall_ms": round(wall, 0),
            "sum_page_wall_ms": round(sum(p["page_wall_ms"] for p in page_results), 0),
            "ok": sum(p["ok"] for p in page_results),
            "failed": sum(p["failed"] for p in page_results),
            "dropped_after_8x429": sum(p["dropped_after_8x429"] for p in page_results),
            "total_429_seen": sum(p["total_429_seen"] for p in page_results),
        }
        all_ok = sorted(r["ms"] for p in page_results for r in p["_records"] if r["ok"])
        if all_ok:
            agg["latency_ms_p50"] = round(all_ok[len(all_ok) // 2], 0)
            agg["latency_ms_p95"] = round(all_ok[min(len(all_ok) - 1, int(len(all_ok) * 0.95))], 0)
            agg["latency_ms_max"] = round(all_ok[-1], 0)
        results.append(agg)
        print(json.dumps(agg, ensure_ascii=False))

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(
        args.out_dir, time.strftime("%Y%m%d-%H%M%S") + "-" + args.label + ".json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"label": args.label, "base": args.base,
                   "config": {"paragraphs": args.paragraphs, "pages": args.pages,
                              "rate": args.rate, "burst": args.burst,
                              "model": args.model},
                   "summary": results}, f, ensure_ascii=False, indent=2)
    print("saved:", out_path)


if __name__ == "__main__":
    main()
