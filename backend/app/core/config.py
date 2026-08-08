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
    # ---------------------------------------------------------------- 交易模式(Q2: 多模式 + 规则化市况分类器)
    # 选型由「市况分类器」(modes.classify) 按 ADX/ATR/突破回踩/量能 确定性选出, 不靠 LLM 决策。
    # 每种模式是配置对象: 各自的比率/止损/加仓规则。该分组与「仓位」同级, 永久禁止 AI 调参。
    "交易模式": {
        "enabled": True,
        "default_mode": "trend_pullback",
        # 分类器阈值(市况判定)——纯数据驱动, 可回测
        "classifier": {
            "adx_strong": 30,       # ADX >= 此值视为趋势强
            "adx_weak": 18,         # ADX < 此值视为震荡/无趋势
            "breakout_dist_pct": 3.0,   # 现价距 N日高 在此范围内视为突破区
            "pullback_dist_pct": 8.0,   # 距高超过此值但在多头排列下仍判回踩
            "volume_ratio_active": 1.3, # 量比 >= 此值视为放量
            "donchian_period": 20,
        },
        # 各模式(配置对象): 各自比率/止损/加仓规则
        "modes": {
            "trend_strong": {
                "label": "趋势强攻",
                "pyramid_ratios": [0.5, 0.3, 0.2],
                "stop_loss_pct": 6.0,
                "trailing_stop_pct": 10.0,
                "min_add_profit_pct": 2.0,   # 加仓最低浮盈门槛
                "allow_add": True,
                "max_stages": 3,
                "take_profit_mode": "atr",
                "atr_multipliers": [1.5, 3.0, 5.0],
                "take_profit_ratios": [0.2, 0.3, 0.5],
                "take_profit_levels": [1.03, 1.06, 1.10],
            },
            "trend_pullback": {
                "label": "趋势回踩",
                "pyramid_ratios": [0.5, 0.3, 0.2],
                "stop_loss_pct": 5.0,
                "trailing_stop_pct": 8.0,
                "min_add_profit_pct": 3.0,
                "allow_add": True,
                "max_stages": 3,
                "take_profit_mode": "atr",
                "atr_multipliers": [1.5, 3.0, 5.0],
                "take_profit_ratios": [0.2, 0.3, 0.5],
                "take_profit_levels": [1.03, 1.06, 1.10],
            },
            "range": {
                "label": "震荡",
                "pyramid_ratios": [0.7, 0.3],
                "stop_loss_pct": 4.0,
                "trailing_stop_pct": 6.0,
                "min_add_profit_pct": 4.0,
                "allow_add": True,
                "max_stages": 2,
                "take_profit_mode": "fixed",
                "atr_multipliers": [1.5, 3.0, 5.0],
                "take_profit_ratios": [0.5, 0.5],
                "take_profit_levels": [1.03, 1.06],
            },
            "defense": {
                "label": "防守",
                "pyramid_ratios": [1.0],
                "stop_loss_pct": 3.0,
                "trailing_stop_pct": 5.0,
                "min_add_profit_pct": 999.0,  # 实际不可达 -> 不加仓
                "allow_add": False,
                "max_stages": 1,
                "take_profit_mode": "fixed",
                "atr_multipliers": [1.5, 3.0, 5.0],
                "take_profit_ratios": [1.0],
                "take_profit_levels": [1.03],
            },
        },
    },
    "评分权重": {
        "timing": 0.25,
        "position": 0.20,
        "stop": 0.20,
        "profit": 0.20,
        "discipline": 0.15,
    },
    "择时闸门": {
        # 大盘环境差时缩减/停止出票(④ 最小版). 参考指数用东财 secid 格式
        "enabled": False,
        "reference_indices": [
            {"name": "沪深300", "secid": "0.000300"},
            {"name": "创业板指", "secid": "0.399006"},
        ],
        "ma_long": 200,      # 长期均线(趋势方向)
        "ma_mid": 60,        # 中期均线(多空排列)
        "lookback": 20,      # 近 N 日收益(动量)
        "require_all_above_ma": False,  # True=全部站上MA才看多; False=多数即可
        "bull_top_n_ratio": 1.0,   # 看多环境: TopN 不缩减
        "bear_top_n_ratio": 0.3,   # 看空环境: TopN 缩到 30%
        "min_index_bars": 220,     # 指数最少 K 线数(不足则跳过该指数)
    },
    "行业限配": {
        # 每行业最多 N 只, 避免 TopN 行业抱团(⑤). level 决定用申万几级分组
        "enabled": False,
        "per_industry": 3,
        "level": "sw_l1",    # sw_l1 / sw_l2 / sw_l3(需分类映射表已填充)
        # 申万分类缺失时, 用 Baostock 证监会行业兜底分组(避免限配形同虚设)
        "fallback_csrc": True,
    },
    "选股池": {
        # 用指数成分股把 5000+ 全A 缩到 300~800 只质量池(数据源: baostock)
        # all / hs300 / zz500 / hs300+zz500(≈中证800) / sz50
        "universe": "all",
        "max_age_days": 7,       # 成分股缓存超过 N 天自动刷新
        "fallback_on_empty": True,  # 成分股取不到时是否放行全池(False=直接空结果)
    },
    "基本面因子": {
        # 把纯动量升级为"动量 + 质量"(数据源: baostock 季度财报)
        "enabled": False,
        "mode": "both",              # filter 只过滤 / score 只加分 / both
        "min_roe": 5.0,              # ROE(%) 下限
        "max_liability_to_asset": 70.0,  # 资产负债率(%) 上限
        "min_yoy_ni": -20.0,         # 归母净利润同比(%) 下限
        "max_pe_ttm": 100.0,         # PE(TTM) 上限, <=0 表示不限
        "exclude_negative_pe": True,  # 剔除亏损股(PE<=0)
        "exclude_st": True,          # 剔除 baostock 标记的 ST
        "bonus_max": 10.0,           # 质量分最高加成(叠加到总分)
        "require_data": False,       # True=无基本面数据直接剔除; False=放行(缺数据不惩罚)
    },
    "业绩事件": {
        # 业绩预告/快报催化(数据源: baostock)
        "enabled": False,
        "lookback_days": 90,
        "min_chg_pct": 30.0,   # 预增幅度达到该值才算"超预期"
        "bonus": 5.0,          # 命中利好加分
        "penalty": -5.0,       # 命中利空减分(预减/预亏/首亏)
    },
    "数据源": {
        "priority": ["mootdx", "tencent", "baostock", "eastmoney", "akshare"],
        "enabled": {"mootdx": True, "tencent": True, "baostock": True,
                    "eastmoney": True, "akshare": True},
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


def _migrate_config(cfg: dict[str, Any]) -> None:
    """旧库配置自愈(就地修改).

    痛点: 列表类配置(数据源.priority)是"整体覆盖"语义, 旧库里存的老列表会把
    新增数据源顶掉, 导致源注册了却永远轮不到. 这里在加载后补齐新增源.
    """
    ds = cfg.setdefault("数据源", {})
    enabled = ds.setdefault("enabled", {})
    priority = ds.setdefault("priority", [])
    enabled.setdefault("baostock", True)
    if "baostock" not in priority:
        # 放在东财之前: 免费无风控, 日线更稳
        if "eastmoney" in priority:
            priority.insert(priority.index("eastmoney"), "baostock")
        else:
            priority.append("baostock")
        logger.info("配置迁移: 数据源优先级补入 baostock -> %s", priority)


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
    for name in ("mootdx", "tencent", "baostock", "eastmoney", "akshare"):
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
        _migrate_config(self._cfg)
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
