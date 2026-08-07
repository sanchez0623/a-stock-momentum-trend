"""全局配置中心。

读取优先级: env > 数据库配置 > 默认值
- 默认值: DEFAULT_CONFIG(下方按功能分组)
- 数据库: config 表单行 JSON, 启动时加载, 修改经 API 写回
- env: LLM_* / ENABLE_* / EASTMONEY_* / PROXY_POOL / DATA_DIR 等覆盖

热更新: ConfigManager.update(partial) 深合并后写库, 并通知已注册的监听器刷新内存配置。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 默认配置
DEFAULT_CONFIG: dict[str, Any] = {
    "趋势": {
        "ma_short": 10,
        "ma_mid": 20,
        "ma_long": 60,
        "trend_filter": "ADX",
        "adx_threshold": 25,
    },
    "动量": {
        "roc_period": 12,
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
    },
    "量能": {
        "volume_ma": 20,
        "volume_ratio_threshold": 1.5,
    },
    "风控": {
        "daily_loss_limit_pct": 3.0,
        "consecutive_loss_limit": 3,
        "max_drawdown_pct": 10.0,
        "single_position_pct": 25.0,
        "total_position_pct": 80.0,
        "stop_loss_pct": 5.0,
        "trailing_stop_pct": 8.0,
    },
    "仓位": {
        "strategy": "pyramid",  # pyramid | kelly | fixed
        "pyramid_ratios": [0.5, 0.3, 0.2],
        # 止盈模式: atr(波动率自适应, 默认) | fixed(固定百分比)
        "take_profit_mode": "atr",
        "atr_multipliers": [1.5, 3.0, 5.0],  # 动态档 = 成本 × (1 + 倍数 × ATR%)
        "atr_period": 14,
        "min_tp_pct": 3.0,                   # 动态档下限保护(第一档至少 +3%)
        "take_profit_levels": [1.03, 1.06, 1.10],  # fixed 模式档位
        "take_profit_ratios": [0.2, 0.3, 0.5],     # 各档减仓比例(早期少减, 让利润奔跑)
        "kelly_fraction": 0.5,
    },
    "做T": {
        "enable": True,
        "min_swing_pct": 1.5,
        "support_lookback": 5,
        "t_position_ratio": 0.3,
    },
    "评分权重": {
        "timing": 0.25,
        "position": 0.20,
        "stop": 0.20,
        "profit": 0.20,
        "discipline": 0.15,
    },
    "数据源": {
        "priority": ["mootdx", "tencent", "eastmoney", "akshare"],
        "enabled": {"mootdx": True, "tencent": True, "eastmoney": True, "akshare": True},
        "eastmoney": {
            "interval_sec": 2.0,
            "max_workers": 1,
            "retry": 3,
            "enable_patch": True,
        },
        "proxy_pool": [],
    },
    "llm": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "temperature": 0.3,
        "max_tokens": 2000,
        "timeout_sec": 60,
        "enabled": False,
    },
    "手续费": {
        "commission_rate": 0.00005,      # 佣金: 万 0.5
        "commission_min": 5.0,           # 单笔最低佣金(元), 不足按 5 元收
        "stamp_tax_rate": 0.0005,        # 印花税: 万 5, 仅卖方
        "exchange_fee_rate": 0.0000341,  # 经手费: 万 0.341, 双边
        "regulatory_fee_rate": 0.00002,  # 证管费: 万 0.2, 双边
        "transfer_fee_rate": 0.00001,    # 过户费: 万 0.1, 双边
    },
}

# env -> 配置路径 的覆盖映射
_ENV_MAP: dict[str, str] = {
    "LLM_PROVIDER": "llm.provider",
    "LLM_BASE_URL": "llm.base_url",
    "LLM_API_KEY": "llm.api_key",
    "LLM_MODEL": "llm.model",
    "EASTMONEY_INTERVAL_SEC": "数据源.eastmoney.interval_sec",
    "EASTMONEY_MAX_WORKERS": "数据源.eastmoney.max_workers",
    "ENABLE_EASTMONEY_PATCH": "数据源.eastmoney.enable_patch",
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """递归合并: overlay 的键覆盖 base; 内层 dict 继续递归."""
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _apply_env_overrides(cfg: dict[str, Any]) -> None:
    """把环境变量覆盖写入配置(就地修改)."""
    for env_name, path in _ENV_MAP.items():
        val = os.getenv(env_name)
        if val is None:
            continue
        parts = path.split(".")
        node = cfg
        for p in parts[:-1]:
            node = node[p]
        raw: str | int | float | bool = val
        target = node.get(parts[-1]) if isinstance(node, dict) else None
        if isinstance(target, bool):
            raw = val.lower() in ("1", "true", "yes", "on")
        elif isinstance(target, int):
            raw = int(float(val))
        elif isinstance(target, float):
            raw = float(val)
        node[parts[-1]] = raw
    # 数据源开关
    enabled = cfg["数据源"]["enabled"]
    for name in ("mootdx", "tencent", "eastmoney", "akshare"):
        flag = os.getenv(f"ENABLE_{name.upper()}")
        if flag is not None:
            enabled[name] = flag.lower() in ("1", "true", "yes", "on")
    # 代理池
    proxy = os.getenv("PROXY_POOL")
    if proxy:
        cfg["数据源"]["proxy_pool"] = [u.strip() for u in proxy.split(",") if u.strip()]


class ConfigManager:
    """配置中心单例. 线程安全."""

    _instance: ConfigManager | None = None

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._cfg: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self._db = None  # sqlmodel Session 工厂, 由 app 启动时注入
        data_dir = data_dir or os.getenv("DATA_DIR", "data")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "trading.db"

    # ------------------------------------------------------------ 生命周期
    @classmethod
    def instance(cls) -> ConfigManager:
        if cls._instance is None:
            cls._instance = ConfigManager()
        return cls._instance

    def attach_db(self, get_session) -> None:
        """注入 db 会话工厂(避免循环依赖, app 启动时调用)."""
        self._db = get_session

    def load_from_db(self) -> None:
        """启动时: 默认值 <- DB 配置 <- env 覆盖."""
        self._cfg = copy.deepcopy(DEFAULT_CONFIG)
        if self._db is not None:
            try:
                with self._db() as s:
                    from app.models.models import ConfigRow
                    row = s.get(ConfigRow, 1)
                    if row and row.data_json:
                        db_cfg = json.loads(row.data_json)
                        self._cfg = _deep_merge(self._cfg, db_cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("加载数据库配置失败, 使用默认配置: %s", exc)
        _apply_env_overrides(self._cfg)

    def update(self, partial: dict[str, Any], persist: bool = True) -> dict[str, Any]:
        """热更新: 深合并 -> 写库 -> 通知监听器. 返回新配置."""
        with self._lock:
            self._cfg = _deep_merge(self._cfg, partial)
            if persist and self._db is not None:
                try:
                    with self._db() as s:
                        from app.models.models import ConfigRow
                        row = s.get(ConfigRow, 1)
                        if row is None:
                            row = ConfigRow(id=1, data_json="{}")
                            s.add(row)
                        row.data_json = json.dumps(self._cfg, ensure_ascii=False)
                        s.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("配置写库失败: %s", exc)
            snapshot = copy.deepcopy(self._cfg)
        for fn in list(self._listeners):
            try:
                fn(snapshot)
            except Exception:  # noqa: BLE001
                logger.exception("配置监听器执行失败")
        return snapshot

    def get(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._cfg)

    def get_path(self, dotted: str, default: Any = None) -> Any:
        """按点分路径取值, 如 get_path('风控.stop_loss_pct')."""
        with self._lock:
            node: Any = self._cfg
            for p in dotted.split("."):
                if not isinstance(node, dict) or p not in node:
                    return default
                node = node[p]
            return copy.deepcopy(node)

    def register_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """注册配置变更监听器(数据源管理器等)."""
        with self._lock:
            self._listeners.append(fn)

    def masked(self) -> dict[str, Any]:
        """返回脱敏配置(隐藏 api_key), 供前端展示."""
        cfg = self.get()
        llm = cfg.get("llm", {})
        if llm.get("api_key"):
            llm["api_key"] = "******"
        return cfg


# 便捷单例
config_manager = ConfigManager.instance()
