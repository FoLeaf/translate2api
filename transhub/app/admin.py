# -*- coding: utf-8 -*-
"""后台管理系统：密码登录 + 服务商管理 + 扫码登录 + API Key + 用量。"""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import config, db, security
from .login import FLOWS, get_flow
from .providers import REGISTRY

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

COOKIE = "th_session"


# ---------------------------------------------------------------- 中间件
def _page(request: Request, name: str, **ctx):
    ctx.setdefault("app_title", config.APP_TITLE)
    ctx.setdefault("path", request.url.path)
    return templates.TemplateResponse(request, name, ctx)


def _is_authed(request: Request) -> bool:
    return security.check_session(request.cookies.get(COOKIE))


def _deny_api() -> JSONResponse:
    return JSONResponse({"ok": False, "detail": "未登录或会话过期"}, status_code=401)


async def _require_admin(request: Request) -> JSONResponse | None:
    """页面用重定向，API 用 401；状态变更接口额外要求 X-Requested-With。"""
    if not _is_authed(request):
        return _deny_api()
    if request.method in ("POST", "PUT", "DELETE") and \
            request.headers.get("x-requested-with") != "fetch":
        return JSONResponse({"ok": False, "detail": "缺少 CSRF 头"}, status_code=403)
    return None


async def _require_admin_page(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


# ------------------------------------------------------------------ 页面
@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if _is_authed(request):
        return RedirectResponse("/admin", status_code=303)
    return _page(request, "login.html")


@router.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    r = await _require_admin_page(request)
    if r:
        return r
    return _page(request, "dashboard.html")


@router.get("/admin/providers", response_class=HTMLResponse)
async def admin_providers_page(request: Request):
    r = await _require_admin_page(request)
    if r:
        return r
    return _page(request, "providers.html")


@router.get("/admin/keys", response_class=HTMLResponse)
async def admin_keys_page(request: Request):
    r = await _require_admin_page(request)
    if r:
        return r
    return _page(request, "keys.html")


@router.get("/admin/usage", response_class=HTMLResponse)
async def admin_usage_page(request: Request):
    r = await _require_admin_page(request)
    if r:
        return r
    return _page(request, "usage.html")


@router.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request):
    r = await _require_admin_page(request)
    if r:
        return r
    return _page(request, "settings.html")


# ------------------------------------------------------------------- API
@router.post("/admin/api/login")
async def admin_api_login(request: Request):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or \
        (request.client.host if request.client else "?")
    if security.login_attempts_exceeded(ip):
        return JSONResponse({"ok": False, "detail": "失败次数过多，请 15 分钟后再试"},
                            status_code=429)
    try:
        payload = await request.json()
        password = str(payload.get("password") or "")
    except Exception:
        password = ""
    stored = security.get_admin_password_hash()
    if not stored or not security.verify_password(password, stored):
        security.record_login_fail(ip)
        return JSONResponse({"ok": False, "detail": "密码错误"}, status_code=401)
    security.record_login_ok(ip)
    resp = JSONResponse({"ok": True})
    secure = request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(COOKIE, security.issue_session(config.SESSION_DAYS),
                    httponly=True, samesite="lax", path="/", secure=secure)
    return resp


@router.post("/admin/api/logout")
async def admin_api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/admin/api/overview")
async def admin_api_overview(request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    host = request.headers.get("host") or f"127.0.0.1:{config.PORT}"
    proto = request.headers.get("x-forwarded-proto") or "http"
    base = f"{proto}://{host}"
    providers = []
    for pid, p in REGISTRY.items():
        try:
            st = await p.status()
        except Exception as e:  # noqa: BLE001
            st = {"ok": False, "detail": repr(e)}
        providers.append({
            "id": pid, "name": p.name, "desc": p.desc,
            "protocols": p.protocols, "login_supported": p.login_supported,
            "status": st,
        })
    return {
        "ok": True,
        "providers": providers,
        "stats": db.usage_stats(86400),
        "key_required": db.key_enabled_exists(),
        "key_count": len(db.list_api_keys()),
        "base_urls": {
            # ReadFrog 的 DeepLX 客户端不发鉴权头，Key 需经 {{apiKey}} 占位符嵌进 URL 路径
            "doubao_deeplx": f"{base}/doubao/" + "{{apiKey}}" + "/translate",
            "bilibili_openai": f"{base}/bilibili/v1",
        },
    }


@router.get("/admin/api/providers/{pid}")
async def admin_api_provider(pid: str, request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    p = REGISTRY.get(pid)
    if not p:
        return JSONResponse({"ok": False, "detail": "未知服务商"}, status_code=404)
    st = await p.status()
    cfg = {}
    if pid == "doubao":
        cfg = {"engine": str(db.get_setting("doubao_engine", "1")),
               "scene": db.get_setting("doubao_scene", 1)}
    flow = FLOWS.get(pid)
    return {"ok": True, "id": pid, "name": p.name, "desc": p.desc,
            "status": st, "config": cfg,
            "login_mode": flow.mode if flow else None,
            "credentials": [{k: c[k] for k in ("name", "status", "updated_at")}
                            | {"account": c["data"].get("account", ""),
                               "uid": c["data"].get("uid", "")}
                            for c in db.list_credentials(pid)]}


@router.put("/admin/api/providers/{pid}/config")
async def admin_api_provider_config(pid: str, request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    body = await request.json()
    if pid == "doubao":
        engine = str(body.get("engine", "1"))
        if engine not in ("0", "1", "3"):
            return JSONResponse({"ok": False, "detail": "engine 只能是 0/1/3"}, status_code=400)
        try:
            scene = int(body.get("scene", 1))
            assert scene in (1, 2, 3, 6)
        except Exception:
            return JSONResponse({"ok": False, "detail": "scene 只能是 1/2/3/6"}, status_code=400)
        db.set_setting("doubao_engine", engine)
        db.set_setting("doubao_scene", scene)
    else:
        return JSONResponse({"ok": False, "detail": "该服务商无可配置项"}, status_code=400)
    return {"ok": True}


@router.post("/admin/api/providers/{pid}/test")
async def admin_api_provider_test(pid: str, request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    p = REGISTRY.get(pid)
    if not p:
        return JSONResponse({"ok": False, "detail": "未知服务商"}, status_code=404)
    res = await p.test()
    return res


@router.post("/admin/api/providers/{pid}/login/start")
async def admin_api_login_start(pid: str, request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    flow = get_flow(pid)
    if not flow:
        return JSONResponse({"ok": False, "detail": "该服务商不支持登录"}, status_code=404)
    return await flow.start()


@router.get("/admin/api/providers/{pid}/login/poll")
async def admin_api_login_poll(pid: str, request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    flow = get_flow(pid)
    if not flow:
        return JSONResponse({"ok": False, "detail": "该服务商不支持登录"}, status_code=404)
    r = await flow.poll()
    r.setdefault("ok", True)
    return r


@router.get("/admin/api/providers/{pid}/login/frame")
async def admin_api_login_frame(pid: str, request: Request):
    deny = await _require_admin(request)
    if deny:
        return _deny_api()
    flow = get_flow(pid)
    if not flow:
        return JSONResponse({"ok": False}, status_code=404)
    fr = await flow.frame()
    if not fr:
        return Response(b"", status_code=503, media_type="image/png")
    _seq, png = fr
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/admin/api/providers/{pid}/login/cancel")
async def admin_api_login_cancel(pid: str, request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    flow = get_flow(pid)
    if flow:
        await flow.cancel()
    return {"ok": True}


@router.post("/admin/api/providers/{pid}/credentials/manual")
async def admin_api_credentials_manual(pid: str, request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    body = await request.json()
    cookie = str(body.get("cookie") or "").strip()
    if len(cookie) < 20:
        return JSONResponse({"ok": False, "detail": "Cookie 太短"}, status_code=400)
    import time as _t
    db.put_credential(pid, {"cookie": cookie, "manual": True, "captured_at": int(_t.time())})
    return {"ok": True}


@router.delete("/admin/api/providers/{pid}/credentials/{name}")
async def admin_api_credential_delete(pid: str, name: str, request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    ok = db.delete_credential(pid, name)
    return {"ok": ok}


# ---------------------------------------------------------------- API Key
@router.get("/admin/api/keys")
async def admin_api_keys(request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    return {"ok": True, "keys": db.list_api_keys(), "key_required": db.key_enabled_exists()}


@router.post("/admin/api/keys")
async def admin_api_keys_create(request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    body = await request.json()
    name = str(body.get("name") or "默认").strip()[:30] or "默认"
    kid, full = db.create_api_key(name)
    return {"ok": True, "id": kid, "key": full,
            "detail": "请立即复制保存，明文不再二次展示"}


@router.post("/admin/api/keys/{key_id}/toggle")
async def admin_api_keys_toggle(key_id: int, request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    keys = {k["id"]: k for k in db.list_api_keys()}
    if key_id not in keys:
        return JSONResponse({"ok": False, "detail": "不存在"}, status_code=404)
    db.set_api_key_enabled(key_id, not keys[key_id]["enabled"])
    return {"ok": True}


@router.delete("/admin/api/keys/{key_id}")
async def admin_api_keys_delete(key_id: int, request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    db.delete_api_key(key_id)
    return {"ok": True}


# ------------------------------------------------------------------ 用量
@router.get("/admin/api/usage")
async def admin_api_usage(request: Request, limit: int = 100):
    deny = await _require_admin(request)
    if deny:
        return deny
    limit = max(1, min(limit, 500))
    return {"ok": True, "rows": db.recent_usage(limit)}


# ------------------------------------------------------------------ 设置
@router.post("/admin/api/password")
async def admin_api_password(request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    body = await request.json()
    old, new = str(body.get("old") or ""), str(body.get("new") or "")
    stored = security.get_admin_password_hash()
    if not stored or not security.verify_password(old, stored):
        return JSONResponse({"ok": False, "detail": "旧密码错误"}, status_code=400)
    if len(new) < 8:
        return JSONResponse({"ok": False, "detail": "新密码至少 8 位"}, status_code=400)
    security.set_admin_password(new)
    return {"ok": True}


@router.get("/admin/api/rate")
async def admin_api_rate_get(request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    from . import config as _cfg
    out = []
    for pid in REGISTRY:
        val = db.get_setting(f"rate_{pid}")
        if isinstance(val, (list, tuple)) and len(val) == 2:
            rate, burst = float(val[0]), float(val[1])
        else:
            rate, burst = _cfg.DEFAULT_RATE.get(pid, (5, 10))
        out.append({"provider": pid, "rate": rate, "burst": burst})
    return {"ok": True, "rates": out}


@router.put("/admin/api/rate")
async def admin_api_rate(request: Request):
    deny = await _require_admin(request)
    if deny:
        return deny
    body = await request.json()
    provider = str(body.get("provider") or "")
    if provider not in REGISTRY:
        return JSONResponse({"ok": False, "detail": "未知服务商"}, status_code=404)
    try:
        rate = float(body["rate"]); burst = float(body["burst"])
        assert 0.1 <= rate <= 50 and 1 <= burst <= 100
    except Exception:
        return JSONResponse({"ok": False, "detail": "参数非法（rate 0.1-50，burst 1-100）"},
                            status_code=400)
    db.set_setting(f"rate_{provider}", [rate, burst])
    return {"ok": True}


def ensure_initial_admin_password() -> str | None:
    """首次启动设置管理员密码；返回明文（仅随机生成时）。"""
    if security.get_admin_password_hash():
        return None
    if config.ADMIN_PASSWORD:
        security.set_admin_password(config.ADMIN_PASSWORD)
        return None
    pw = "th-" + secrets.token_urlsafe(9)
    security.set_admin_password(pw)
    try:
        import os
        path = os.path.join(config.DATA_DIR, "initial_admin_password.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(pw + "\n")
        os.chmod(path, 0o600)
    except OSError:
        pass
    return pw
