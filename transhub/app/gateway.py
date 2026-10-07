# -*- coding: utf-8 -*-
"""对外翻译网关：给 ReadFrog 等客户端调用的公开端点。

路由约定（一个服务商一个前缀，新增服务商自动挂新前缀不需要改这里）：
- POST /doubao/translate            DeepLX 协议（ReadFrog「纯翻译服务商 DeepLX」）
- POST /doubao/{api_key}/translate  同上，API Key 放路径段（ReadFrog {{apiKey}} 占位符替换后的形态）
- ANY  /bilibili/{path}             OpenAI 兼容透传（ReadFrog「OpenAI 兼容(自定义)」）
鉴权：一旦后台创建过 API Key，则所有网关端点都要求携带；
      DeepLX 端点两种携带方式都支持：Authorization 头（Bearer / DeepL-Auth-Key / 裸 key）
      或 /doubao/{api_key}/... 的路径段（ReadFrog 的 DeepLX 客户端不发鉴权头，只能走路径）。
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import config, db, security
from .providers import ProviderError, get as get_provider

router = APIRouter()

# 豆包入口排队信号量：上游串行消化，入口排队等待而非直接拒绝（惰性创建）
_doubao_queue: asyncio.Semaphore | None = None


def _get_doubao_queue() -> asyncio.Semaphore:
    global _doubao_queue
    if _doubao_queue is None:
        _doubao_queue = asyncio.Semaphore(config.DOUBAO_QUEUE_CAP)
    return _doubao_queue


def _cors(headers: dict | None = None) -> dict:
    h = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    }
    if headers:
        h.update(headers)
    return h


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _unauthorized() -> JSONResponse:
    return JSONResponse({"code": 401, "message": "需要 API Key：请在后台创建，并以 "
                        "Authorization: Bearer <key> 携带"}, status_code=401, headers=_cors())


def _check_key(request: Request, path_key: str | None = None) -> JSONResponse | None:
    if not db.key_enabled_exists():
        return None
    if path_key is not None:
        # ReadFrog 的 DeepLX 客户端不发鉴权头，Key 只能经 {{apiKey}} 占位符嵌进 URL 路径
        name = db.verify_api_key(path_key.strip())
    else:
        name = security.check_api_key_from_header(request.headers.get("authorization"))
    if name is None:
        return _unauthorized()
    return None


@router.get("/healthz")
async def healthz():
    return {"ok": True, "service": config.APP_NAME}


@router.options("/doubao/translate")
@router.options("/doubao")
async def doubao_preflight():
    return Response(status_code=204, headers=_cors())


async def _deeplx(request: Request, provider_id: str, path_key: str | None = None):
    deny = _check_key(request, path_key)
    if deny is not None:
        return deny
    provider = get_provider(provider_id)
    if provider is None:
        return JSONResponse({"code": 404, "message": "unknown provider"}, status_code=404)

    t0 = time.time()
    try:
        req = await request.json()
    except Exception:
        db.log_usage(provider_id, "deeplx", False, code=400, ip=_client_ip(request), msg="bad json")
        return JSONResponse({"code": 400, "message": "请求体不是合法 JSON"},
                            status_code=400, headers=_cors())

    text = req.get("text")
    if not isinstance(text, str) or text.strip() == "":
        db.log_usage(provider_id, "deeplx", False, code=400, ip=_client_ip(request), msg="no text")
        return JSONResponse({"code": 400, "message": "缺少 text 字段"},
                            status_code=400, headers=_cors())

    # 入口排队：上游串行消化能力有限，令牌桶直接拒绝会让 ReadFrog 整队列退避
    # （无 Retry-After 头时 5 秒起步翻倍），累计 8 次 429 后放弃段落；
    # 改为在网关内排队等待，超限才拒绝并带 Retry-After 让客户端精准重试。
    queue = _get_doubao_queue()
    try:
        await asyncio.wait_for(queue.acquire(), timeout=config.DOUBAO_QUEUE_WAIT)
    except asyncio.TimeoutError:
        db.log_usage(provider_id, "deeplx", False, code=429, ip=_client_ip(request), msg="queue wait timeout")
        return JSONResponse({"code": 429, "message": "排队超时，请稍后重试"},
                            status_code=429, headers=_cors({"Retry-After": "2"}))

    try:
        try:
            data = await provider.deeplx_translate(text, req.get("source_lang"), req.get("target_lang"))
        except ProviderError as e:
            db.log_usage(provider_id, "deeplx", False, code=e.code, ms=int((time.time()-t0)*1000),
                         ip=_client_ip(request), msg=e.msg)
            return JSONResponse({"code": e.code, "message": e.msg},
                                status_code=e.http_status, headers=_cors())
        except Exception as e:  # noqa: BLE001
            db.log_usage(provider_id, "deeplx", False, code=500, ms=int((time.time()-t0)*1000),
                         ip=_client_ip(request), msg=repr(e)[:200])
            return JSONResponse({"code": 500, "message": "网关内部错误"}, status_code=500, headers=_cors())

        db.log_usage(provider_id, "deeplx", True, chars=len(text),
                     ms=int((time.time()-t0)*1000), ip=_client_ip(request))
        return JSONResponse({
            "code": 200,
            "id": int(time.time() * 1000),
            "data": data,
            "method": "transhub-%s" % provider_id,
            "source_lang": req.get("source_lang", "auto"),
            "target_lang": req.get("target_lang", ""),
        }, headers=_cors())
    finally:
        queue.release()


@router.post("/doubao/translate")
async def doubao_translate(request: Request):
    return await _deeplx(request, "doubao")


@router.post("/doubao")
async def doubao_alias(request: Request):
    return await _deeplx(request, "doubao")


@router.options("/doubao/{api_key}/translate")
@router.options("/doubao/{api_key}")
async def doubao_key_preflight():
    return Response(status_code=204, headers=_cors())


@router.post("/doubao/{api_key}/translate")
async def doubao_translate_key_in_path(request: Request, api_key: str):
    return await _deeplx(request, "doubao", path_key=api_key)


@router.post("/doubao/{api_key}")
async def doubao_alias_key_in_path(request: Request, api_key: str):
    return await _deeplx(request, "doubao", path_key=api_key)


@router.options("/bilibili/{path:path}")
async def bilibili_preflight(path: str):
    return Response(status_code=204, headers=_cors())


@router.api_route("/bilibili/{path:path}", methods=["GET", "POST", "HEAD"])
async def bilibili_proxy(request: Request, path: str):
    deny = _check_key(request)
    if deny is not None:
        return deny
    provider = get_provider("bilibili")
    if provider is None:
        return JSONResponse({"code": 404, "message": "unknown provider"}, status_code=404)

    if not security.rate_allow("bilibili", config.DEFAULT_RATE.get("bilibili", (5, 10))):
        db.log_usage("bilibili", path, False, code=429, ip=_client_ip(request), msg="rate limited")
        return JSONResponse({"code": 429, "message": "请求过于频繁，已被限流"},
                            status_code=429, headers=_cors({"Retry-After": "1"}))

    t0 = time.time()
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()}
    result = await provider.openai_proxy(request.method, path,
                                         request.url.query.decode() if isinstance(request.url.query, bytes) else str(request.url.query),
                                         headers, body if body else None)

    resp_headers = {k: v for k, v in result.headers}
    resp_headers.update(_cors())

    status_ok = result.status < 400
    db.log_usage("bilibili", path, status_ok, code=result.status,
                 chars=len(body) if body else 0,
                 ms=int((time.time()-t0)*1000), ip=_client_ip(request))

    if isinstance(result.body, bytes):
        return Response(content=result.body, status_code=result.status,
                        headers=resp_headers, media_type=None)

    async def stream():
        async for chunk in result.body:
            yield chunk

    return StreamingResponse(stream(), status_code=result.status,
                             headers=resp_headers, background=None)
