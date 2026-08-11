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

# 加载项目根 .env(如 LIXINGER_TOKEN / LLM_API_KEY 等). 不覆盖已存在的环境变量,
# 保证外部注入(容器/IDE/手动 export)优先. dotenv 为可选依赖, 缺失时静默跳过.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
try:
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    load_dotenv(_PROJECT_ROOT / "backend" / ".env", override=False)
except ImportError:  # pragma: no cover - 可选依赖
    pass

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
        # 量能分段评分(线性, 消除"未过阈值一律0分"的一刀切):
        #   vr <= volume_low_ratio       -> 0 分(明显缩量)
        #   low < vr < threshold         -> 0~5 分(线性)
        #   vr >= threshold              -> 5~10 分(线性, vr=threshold+3 满分)
        "volume_low_ratio": 0.5,
        # 量价配合分段: 收阳且 vr>threshold +10 / 收阳且 vr>=mild +5 / 缩量收阳(惜售) +2 / 收阴 0
        "volume_mild_ratio": 0.8,
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
        # 用指数成分股把 5000+ 全A 缩到质量池(数据源: baostock)
        # all / hs300 / zz500 / hs300+zz500(≈中证800) / sz50
        # 2026-08-10: 默认改为 sz50(全A扫描串行小时级; 上证50池 ~50 只秒级完成)
        "universe": "sz50",
        "max_age_days": 7,       # 成分股缓存超过 N 天自动刷新
        "fallback_on_empty": True,  # 成分股取不到时是否放行全池(False=直接空结果)
    },
    "基本面因子": {
        # 把纯动量升级为"动量 + 质量"(数据源: baostock 季度财报)
        # 短线定位(2026-08-09 用户拍板): 财报硬过滤默认不触发(滞后指标不误杀题材/亏损强势票),
        # 仅保留 ST 实时排除 + 质量加分(bonus_max=5 封顶); 需财报过滤时上调阈值即可。
        "enabled": True,
        "mode": "both",              # filter 只过滤 / score 只加分 / both
        "min_roe": 0.0,              # ROE(%) 下限, 0=不触发
        "max_liability_to_asset": 100.0,  # 资产负债率(%) 上限, 100=不触发
        "min_yoy_ni": -999.0,        # 归母净利润同比(%) 下限, 极小值=不触发
        "max_pe_ttm": 0.0,           # PE(TTM) 上限, <=0 表示不限
        "exclude_negative_pe": False,  # 不剔除亏损股(短线题材票可为负 PE)
        "exclude_st": True,          # 剔除 baostock 标记的 ST(实时状态, 非滞后财报)
        "bonus_max": 5.0,            # 质量分最高加成(叠加到总分)
        "require_data": False,       # True=无基本面数据直接剔除; False=放行(缺数据不惩罚)
    },
    "业绩事件": {
        # 业绩预告/快报催化(数据源: baostock)
        "enabled": True,
        "lookback_days": 3,      # 只认近 3 天内的披露事件(短线催化剂时效强)
        "min_chg_pct": 30.0,     # 预增幅度达到该值才算"超预期"
        "bonus": 5.0,            # 命中利好加分
        "penalty": -5.0,         # 命中利空减分(预减/预亏/首亏)
    },
    "趋势阶段": {
        # 趋势生命周期识别(方案B, 2026-08-09): 启动/加速/过热/衰竭 四阶段
        # - 启动加分: 识别"刚起趋势"(金叉/ROC转正/短均线刚上穿/ADX首次达标),
        #   让早期票浮进高分区 —— 对应"中间分数/刚起趋势"打法
        # - 过热/衰竭扣分: 乖离过大、量比异常、动能衰竭的票压分
        # - 所有数值参数均在 AI 复盘可调白名单(tuning.WHITELIST)内,
        #   复盘建议可一键采纳热生效, 人工可在设置页修改
        "enabled": True,
        "launch_macd_golden": 2.0,   # 近3根内 MACD 金叉(柱由负转正) 加分
        "launch_roc_turn": 2.0,      # 近3根内 ROC 由负转正 加分
        "launch_ma_cross": 2.0,      # 近3根内 短均线刚上穿中均线 加分
        "launch_adx_first": 1.0,     # ADX 首次达标且走高 加分
        "launch_bonus_max": 5.0,     # 启动加分封顶(防事件叠加虚高)
        "overheat_bias": 10.0,       # 乖离率 >= 此值(%) 触发过热扣分
        "overheat_bias_penalty": 3.0,
        "overheat_rsi_penalty": 2.0, # RSI 过热(>= rsi_overheat) 扣分(动量分已衰减, 象征性再扣)
        "overheat_volume": 3.0,      # 量比 >= 此值 触发过热扣分
        "overheat_volume_penalty": 3.0,
        "exhaust_penalty": 5.0,      # 衰竭期扣分(RSI 超买 + MACD 红柱缩短)
        "rsi_overheat": 75.0,        # 阶段判定: RSI >= 此值视为过热
        "rsi_exhaust": 80.0,         # 阶段判定: RSI >= 此值且红柱缩短视为衰竭
    },
    "数据源": {
        "priority": ["mootdx", "tencent", "baostock", "eastmoney", "lixinger", "akshare"],
        "enabled": {"mootdx": True, "tencent": True, "baostock": True,
                    "eastmoney": True, "akshare": True, "lixinger": True},
        "eastmoney": {
            "interval_sec": 2.0,
            "max_workers": 1,
            "retry": 3,
            "enable_patch": True,
        },
        "lixinger": {
            # 理杏仁开放平台: 申万2021行业分级 + 日线 K 线(前复权)兜底;
            # token 由 .env LIXINGER_TOKEN 注入(接口限流 1000次/分, 36次/秒).
            # 排在 akshare 之前: 前 4 源健康时不会被调用, 请求数天然受控.
            "token": "",
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
        # 复盘记忆 RAG: 历史复盘 embedding 入库, 本次复盘检索相似经验注入两步链
        "embedding": {
            "enabled": False,
            "base_url": "https://api.siliconflow.cn/v1",
            "api_key": "",
            "model": "BAAI/bge-m3",
            "timeout_sec": 30,
        },
    },
    # 盘后 AI 日报: 定时生成「当日回顾 + 明日行动清单」, 全只读, 不产生配置变更
    "日报": {
        "enabled": True,
        "hour": 16,
        "minute": 30,
        "push_webhook": "",  # 企业微信机器人 URL, 留空仅站内通知
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
    "EMBEDDING_BASE_URL": "llm.embedding.base_url",
    "EMBEDDING_API_KEY": "llm.embedding.api_key",
    "EMBEDDING_MODEL": "llm.embedding.model",
    "EMBEDDING_ENABLED": "llm.embedding.enabled",
    "EASTMONEY_INTERVAL_SEC": "数据源.eastmoney.interval_sec",
    "EASTMONEY_MAX_WORKERS": "数据源.eastmoney.max_workers",
    "ENABLE_EASTMONEY_PATCH": "数据源.eastmoney.enable_patch",
    "LIXINGER_TOKEN": "数据源.lixinger.token",
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
    # 2026-08-10: 默认选股池 all -> sz50(全A串行扫描小时级; 上证50秒级),
    # 旧库存过配置的 universe 仍是 all, 一并迁移(想要全A可在设置页显式改回)
    pool = cfg.setdefault("选股池", {})
    if pool.get("universe", "all") == "all":
        pool["universe"] = "sz50"
        logger.info("配置迁移: 默认选股池 all -> sz50")
    enabled.setdefault("lixinger", True)
    if "lixinger" not in priority:
        # 放在 akshare 之前: 付费日线复权稳定, 前 4 源健康时不被打扰(请求数受控)
        if "akshare" in priority:
            priority.insert(priority.index("akshare"), "lixinger")
        else:
            priority.append("lixinger")
        logger.info("配置迁移: 数据源优先级补入 lixinger -> %s", priority)


def _apply_env_overrides(cfg: dict[str, Any]) -> None:
    """把环境变量覆盖写入配置(就地修改).

    空字符串视为未设置(不覆盖): 防止 .env 里留空的变量(如 LLM_API_KEY=)
    在启动时把 DB 中页面保存的 Key 抹掉。
    """
    for env_name, path in _ENV_MAP.items():
        val = os.getenv(env_name)
        if val is None or val == "":
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
    for name in ("mootdx", "tencent", "baostock", "eastmoney", "akshare", "lixinger"):
        flag = os.getenv(f"ENABLE_{name.upper()}")
        if flag is None or flag == "":
            continue
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
