"""SQLModel 表定义(方案 §5)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> str:
    # 强制东八区(不依赖进程时区): 容器/环境 TZ 可能为 UTC, 历史曾写入非东八区时间
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 基础
class Stock(SQLModel, table=True):
    """股票代码/名称/市场/行业."""

    symbol: str = Field(primary_key=True)
    name: str = Field(default="", index=True)
    market: str = Field(default="", index=True)  # sh/sz/bj
    industry: str = Field(default="")


class StockClassification(SQLModel, table=True):
    """个股分类映射(申万一级/二级/三级 + 行业板块/概念板块).

    数据来源: akshare (申万 via sw_index_* / 板块 via stock_board_*_em).
    用途: 选股器每行业限配(⑤) + 板块动量因子(④进阶) + 风控行业集中度.
    与 Stock.industry(东财 f100 仅一级)互补: 本表是更完整的权威分类, 优先使用.
    """

    symbol: str = Field(primary_key=True)  # 6 位代码
    name: str = Field(default="")
    sw_l1: str = Field(default="", index=True)  # 申万一级
    sw_l2: str = Field(default="", index=True)  # 申万二级
    sw_l3: str = Field(default="", index=True)  # 申万三级
    boards_industry: str = Field(default="[]")  # 行业板块名 JSON list(东财)
    boards_concept: str = Field(default="[]")  # 概念板块名 JSON list(东财)
    source: str = Field(default="")  # 数据来源标记
    updated_at: str = Field(default_factory=_now)


class IndexConstituent(SQLModel, table=True):
    """指数成分股缓存(数据源: baostock).

    用途: 选股 universe 预筛 —— 把 5000+ 只的全A噪声池缩到 300~800 只质量池,
    扫描更快、出票更贴合中大盘动量趋势风格.
    index_key: hs300 / zz500 / sz50
    """

    id: int | None = Field(default=None, primary_key=True)
    index_key: str = Field(default="", index=True)
    symbol: str = Field(default="", index=True)  # 6 位代码
    name: str = Field(default="")
    updated_at: str = Field(default_factory=_now)


class StockFundamental(SQLModel, table=True):
    """季度基本面 + 估值快照(数据源: baostock).

    用途: 把纯价格动量升级为"动量 + 质量" —— 剔除 ROE 低/负债高/业绩下滑的票.
    由后台刷新任务批量填充, 选股时只读本表, 不产生额外网络调用.
    百分比字段统一为百分数口径(roe=12.5 表示 12.5%).
    """

    symbol: str = Field(primary_key=True)
    name: str = Field(default="")
    stat_date: str = Field(default="", index=True)  # 报告期
    pub_date: str = Field(default="")
    roe: float | None = Field(default=None)
    np_margin: float | None = Field(default=None)
    gp_margin: float | None = Field(default=None)
    eps_ttm: float | None = Field(default=None)
    yoy_ni: float | None = Field(default=None)
    yoy_eps: float | None = Field(default=None)
    yoy_equity: float | None = Field(default=None)
    liability_to_asset: float | None = Field(default=None)
    current_ratio: float | None = Field(default=None)
    cfo_to_np: float | None = Field(default=None)
    dupont_roe: float | None = Field(default=None)
    pe_ttm: float | None = Field(default=None)
    pb_mrq: float | None = Field(default=None)
    ps_ttm: float | None = Field(default=None)
    is_st: bool | None = Field(default=None)
    industry: str = Field(default="", index=True)   # 证监会行业(baostock)
    industry_source: str = Field(default="")
    source: str = Field(default="baostock")
    updated_at: str = Field(default_factory=_now)


class EarningsEvent(SQLModel, table=True):
    """业绩预告 / 业绩快报事件(数据源: baostock).

    用途: 动量策略最爱的催化剂 —— 近期业绩超预期的票给动量分加权/打事件标签.
    """

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(default="", index=True)
    kind: str = Field(default="forecast", index=True)  # forecast 预告 / express 快报
    pub_date: str = Field(default="", index=True)
    stat_date: str = Field(default="")
    forecast_type: str = Field(default="")   # 预增/略增/扭亏/预减/预亏/快报...
    chg_pct_up: float | None = Field(default=None)
    chg_pct_down: float | None = Field(default=None)
    abstract: str = Field(default="")
    updated_at: str = Field(default_factory=_now)


class ConfigRow(SQLModel, table=True):
    """全局配置(单行 JSON)."""

    id: int | None = Field(default=1, primary_key=True)
    data_json: str = Field(default="{}")
    updated_at: str = Field(default_factory=_now)


class KlineCache(SQLModel, table=True):
    """K线缓存 (symbol, period, date, ohlcv)."""

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    period: str = Field(index=True)
    date: str = Field(default="")  # 最后一条日期, 用于增量判断
    ohlcv_json: str = Field(default="[]")
    updated_at: str = Field(default="")  # 写入时间(盘中 TTL 判定用; 旧库迁移补列后为空)


class BacktestKline(SQLModel, table=True):
    """回测专用 K 线(方案 v2 §4): 行式存储, 冻结快照, 与实盘 kline_cache 完全隔离.

    - 回测统一前复权(qfq): 区间拉取后**冻结**(同 key 行不随日常增量刷新/重算),
      从根上消除动态前复权的未来函数问题(§4.3);
    - 指数用 symbol = "idx:" + secid(如 idx:0.000300), adjust = raw(指数无复权);
    - force 重拉只允许显式触发(诊断/修复), 常规路径永不覆盖已有行.
    """

    __table_args__ = (
        UniqueConstraint("symbol", "period", "adjust", "date", name="uq_backtest_kline"),
    )

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)  # 6 位代码 / idx:secid
    period: str = Field(default="daily", index=True)
    adjust: str = Field(default="qfq", index=True)  # qfq(前复权) / raw(指数不复权)
    date: str = Field(default="", index=True)  # YYYY-MM-DD
    open: float = Field(default=0.0)
    high: float = Field(default=0.0)
    low: float = Field(default=0.0)
    close: float = Field(default=0.0)
    volume: float = Field(default=0.0)
    amount: float = Field(default=0.0)
    fetched_at: str = Field(default_factory=_now)


class Watchlist(SQLModel, table=True):
    """自选股."""

    symbol: str = Field(primary_key=True)
    name: str = Field(default="")
    added_at: str = Field(default_factory=_now)


# ---------------------------------------------------------------- 交易
class Position(SQLModel, table=True):
    """持仓快照."""

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    name: str = Field(default="")
    qty: int = Field(default=0)  # 股
    cost: float = Field(default=0.0)  # 含费摊薄成本(加权, 已摊入买入手续费), 券商 APP 口径
    cost_raw: float = Field(default=0.0)  # 纯成交均价(不含费), 仅用于顺向加仓判断
    status: str = Field(default="holding", index=True)  # holding / closed
    # 金字塔加仓档位: 已完成的分档数(0=仅首仓, 1=已加1档, 2=已加2档).
    # 取代原先「由成交笔数倒推」的脆弱逻辑, 由 open_or_add 显式维护. 新增列, 旧库迁移默认 0.
    pyramid_stage: int = Field(default=0)
    # 持仓时间(首仓录入时间, 加仓不刷新); 用于 T+1 锁定期判定与列表展示
    opened_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class ScreenerHistory(SQLModel, table=True):
    """选股扫描历史(结果落库, 供前端回看; 内存任务重启即清, 此表持久)."""

    id: int | None = Field(default=None, primary_key=True)
    time: str = Field(default_factory=_now, index=True)
    market: str = Field(default="all", index=True)
    board: str = Field(default="")          # 板块多值(逗号分隔)
    industry: str = Field(default="")       # 行业多值(逗号分隔)
    top_n: int = Field(default=30)
    per_industry: int = Field(default=0)
    industry_level: str = Field(default="sw_l1")
    apply_gate: bool = Field(default=True)
    universe: str = Field(default="")
    apply_factors: bool = Field(default=True)
    total: int = Field(default=0)            # 扫描股票总数
    result_count: int = Field(default=0)     # 命中结果数
    status: str = Field(default="done")
    result_json: str = Field(default="[]")  # 结果列表 JSON(含 detail/reason/tags)
    error: str = Field(default="")


class ScreenerPreset(SQLModel, table=True):
    """选股条件组合预设(指数池 + 板块 + 行业, 一键复用)."""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(default="", index=True)
    universe: str = Field(default="")      # 逗号分隔多值
    board: str = Field(default="")         # 板块多值(逗号分隔)
    industry: str = Field(default="")      # 行业多值(逗号分隔)
    created_at: str = Field(default_factory=_now)


class ScreenerTask(SQLModel, table=True):
    """选股扫描任务持久化(断点续传: 服务重启后任务不丢, 可继续扫描)."""

    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(default="", index=True)  # 与内存任务对应
    status: str = Field(default="running", index=True)  # running/done/failed/interrupted
    market: str = Field(default="all")
    board: str = Field(default="")
    industry: str = Field(default="")
    top_n: int = Field(default=30)
    per_industry: int = Field(default=0)
    industry_level: str = Field(default="sw_l1")
    apply_gate: bool = Field(default=True)
    universe: str = Field(default="")
    apply_factors: bool = Field(default=True)
    symbols_json: str = Field(default="[]")  # 扫描池快照(过滤后), 恢复时用原池
    total: int = Field(default=0)
    done: int = Field(default=0)
    error: str = Field(default="")
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class ScreenerTaskBatch(SQLModel, table=True):
    """扫描结果批次(每批约 N 只增量落库, 恢复时按 seq 合并; 结果即进度)."""

    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(default="", index=True)
    seq: int = Field(default=0)
    data_json: str = Field(default="[]")  # 本批结果列表
    created_at: str = Field(default_factory=_now)


class TrackedStock(SQLModel, table=True):
    """选股得分追踪(从选股结果一键追踪, 每日 3 次采样得分/价格/阶段/信号)."""

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(default="", index=True)
    name: str = Field(default="")
    track_time: str = Field(default_factory=_now)
    score_at_track: float = Field(default=0.0)   # 追踪时的总分
    stage_at_track: str = Field(default="")       # 追踪时的阶段(启动/加速/过热/衰竭)
    status: str = Field(default="active", index=True)  # active / archived(30天到期或手动)
    archived_at: str = Field(default="")
    archive_reason: str = Field(default="")      # manual / expired


class ScorePoint(SQLModel, table=True):
    """追踪采样点(每次采样一条: 总分/三因子/阶段/价格/量比/信号)."""

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(default="", index=True)
    time: str = Field(default_factory=_now)
    score: float = Field(default=0.0)
    trend_score: float = Field(default=0.0)
    momentum_score: float = Field(default=0.0)
    volume_score: float = Field(default=0.0)
    stage: str = Field(default="")
    price: float = Field(default=0.0)
    volume_ratio: float = Field(default=0.0)
    signal_type: str = Field(default="")  # 采样时触发的信号(无则空)
    sample_kind: str = Field(default="")   # pre(盘前) / noon(午间) / after(盘后) / manual


class SignalRecord(SQLModel, table=True):
    """信号记录."""

    id: int | None = Field(default=None, primary_key=True)
    time: str = Field(default_factory=_now, index=True)
    symbol: str = Field(index=True)
    name: str = Field(default="")
    type: str = Field(index=True)  # BUY_FIRST/BUY_ADD/SELL_REDUCE/SELL_STOP/T_BUY/T_SELL
    direction: str = Field(default="")  # buy/sell
    strength: float = Field(default=0.0)  # 0-100
    reason: str = Field(default="")
    indicators_json: str = Field(default="{}")


class Plan(SQLModel, table=True):
    """交易计划."""

    id: int | None = Field(default=None, primary_key=True)
    time: str = Field(default_factory=_now, index=True)
    symbol: str = Field(index=True)
    name: str = Field(default="")
    action: str = Field(default="")  # buy_add / sell_reduce / stop / hold / t_trade
    content: str = Field(default="")  # 人话指引
    status: str = Field(default="pending", index=True)  # pending / done / ignored


class Trade(SQLModel, table=True):
    """成交记录(手动回填)."""

    id: int | None = Field(default=None, primary_key=True)
    time: str = Field(default_factory=_now, index=True)
    symbol: str = Field(index=True)
    name: str = Field(default="")
    action: str = Field(index=True)  # buy/sell
    price: float = Field(default=0.0)
    qty: int = Field(default=0)
    amount: float = Field(default=0.0)
    fee: float = Field(default=0.0)  # 本笔手续费(净额的 pnl 已扣减)
    reason: str = Field(default="")
    signal_strength: float = Field(default=0.0)
    plan_id: int | None = Field(default=None)
    pnl: float = Field(default=0.0)
    score: float | None = Field(default=None)
    note: str = Field(default="")


class RiskState(SQLModel, table=True):
    """风控状态(单行)."""

    id: int | None = Field(default=1, primary_key=True)
    day_loss_tripped: bool = Field(default=False)  # 日亏损熔断
    defense_mode: bool = Field(default=False)  # 防守模式(回撤)
    consecutive_losses: int = Field(default=0)
    last_trade_pnl: float = Field(default=0.0)
    day_pnl: float = Field(default=0.0)
    updated_at: str = Field(default_factory=_now)


class Account(SQLModel, table=True):
    """资金账户(单行).

    start_capital: 启动资金(默认 50w, 可在前端修改).
    可用资金与总权益为前端派生值, 不在后端落库:
      可用资金 = 启动资金 - 持仓市值(实时)
      总权益   = 持仓市值 + 可用资金 = 启动资金
    """

    id: int = Field(default=1, primary_key=True)
    start_capital: float = Field(default=500000.0)
    updated_at: str = Field(default_factory=_now)


# ---------------------------------------------------------------- 复盘与评分
class AiReview(SQLModel, table=True):
    """AI 复盘记录."""

    id: int | None = Field(default=None, primary_key=True)
    time: str = Field(default_factory=_now, index=True)
    range: str = Field(default="")  # 复盘范围, 如 week:2026-08-03
    content: str = Field(default="")  # 复盘正文(规则诊断 + LLM)
    suggestions_json: str = Field(default="[]")  # [{text, status}]
    model: str = Field(default="")
    rule_result_json: str = Field(default="{}")


class ReviewMemory(SQLModel, table=True):
    """复盘记忆条目(RAG): 历史复盘的向量化文本, 供后续复盘检索注入."""

    id: int | None = Field(default=None, primary_key=True)
    review_id: int = Field(index=True)         # 关联 AiReview.id
    time: str = Field(default_factory=_now, index=True)
    text: str = Field(default="")              # 记忆条目文本(问题+建议+采纳结果+效果)
    embedding_json: str = Field(default="[]")  # 向量(JSON 数组)
    model: str = Field(default="")             # embedding 模型名


class DailyReport(SQLModel, table=True):
    """盘后 AI 交易日报(只读: 当日回顾 + 明日行动清单, 不产生配置变更)."""

    id: int | None = Field(default=None, primary_key=True)
    date: str = Field(default="", unique=True, index=True)  # 交易日 YYYY-MM-DD
    content_json: str = Field(default="{}")                 # 结构化内容(含降级模板文本)
    model: str = Field(default="")
    status: str = Field(default="ok")                       # ok / degraded / failed
    created_at: str = Field(default_factory=_now)


class Notification(SQLModel, table=True):
    """站内通知(日报/信号/风控提醒)."""

    id: int | None = Field(default=None, primary_key=True)
    time: str = Field(default_factory=_now, index=True)
    category: str = Field(default="report")  # report / signal / risk / assistant
    title: str = Field(default="")
    content: str = Field(default="")
    fingerprint: str = Field(default="", index=True)  # 去重指纹, 如 2026-08-12:300139:SELL_REDUCE
    read: bool = Field(default=False)


class ConfigChange(SQLModel, table=True):
    """参数变更记录(复盘建议采纳 -> 写回配置), 支持一键回滚与效果追踪骨架."""

    id: int | None = Field(default=None, primary_key=True)
    time: str = Field(default_factory=_now, index=True)
    group: str = Field(default="", index=True)   # 配置分组, 如 趋势
    key: str = Field(default="", index=True)     # 字段名, 如 adx_threshold
    label: str = Field(default="")               # 中文标签, 展示用
    from_value: float = Field(default=0.0)       # 改前值(标量, 展示用)
    to_value: float = Field(default=0.0)         # 改后值(标量, 展示用)
    patch_json: str = Field(default="{}")        # 实际应用的 partial config
    revert_json: str = Field(default="{}")       # 撤销用 partial config
    source: str = Field(default="")              # rule:<code> / llm
    review_id: int | None = Field(default=None, index=True)
    suggestion_index: int | None = Field(default=None)
    status: str = Field(default="active", index=True)  # active / reverted
    reverted_at: str = Field(default="")
    note: str = Field(default="")                # 闸门说明, 如"已按±20%上限收敛"


class Score(SQLModel, table=True):
    """评分."""

    id: int | None = Field(default=None, primary_key=True)
    scope: str = Field(index=True)  # trade / week / month / health
    scope_id: str = Field(default="", index=True)  # 关联 id 或周期标识
    dims_json: str = Field(default="{}")  # 各维度得分
    total: float = Field(default=0.0)
    created_at: str = Field(default_factory=_now)


class DailyStat(SQLModel, table=True):
    """每日净值/回撤快照."""

    date: str = Field(primary_key=True)
    equity: float = Field(default=0.0)
    drawdown: float = Field(default=0.0)
    day_pnl: float = Field(default=0.0)
