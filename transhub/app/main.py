# -*- coding: utf-8 -*-
"""TransHub 入口：装配网关、后台、静态资源，完成初始化。"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import admin, config, db, gateway
from .providers import REGISTRY  # 触发服务商注册（login 流在 admin 导入链里注册）


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    pw = admin.ensure_initial_admin_password()
    if pw:
        print("=" * 60)
        print("[TransHub] 已生成随机管理员密码: %s" % pw)
        print("[TransHub] 同时写入 %s/initial_admin_password.txt" % config.DATA_DIR)
        print("[TransHub] 登录后请到「设置」页修改。")
        print("=" * 60)
    print("[TransHub] 已加载服务商: %s" % ", ".join(REGISTRY.keys()))
    yield


app = FastAPI(title=config.APP_NAME, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

app.include_router(gateway.router)
app.include_router(admin.router)

_static = Path(__file__).parent / "static"
app.mount("/admin/static", StaticFiles(directory=str(_static)), name="static")


@app.get("/")
async def index():
    return JSONResponse({
        "service": config.APP_NAME,
        "endpoints": {
            "admin": "/admin",
            "health": "/healthz",
            "providers": sorted(REGISTRY.keys()),
        },
    })
