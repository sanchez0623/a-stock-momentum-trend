"""行情 API(方案 §6.2): 实时行情 / K线 / 指标 / WebSocket 推送.

WebSocket 支持两类订阅:
- /ws/quote?symbols=...  行情推送(原有)
- /ws/alert                盘中预警推送(新增)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import config_manager
from app.core.datasource import data_source_manager
from app.core.indicators import compute_all

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["quote"])


class ConnectionManager:
    """WebSocket 连接管理器: 支持行情订阅 + 预警广播."""

    def __init__(self) -> None:
        # 行情订阅: {websocket: set(symbols)}
        self._quote_subs: dict[WebSocket, set[str]] = {}
        # 预警订阅: set(websocket)
        self._alert_subs: set[WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect_quote(self, websocket: WebSocket, symbols: list[str]) -> None:
        await websocket.accept()
        async with self._lock:
            self._quote_subs[websocket] = set(symbols)

    async def connect_alert(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._alert_subs.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._quote_subs.pop(websocket, None)
            self._alert_subs.discard(websocket)

    async def update_symbols(self, websocket: WebSocket, symbols: list[str]) -> None:
        async with self._lock:
            if websocket in self._quote_subs:
                self._quote_subs[websocket] = set(symbols)

    async def broadcast_quote(self, data: dict[str, Any]) -> None:
        """广播行情给订阅了相关标的的连接."""
        symbols = {item["symbol"] for item in data.get("data", [])}
        async with self._lock:
            targets = [ws for ws, subs in self._quote_subs.items() if subs & symbols]
        for ws in targets:
            try:
                await ws.send_json(data)
            except Exception:
                await self.disconnect(ws)

    async def broadcast_alert(self, alert: dict[str, Any]) -> None:
        """广播预警给所有预警订阅者."""
        payload = {"code": 0, "type": "alert", "data": alert}
        async with self._lock:
            targets = list(self._alert_subs)
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                await self.disconnect(ws)


# 全局连接管理器
ws_manager = ConnectionManager()


class QuoteBatchBody(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=200)


def _kline_to_records(df) -> list[dict]:
    """DataFrame -> [{date, open, high, low, close, volume, amount}] 供 JSON."""
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        out.append({
            "date": str(r["date"]),
            "open": round(float(r["open"]), 3),
            "high": round(float(r["high"]), 3),
            "low": round(float(r["low"]), 3),
            "close": round(float(r["close"]), 3),
            "volume": float(r["volume"]),
            "amount": float(r["amount"]),
        })
    return out


def _quote_dict(q) -> dict:
    """Quote -> 统一字典(单只/批量共用, 保持字段一致)."""
    return {
        "symbol": q.symbol, "name": q.name, "price": q.price,
        "open": q.open, "high": q.high, "low": q.low, "prev_close": q.prev_close,
        "volume": q.volume, "amount": q.amount, "change": q.change,
        "change_pct": q.change_pct, "timestamp": q.timestamp,
    }


@router.get("/quote/{symbol}", response_model=None)
async def get_quote(symbol: str) -> dict | JSONResponse:
    quotes = await data_source_manager.get_realtime_quote([symbol])
    if not quotes:
        return JSONResponse(status_code=404, content={"code": 1, "msg": "未获取到行情", "data": None})
    return {"code": 0, "msg": "ok", "data": _quote_dict(quotes[0])}


@router.post("/quote/batch")
async def batch_quote(body: QuoteBatchBody) -> dict:
    """批量实时行情(自选/持仓页用): 一次请求多只, 复用 5s 内存缓存.

    替代前端 N 只自选 N 个轮询请求; 无行情的代码不在返回中(前端按需容错).
    """
    quotes = await data_source_manager.get_realtime_quote(body.symbols)
    return {"code": 0, "msg": "ok", "data": [_quote_dict(q) for q in quotes]}


@router.get("/kline/{symbol}")
async def get_kline(
    symbol: str,
    period: str = Query("daily", pattern="^(1m|5m|15m|30m|60m|daily|weekly)$"),
    count: int = Query(120, ge=10, le=2000),
) -> dict:
    df = await data_source_manager.get_kline(symbol, period, count)
    return {"code": 0, "msg": "ok", "data": {"symbol": symbol, "period": period, "klines": _kline_to_records(df)}}


@router.get("/indicators/{symbol}", response_model=None)
async def get_indicators(
    symbol: str,
    period: str = Query("daily", pattern="^(1m|5m|15m|30m|60m|daily|weekly)$"),
    count: int = Query(200, ge=30, le=2000),
) -> dict | JSONResponse:
    """计算并返回该票全部指标(最后一个 bar 快照 + 序列)."""
    df = await data_source_manager.get_kline(symbol, period, count)
    if df is None or df.empty:
        return JSONResponse(status_code=404, content={"code": 1, "msg": "无行情数据", "data": None})
    cfg = config_manager.get()
    ind = compute_all(
        df,
        ma_short=cfg["趋势"]["ma_short"],
        ma_mid=cfg["趋势"]["ma_mid"],
        ma_long=cfg["趋势"]["ma_long"],
        macd_fast=cfg["动量"]["macd_fast"],
        macd_slow=cfg["动量"]["macd_slow"],
        macd_signal=cfg["动量"]["macd_signal"],
        rsi_period=cfg["动量"]["rsi_period"],
        roc_period=cfg["动量"]["roc_period"],
        volume_ma=cfg["量能"]["volume_ma"],
    )
    last = ind.iloc[-1].to_dict()
    # 清理 NaN
    snapshot = {k: (None if v is None else (round(float(v), 4) if isinstance(v, float) else v)) for k, v in last.items()}
    return {"code": 0, "msg": "ok", "data": {"symbol": symbol, "period": period, "snapshot": snapshot}}


@router.websocket("/ws/quote")
async def ws_quote(websocket: WebSocket, symbols: str = "300750,600519") -> None:
    """实时行情推送(5s 轮询源, 自选股页面订阅)."""
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    await ws_manager.connect_quote(websocket, sym_list)
    try:
        while True:
            quotes = await data_source_manager.get_realtime_quote(sym_list)
            await websocket.send_json({"code": 0, "data": [
                {"symbol": q.symbol, "price": q.price, "change_pct": q.change_pct,
                 "name": q.name, "high": q.high, "low": q.low, "open": q.open,
                 "prev_close": q.prev_close, "volume": q.volume} for q in quotes
            ]})
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        logger.info("WS 断开: %s", symbols)
    except Exception as exc:  # noqa: BLE001
        logger.warning("WS 异常: %s", exc)
    finally:
        await ws_manager.disconnect(websocket)


@router.websocket("/ws/alert")
async def ws_alert(websocket: WebSocket) -> None:
    """盘中预警推送(监控页面/全局订阅)."""
    await ws_manager.connect_alert(websocket)
    try:
        # 保持连接, 等待广播
        while True:
            await asyncio.sleep(30)  # 心跳
    except WebSocketDisconnect:
        logger.info("预警 WS 断开")
    except Exception as exc:  # noqa: BLE001
        logger.warning("预警 WS 异常: %s", exc)
    finally:
        await ws_manager.disconnect(websocket)
