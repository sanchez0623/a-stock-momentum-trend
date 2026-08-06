"""FastAPI 应用入口(方案 §6, 端口 8000).

- 统一前缀 /api, 统一响应 {code, msg, data}
- lifespan: 初始化 db / 配置 / 数据源管理器 / 健康检查 / 定时任务
- 构建产物 frontend/dist 存在时由本服务托管(生产单容器)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import db
from app.api import quote as quote_api
from app.api import system as system_api
from app.core.config import config_manager
from app.core.datasource import build_sources, data_source_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("app")


def _sanitize_proxy_env() -> None:
    """清除系统代理环境变量: 数据源直连国内接口, 不应走科学上网代理.

    注意: 若用户显式在 .env 配置 PROXY_POOL(东财换出口 IP), 由东财源内部使用.
    """
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(key, None)


async def init_app() -> None:
    """业务初始化(供 uvicorn lifespan 与 CLI 复用)."""
    _sanitize_proxy_env()
    db.init_db()
    config_manager.attach_db(db.session_scope)
    config_manager.load_from_db()
    data_source_manager.setup(build_sources())
    await data_source_manager.start_health_checks()
    from app.scheduler import start_scheduler

    start_scheduler()
    logger.info("应用初始化完成")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_app()
    yield
    from app.scheduler import shutdown_scheduler

    shutdown_scheduler()
    await data_source_manager.stop()


app = FastAPI(
    title="Momentum Trader 动量趋势交易系统",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本地个人系统, 开发期放开; 上公网前收紧
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- 路由(先注册 API, 再挂静态, 保证 /api/* 优先) ----
app.include_router(system_api.router)
app.include_router(quote_api.router)

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
    logger.info("前端静态托管: %s", FRONTEND_DIST)
