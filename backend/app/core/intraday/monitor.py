"""盘中实时监控模块(方案 A: 快照轮询 + 基础预警).

核心设计:
- 每 30 秒轮询一次持仓/自选股实时快照
- 基于持仓止损/止盈/关键位 + 自选股涨跌幅/量能异动判断预警
- 预警去重: 同标的同类型同一交易日仅触发一次(指纹去重)
- 双通道推送: 写 Notification 表(站内) + WebSocket 广播(实时)
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from app import db
from app.core.config import config_manager
from app.core.datasource import data_source_manager
from app.core.position import position_manager
from app.core.volatility import dynamic_threshold
from app.models.models import Notification, Position, Watchlist, _now

logger = logging.getLogger(__name__)

TZ = dt.timezone(dt.timedelta(hours=8))


@dataclass
class AlertRule:
    """预警规则配置项."""

    key: str
    label: str
    enabled: bool = True
    threshold: float | None = None
    cooldown_sec: int = 300  # 同标的同类型冷却秒数


DEFAULT_ALERT_RULES: dict[str, AlertRule] = {
    # 持仓预警
    "stop_loss_approach": AlertRule("stop_loss_approach", "止损线逼近", True, 0.02, 300),      # 距止损线 < 2%
    "stop_loss_hit": AlertRule("stop_loss_hit", "触发止损", True, None, 300),            # 价格 <= 止损线
    "take_profit_hit": AlertRule("take_profit_hit", "触及止盈档", True, None, 300),        # 价格 >= 止盈档
    "trailing_stop_approach": AlertRule("trailing_stop_approach", "移动止损逼近", True, 0.02, 300),# 距移动止损 < 2%
    "key_support_break": AlertRule("key_support_break", "关键支撑跌破", True, None, 300),    # 跌破 MA20/MA60
    "key_resistance_break": AlertRule("key_resistance_break", "关键压力突破", True, None, 300),  # 突破前高/压力位

    # 自选股/通用预警
    "price_up_pct": AlertRule("price_up_pct", "异动上涨", True, 5.0, 300),              # 涨幅 >= 5%
    "price_down_pct": AlertRule("price_down_pct", "异动下跌", True, -5.0, 300),           # 跌幅 <= -5%
    "volume_surge": AlertRule("volume_surge", "放量异动", False, 3.0, 300),              # 量比 >= 3.0
    "volume_shrink": AlertRule("volume_shrink", "缩量异动", False, 0.3, 300),             # 量比 <= 0.3
    "new_high": AlertRule("new_high", "创新高", False, None, 300),                   # 突破近期高点
    "new_low": AlertRule("new_low", "创新低", False, None, 300),                    # 跌破近期低点
}


class IntradayMonitor:
    """盘中监控主类."""

    def __init__(self) -> None:
        self._rules: dict[str, AlertRule] = dict(DEFAULT_ALERT_RULES)
        self._last_alert: dict[str, float] = {}  # fingerprint -> timestamp
        self._refresh_rules_from_config()

    def _load_config(self) -> dict[str, Any]:
        cfg = config_manager.get()
        intraday = cfg.get("盘中监控", {}) or {}
        return intraday

    def _refresh_rules_from_config(self) -> None:
        """从配置热更新预警规则."""
        cfg = self._load_config()
        alert_rules_cfg = cfg.get("alert_rules", {}) or {}
        cooldown_sec = cfg.get("cooldown_sec", 300)

        for key, rule in self._rules.items():
            rcfg = alert_rules_cfg.get(key, {})
            if rcfg:
                rule.enabled = rcfg.get("enabled", rule.enabled)
                if "threshold" in rcfg:
                    rule.threshold = rcfg["threshold"]
                elif "threshold_pct" in rcfg:
                    # 百分比转小数
                    rule.threshold = rcfg["threshold_pct"] / 100.0
                rule.cooldown_sec = rcfg.get("cooldown_sec", cooldown_sec)

    def _is_trading_time(self, now: dt.datetime | None = None) -> bool:
        """判断是否在交易时间内(周一至五 9:30-15:00)."""
        now = now or dt.datetime.now(TZ)
        if now.weekday() >= 5:
            return False
        hm = (now.hour, now.minute)
        return (9, 30) <= hm < (15, 0)

    def _get_monitor_symbols(self, session: Session) -> list[str]:
        """获取监控标的池: 持仓 + 自选股(去重)."""
        cfg = self._load_config()
        scope = cfg.get("scope", "positions_watchlist")
        symbols: set[str] = set()

        if scope in ("positions", "positions_watchlist"):
            positions = session.exec(select(Position).where(Position.status == "holding")).all()
            symbols.update(p.symbol for p in positions)

        if scope in ("watchlist", "positions_watchlist"):
            watchlist = session.exec(select(Watchlist)).all()
            symbols.update(w.symbol for w in watchlist)

        return sorted(symbols)

    def _get_position_info(self, session: Session, symbol: str) -> tuple[float, float, float, float] | None:
        """获取持仓关键信息: 成本、固定止损价、移动止损价、止盈档位.

        Returns:
            (cost, fixed_stop_price, trailing_stop_price, take_profit_price) 或 None
        """
        pos = position_manager.get_position(symbol, session)
        if pos is None:
            return None
        cfg = config_manager.get()
        risk_cfg = cfg.get("风控", {})
        stop_loss_pct = risk_cfg.get("stop_loss_pct", 5.0) / 100.0
        trailing_stop_pct = risk_cfg.get("trailing_stop_pct", 8.0) / 100.0
        # 固定止损: 相对成本价
        fixed_stop_price = pos.cost * (1 - stop_loss_pct)
        # 移动止损: 相对持仓期间最高价(无峰值记录时以成本价为基准)
        peak_price = pos.peak_price if pos.peak_price > 0 else pos.cost
        trailing_stop_price = peak_price * (1 - trailing_stop_pct)
        # 取第一档止盈
        tp_levels = risk_cfg.get("take_profit_levels", [1.03, 1.06, 1.10])
        tp1 = pos.cost * tp_levels[0] if tp_levels else pos.cost * 1.03
        return (pos.cost, fixed_stop_price, trailing_stop_price, tp1)

    def _get_ma_levels(self, symbol: str) -> dict[str, float] | None:
        """获取关键均线位(同步调用, 内部用 to_thread). 返回 {ma20, ma60}."""
        # 这里需要异步获取, 暂时返回 None, 在 run 中异步处理
        return None

    async def _fetch_key_levels(self, symbol: str) -> dict[str, float] | None:
        """异步获取关键位: MA20/MA60 + ATR%(动态阈值用).

        Returns:
            {ma20, ma60, atr_pct} 或 None. atr_pct 为小数(0.025 = 2.5%).
        """
        try:
            df = await data_source_manager.get_kline(symbol, "daily", 60)
            if df is None or df.empty:
                return None
            ma20 = float(df["close"].rolling(20).mean().iloc[-1])
            ma60 = float(df["close"].rolling(60).mean().iloc[-1])
            # ATR14: 简易计算(真波幅均值), 与 indicators.py 口径一致
            atr = self._atr14(df)
            close = float(df["close"].iloc[-1])
            atr_pct = atr / close if close > 0 and atr > 0 else 0.0
            return {"ma20": ma20, "ma60": ma60, "atr_pct": round(atr_pct, 4)}
        except Exception:
            return None

    @staticmethod
    def _atr14(df) -> float:
        """简易 ATR14(真波幅均值), 失败返回 0(调用方走固定阈值兜底)."""
        try:
            high, low, close = df["high"], df["low"], df["close"]
            prev_close = close.shift(1)
            tr = (high - low).combine((high - prev_close).abs(), max).combine(
                (low - prev_close).abs(), max)
            atr = float(tr.tail(14).mean())
            return atr if atr > 0 else 0.0
        except Exception:
            return 0.0

    async def _fetch_ma_levels(self, symbol: str) -> dict[str, float] | None:
        """异步获取 MA20/MA60(兼容保留, 内部走 _fetch_key_levels)."""
        r = await self._fetch_key_levels(symbol)
        return {k: v for k, v in (r or {}).items() if k in ("ma20", "ma60")} or None

    async def _calc_volume_ratio(self, symbol: str, current_volume: float, prev_close: float) -> float:
        """计算量比: 当前累计成交量 / (前一日成交量/240 * 已过交易分钟数).

        使用 5 分钟线数据获取前一日成交量, 按已过交易分钟数估算当前时刻应有量.
        """
        if current_volume <= 0 or prev_close <= 0:
            return 0.0
        try:
            # 获取最近 2 日的 5 分钟线, 用于计算前一日总成交量
            df = await data_source_manager.get_kline(symbol, "5m", 100)
            if df is None or df.empty:
                return 0.0

            # 找到前一交易日的最后一根 5m K 线, 累计其成交量
            # 简化: 取倒数第 48-96 根(假设每日 48 根 5m 线)的 volume 求和
            # 更稳健: 按 date 分组求前一日的总 volume
            df["date"] = df["date"].astype(str)
            dates = df["date"].unique()
            if len(dates) < 2:
                return 0.0
            prev_date = dates[-2]  # 前一交易日
            prev_day_volume = df[df["date"] == prev_date]["volume"].sum()
            if prev_day_volume <= 0:
                return 0.0

            # 计算今日已过交易分钟数 (9:30 到现在)
            now = dt.datetime.now(TZ)
            if now.hour < 9 or (now.hour == 9 and now.minute < 30):
                return 0.0
            if now.hour >= 15:
                elapsed_min = 240  # 全天
            elif now.hour >= 13:
                elapsed_min = (now.hour - 13) * 60 + now.minute + 120  # 下午
            else:
                elapsed_min = (now.hour - 9) * 60 + now.minute - 30  # 上午

            expected_volume = prev_day_volume / 240 * elapsed_min
            if expected_volume <= 0:
                return 0.0

            return current_volume / expected_volume
        except Exception:
            return 0.0

    def _check_alert_cooldown(self, fingerprint: str, cooldown_sec: int) -> bool:
        """检查预警冷却. True=可触发, False=冷却中."""
        now = dt.datetime.now(TZ).timestamp()
        last = self._last_alert.get(fingerprint, 0)
        if now - last < cooldown_sec:
            return False
        self._last_alert[fingerprint] = now
        return True

    async def _create_notification(
        self,
        category: str,
        title: str,
        content: str,
        fingerprint: str,
        session: Session,
    ) -> Notification:
        """创建站内通知(去重: 同指纹当日仅保留最新)."""
        today = _now()[:10]
        fp_day = f"{today}:{fingerprint}"

        # 查找今日同指纹通知
        existing = session.exec(
            select(Notification).where(Notification.fingerprint == fp_day)
        ).first()

        if existing:
            # 更新内容与时间
            existing.time = _now()
            existing.content = content
            existing.title = title
            existing.read = False
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing

        notif = Notification(
            time=_now(),
            category=category,
            title=title,
            content=content,
            fingerprint=fp_day,
            read=False,
        )
        session.add(notif)
        session.commit()
        session.refresh(notif)
        return notif

    async def _broadcast_alert(self, alert: dict[str, Any]) -> None:
        """通过 WebSocket 广播预警(广播给所有预警订阅者)."""
        from app.api.quote import ws_manager
        try:
            await ws_manager.broadcast_alert(alert)
        except Exception as exc:  # noqa: BLE001
            logger.debug("预警广播失败: %s", exc)

    def _build_alert_payload(self, rule_key: str, symbol: str, name: str, price: float,
                              detail: str, level: str = "warning") -> dict[str, Any]:
        """构建标准预警载荷."""
        rule = self._rules.get(rule_key)
        return {
            "type": "alert",
            "rule_key": rule_key,
            "rule_label": rule.label if rule else rule_key,
            "symbol": symbol,
            "name": name,
            "price": round(price, 2),
            "detail": detail,
            "level": level,  # info / warning / danger
            "timestamp": _now(),
        }

    async def run_once(self) -> dict[str, Any]:
        """执行单次盘中监控轮询.

        Returns:
            统计结果: {"checked": n, "alerts": m, "errors": k}
        """
        # 刷新规则配置(热更新)
        self._refresh_rules_from_config()

        if not self._is_trading_time():
            return {"checked": 0, "alerts": 0, "errors": 0, "skipped": "非交易时间"}

        cfg = self._load_config()
        if not cfg.get("enabled", False):
            return {"checked": 0, "alerts": 0, "errors": 0, "skipped": "监控未启用"}

        interval = cfg.get("interval_sec", 30)
        # 获取监控标的
        with db.session_scope() as session:
            symbols = self._get_monitor_symbols(session)

        if not symbols:
            return {"checked": 0, "alerts": 0, "errors": 0, "skipped": "无监控标的"}

        # 批量获取实时行情(复用 5s 缓存)
        quotes = await data_source_manager.get_realtime_quote(symbols)
        if not quotes:
            return {"checked": 0, "alerts": 0, "errors": 1, "error": "行情获取失败"}

        quote_map = {q.symbol: q for q in quotes if q.is_valid}
        checked = 0
        alerts = 0
        errors = 0

        # 预检关键位: MA20/MA60 + ATR%(动态阈值用). 持仓股必取; 自选股在
        # 动态阈值开启时也取(异动涨跌幅需要 ATR, 走日线缓存基本零成本)
        dynamic_on = bool(cfg.get("dynamic_threshold_enabled", True))
        key_cache: dict[str, dict[str, float]] = {}
        with db.session_scope() as session:
            pos_symbols = {p.symbol for p in session.exec(
                select(Position).where(Position.status == "holding", Position.symbol.in_(symbols))
            ).all()}

        need_key = set(pos_symbols) if not dynamic_on else set(symbols)
        for sym in need_key:
            key_cache[sym] = await self._fetch_key_levels(sym)
        # 兼容旧引用
        ma_cache = key_cache

        # 逐只检查预警
        for q in quotes:
            if not q.is_valid:
                errors += 1
                continue
            checked += 1
            sym = q.symbol
            name = q.name or sym
            price = q.price
            change_pct = q.change_pct or 0.0
            volume_ratio = 0.0

            # 个股 ATR%(动态阈值; 缺失时各规则回退固定阈值)
            atr_pct = float((key_cache.get(sym) or {}).get("atr_pct", 0.0) or 0.0)
            # 止损逼近阈值: 动态 = 0.5×ATR%, 固定 = 配置 threshold_pct
            if dynamic_on and atr_pct > 0:
                stop_approach_th = dynamic_threshold(
                    float(cfg.get("stop_approach_atr_mult", 0.5)), atr_pct)
                if stop_approach_th <= 0:
                    stop_approach_th = self._rules["stop_loss_approach"].threshold
            else:
                stop_approach_th = self._rules["stop_loss_approach"].threshold
            # 异动涨跌幅阈值: 动态 = max(2×ATR%, 下限), 固定 = 配置 threshold
            if dynamic_on and atr_pct > 0:
                move_up_th = max(
                    dynamic_threshold(float(cfg.get("price_move_atr_mult", 2.0)), atr_pct) * 100,
                    float(cfg.get("price_move_floor_pct", 3.0)))
                move_down_th = -move_up_th
            else:
                move_up_th = self._rules["price_up_pct"].threshold
                move_down_th = self._rules["price_down_pct"].threshold

            with db.session_scope() as session:
                # --- 持仓预警 ---
                if sym in pos_symbols:
                    pos_info = self._get_position_info(session, sym)
                    if pos_info:
                        cost, fixed_stop_price, trailing_stop_price, tp_price = pos_info
                        # 止损线逼近(固定止损)
                        if self._rules["stop_loss_approach"].enabled:
                            dist = (price - fixed_stop_price) / price if price > 0 else 1.0
                            if 0 < dist <= stop_approach_th:
                                fp = f"{sym}:stop_loss_approach"
                                if self._check_alert_cooldown(fp, self._rules["stop_loss_approach"].cooldown_sec):
                                    await self._create_notification(
                                        "intraday_alert",
                                        f"{name}({sym}) 止损线逼近",
                                        f"当前价 {price:.2f}, 固定止损 {fixed_stop_price:.2f}, 距离 {dist*100:.1f}%",
                                        fp,
                                        session,
                                    )
                                    await self._broadcast_alert(self._build_alert_payload(
                                        "stop_loss_approach", sym, name, price,
                                        f"距固定止损线 {dist*100:.1f}%", "danger"
                                    ))
                                    alerts += 1

                        # 移动止损逼近
                        if self._rules["trailing_stop_approach"].enabled:
                            dist = (price - trailing_stop_price) / price if price > 0 else 1.0
                            if 0 < dist <= stop_approach_th:
                                fp = f"{sym}:trailing_stop_approach"
                                if self._check_alert_cooldown(fp, self._rules["trailing_stop_approach"].cooldown_sec):
                                    await self._create_notification(
                                        "intraday_alert",
                                        f"{name}({sym}) 移动止损逼近",
                                        f"当前价 {price:.2f}, 移动止损 {trailing_stop_price:.2f}, 距离 {dist*100:.1f}%",
                                        fp,
                                        session,
                                    )
                                    await self._broadcast_alert(self._build_alert_payload(
                                        "trailing_stop_approach", sym, name, price,
                                        f"距移动止损线 {dist*100:.1f}%", "danger"
                                    ))
                                    alerts += 1

                        # 触发固定止损
                        if self._rules["stop_loss_hit"].enabled and price <= fixed_stop_price:
                            fp = f"{sym}:stop_loss_hit"
                            if self._check_alert_cooldown(fp, self._rules["stop_loss_hit"].cooldown_sec):
                                await self._create_notification(
                                    "intraday_alert",
                                    f"{name}({sym}) 触发固定止损",
                                    f"当前价 {price:.2f} <= 固定止损线 {fixed_stop_price:.2f}, 建议立即止损",
                                    fp,
                                    session,
                                )
                                await self._broadcast_alert(self._build_alert_payload(
                                    "stop_loss_hit", sym, name, price,
                                    f"价格跌破固定止损线 {fixed_stop_price:.2f}", "danger"
                                ))
                                alerts += 1

                        # 触发移动止损
                        if self._rules["trailing_stop_approach"].enabled and price <= trailing_stop_price:
                            fp = f"{sym}:trailing_stop_hit"
                            if self._check_alert_cooldown(fp, self._rules["trailing_stop_approach"].cooldown_sec):
                                await self._create_notification(
                                    "intraday_alert",
                                    f"{name}({sym}) 触发移动止损",
                                    f"当前价 {price:.2f} <= 移动止损线 {trailing_stop_price:.2f}, 建议立即止损",
                                    fp,
                                    session,
                                )
                                await self._broadcast_alert(self._build_alert_payload(
                                    "trailing_stop_hit", sym, name, price,
                                    f"价格跌破移动止损线 {trailing_stop_price:.2f}", "danger"
                                ))
                                alerts += 1

                        # 触及止盈
                        if self._rules["take_profit_hit"].enabled and price >= tp_price:
                            fp = f"{sym}:take_profit_hit"
                            if self._check_alert_cooldown(fp, self._rules["take_profit_hit"].cooldown_sec):
                                await self._create_notification(
                                    "intraday_alert",
                                    f"{name}({sym}) 触及止盈档",
                                    f"当前价 {price:.2f} >= 止盈价 {tp_price:.2f}, 可考虑分批止盈",
                                    fp,
                                    session,
                                )
                                await self._broadcast_alert(self._build_alert_payload(
                                    "take_profit_hit", sym, name, price,
                                    f"价格触及止盈档 {tp_price:.2f}", "warning"
                                ))
                                alerts += 1

                    # 关键均线支撑/压力
                    ma = ma_cache.get(sym)
                    if ma and self._rules["key_support_break"].enabled:
                        if price < ma.get("ma20", 0):
                            fp = f"{sym}:key_support_break_ma20"
                            if self._check_alert_cooldown(fp, self._rules["key_support_break"].cooldown_sec):
                                await self._create_notification(
                                    "intraday_alert",
                                    f"{name}({sym}) 跌破 MA20",
                                    f"当前价 {price:.2f} < MA20 {ma['ma20']:.2f}, 趋势支撑减弱",
                                    fp,
                                    session,
                                )
                                await self._broadcast_alert(self._build_alert_payload(
                                    "key_support_break", sym, name, price,
                                    f"跌破 MA20({ma['ma20']:.2f})", "warning"
                                ))
                                alerts += 1
                    if ma and self._rules["key_resistance_break"].enabled:
                        if price > ma.get("ma60", 0) and price > q.high * 0.995:  # 近高位
                            fp = f"{sym}:key_resistance_break_ma60"
                            if self._check_alert_cooldown(fp, self._rules["key_resistance_break"].cooldown_sec):
                                await self._create_notification(
                                    "intraday_alert",
                                    f"{name}({sym}) 站上 MA60",
                                    f"当前价 {price:.2f} > MA60 {ma['ma60']:.2f}, 长期趋势向好",
                                    fp,
                                    session,
                                )
                                await self._broadcast_alert(self._build_alert_payload(
                                    "key_resistance_break", sym, name, price,
                                    f"站上 MA60({ma['ma60']:.2f})", "info"
                                ))
                                alerts += 1

                # --- 自选股/通用预警 ---
                # 异动涨跌幅(动态阈值: 高波动股门槛更高, 低波动股下限保护)
                if self._rules["price_up_pct"].enabled and change_pct >= move_up_th:
                    fp = f"{sym}:price_up_pct"
                    if self._check_alert_cooldown(fp, self._rules["price_up_pct"].cooldown_sec):
                        await self._create_notification(
                            "intraday_alert",
                            f"{name}({sym}) 异动上涨 {change_pct:.2f}%",
                            f"当前价 {price:.2f}, 涨幅 {change_pct:.2f}%",
                            fp,
                            session,
                        )
                        await self._broadcast_alert(self._build_alert_payload(
                            "price_up_pct", sym, name, price,
                            f"异动上涨 {change_pct:.2f}%", "warning"
                        ))
                        alerts += 1

                if self._rules["price_down_pct"].enabled and change_pct <= move_down_th:
                    fp = f"{sym}:price_down_pct"
                    if self._check_alert_cooldown(fp, self._rules["price_down_pct"].cooldown_sec):
                        await self._create_notification(
                            "intraday_alert",
                            f"{name}({sym}) 异动下跌 {change_pct:.2f}%",
                            f"当前价 {price:.2f}, 跌幅 {abs(change_pct):.2f}%",
                            fp,
                            session,
                        )
                        await self._broadcast_alert(self._build_alert_payload(
                            "price_down_pct", sym, name, price,
                            f"异动下跌 {change_pct:.2f}%", "danger"
                        ))
                        alerts += 1

                # 量能异动(接入分钟线计算真实量比)
                volume_ratio = await self._calc_volume_ratio(sym, q.volume, q.prev_close)
                if self._rules["volume_surge"].enabled and volume_ratio >= self._rules["volume_surge"].threshold:
                    fp = f"{sym}:volume_surge"
                    if self._check_alert_cooldown(fp, self._rules["volume_surge"].cooldown_sec):
                        await self._create_notification(
                            "intraday_alert",
                            f"{name}({sym}) 放量异动",
                            f"量比 {volume_ratio:.2f}x (阈值 {self._rules['volume_surge'].threshold:.1f}x)",
                            fp,
                            session,
                        )
                        await self._broadcast_alert(self._build_alert_payload(
                            "volume_surge", sym, name, price,
                            f"放量异动 量比 {volume_ratio:.2f}x", "warning"
                        ))
                        alerts += 1

                if self._rules["volume_shrink"].enabled and volume_ratio > 0 and volume_ratio <= self._rules["volume_shrink"].threshold:
                    fp = f"{sym}:volume_shrink"
                    if self._check_alert_cooldown(fp, self._rules["volume_shrink"].cooldown_sec):
                        await self._create_notification(
                            "intraday_alert",
                            f"{name}({sym}) 缩量异动",
                            f"量比 {volume_ratio:.2f}x (阈值 {self._rules['volume_shrink'].threshold:.2f}x)",
                            fp,
                            session,
                        )
                        await self._broadcast_alert(self._build_alert_payload(
                            "volume_shrink", sym, name, price,
                            f"缩量异动 量比 {volume_ratio:.2f}x", "info"
                        ))
                        alerts += 1

        return {"checked": checked, "alerts": alerts, "errors": errors}


# 单例
_monitor_instance: IntradayMonitor | None = None


def get_monitor() -> IntradayMonitor:
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = IntradayMonitor()
    return _monitor_instance


async def run_intraday_monitor() -> dict[str, Any]:
    """入口函数, 供调度器调用."""
    monitor = get_monitor()
    return await monitor.run_once()