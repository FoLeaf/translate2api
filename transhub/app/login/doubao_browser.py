# -*- coding: utf-8 -*-
"""豆包扫码登录：容器内 Playwright 真实浏览器 + 截图流。

豆包的 SSO 扫码接口（sso.douyin.com/get_qrcode）被字节风控(bdms)保护，裸调 API
拿不到 token；www.doubao.com 首页又有营销弹窗干扰。而 accounts.doubao.com 是
豆包自建的整页 SSO 登录页：默认即展示「豆包 App 扫一扫」二维码，扫码确认后
自动回跳 doubao.com 并落地登录 Cookie——最适合自动化收割。

实现：起一个真实 Chromium 打开该页，把页面以截图帧实时回传到后台；轮询浏览器
Cookie（Playwright 可读 HttpOnly），出现 sessionid/sid_tt 即收割入库。
浏览器仅在登录期间存活，平时不占内存。
"""
from __future__ import annotations

import asyncio
import time

from .. import db
from .base import LoginFlow, register_flow

LOGIN_URL = "https://accounts.doubao.com/"
TIMEOUT_S = 300
FRAME_INTERVAL = 1.2
COOKIE_MARKERS = ("sessionid", "sid_tt", "uid_tt")


@register_flow("doubao")
class DoubaoBrowserFlow(LoginFlow):
    mode = "frame"

    def __init__(self):
        self._status = "idle"
        self._detail = ""
        self._frame: bytes = b""
        self._seq = 0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- api
    async def start(self) -> dict:
        async with self._lock:
            if self._task and not self._task.done():
                return {"ok": True, "mode": self.mode, "detail": "登录会话进行中"}
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                return {"ok": False, "detail":
                        "镜像未包含 Playwright 浏览器组件，无法扫码登录（可临时用 Cookie 手动导入）"}
            self._status = "waiting"
            self._detail = "正在启动浏览器…"
            self._frame = b""
            self._task = asyncio.create_task(self._run(async_playwright))
            return {"ok": True, "mode": self.mode, "detail": "浏览器启动中"}

    async def poll(self) -> dict:
        return {"status": self._status, "detail": self._detail}

    async def frame(self):
        if not self._frame:
            return None
        return self._seq, self._frame

    async def cancel(self) -> None:
        if self._task:
            self._task.cancel()
        self._status = "idle"

    # ------------------------------------------------------------- worker
    async def _run(self, async_playwright) -> None:
        pw = None
        browser = None
        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled",
                      "--disable-gpu", "--mute-audio"],
            )
            context = await browser.new_context(
                viewport={"width": 1100, "height": 860},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/141.0.0.0 Safari/537.36"),
            )
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            self._detail = "请用豆包 App「扫一扫」扫描页面右侧二维码"
            t0 = time.time()
            while time.time() - t0 < TIMEOUT_S:
                try:
                    png = await page.screenshot(type="png")
                    self._frame = png
                    self._seq += 1
                except Exception:
                    pass

                names: set[str] = set()
                all_cookies = []
                for domain in ("https://www.doubao.com", "https://accounts.doubao.com"):
                    try:
                        cs = await context.cookies(domain)
                    except Exception:
                        cs = []
                    all_cookies.extend(cs)
                    names |= {c["name"] for c in cs}

                if any(m in names for m in COOKIE_MARKERS):
                    # 等待回跳把完整 Cookie 落稳
                    await page.wait_for_timeout(2500)
                    for domain in ("https://www.doubao.com", "https://accounts.doubao.com"):
                        try:
                            all_cookies.extend(await context.cookies(domain))
                        except Exception:
                            pass
                    seen, merged = set(), []
                    for c in all_cookies:
                        key = (c["name"], c["domain"])
                        if key in seen:
                            continue
                        seen.add(key)
                        merged.append(f"{c['name']}={c['value']}")
                    db.put_credential("doubao", {
                        "cookie": "; ".join(merged),
                        "cookie_names": sorted({c["name"] for c in all_cookies}),
                        "captured_at": int(time.time()),
                    })
                    self._status = "confirmed"
                    self._detail = "登录成功，豆包 Cookie 已自动保存"
                    return

                await page.wait_for_timeout(int(FRAME_INTERVAL * 1000))

            self._status = "expired"
            self._detail = "超时未扫码，请重新发起登录"
        except asyncio.CancelledError:
            self._status = "idle"
            self._detail = "已取消"
        except Exception as e:
            self._status = "error"
            self._detail = "登录流程异常: %r" % e
        finally:
            for closer in (browser, pw):
                try:
                    if closer:
                        await closer.close()
                except Exception:
                    pass
