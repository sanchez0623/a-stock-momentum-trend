"""复盘建议 -> 参数调优闭环(方案增补).

把「一句话建议」升级为「一句话 + 可执行参数补丁」, 采纳时经三道闸门校验后热写回配置,
并落一条可回滚的变更记录。

三道闸门
1. 白名单   : 只有 21 个数值字段可被 AI 修改; 风控/仓位/数据源/LLM/手续费 永久禁止;
              枚举(trend_filter)与开关(做T.enable)也排除 —— 那是换策略不是调参。
2. 幅度上限 : 单次相对当前值 ±20%, 超出截断到边界并标注; 同时受字段硬边界约束;
              整数字段自动取整。
3. 全局校验 : 应用后整份配置必须整体合法(均线递增/MACD快慢/权重和=1/止盈档递增...),
              防止单参数合规但组合起来把系统调坏。

额外护栏(防多轮累积漂移)
- 同一字段 7 天内只能调一次(给参数留观察窗口)。
- 相对出厂默认值累积偏离 ≤ ±50%, 到顶后需人工去设置页改。
- 每次复盘最多采纳 3 条带补丁的建议。

评分权重特例: 五项和恒为 1, 单改一项必然破坏约束, 因此整组一起改并按比例归一化。
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from app import db
from app.core.config import DEFAULT_CONFIG, config_manager
from app.models.models import ConfigChange

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 护栏常量
MAX_STEP_PCT = 0.20        # 单次相对现值最大变动
MAX_DRIFT_PCT = 0.50       # 相对出厂默认值累积最大偏离
COOLDOWN_DAYS = 7          # 同字段冷却天数
MAX_ACCEPT_PER_REVIEW = 3  # 单次复盘最多采纳的带补丁建议数

FORBIDDEN_GROUPS = ("风控", "仓位", "数据源", "llm", "手续费")
WEIGHT_GROUP = "评分权重"


@dataclass(frozen=True)
class FieldRule:
    """白名单字段的类型与硬边界."""

    kind: str    # int | float
    lo: float
    hi: float
    label: str


# 21 个可调数值字段(排除 趋势.trend_filter 枚举 与 做T.enable 开关)
WHITELIST: dict[str, FieldRule] = {
    # 趋势 4
    "趋势.ma_short":                FieldRule("int", 3, 60, "短均线周期"),
    "趋势.ma_mid":                  FieldRule("int", 5, 120, "中均线周期"),
    "趋势.ma_long":                 FieldRule("int", 10, 250, "长均线周期"),
    "趋势.adx_threshold":           FieldRule("int", 10, 50, "ADX 阈值"),
    # 动量 7
    "动量.roc_period":              FieldRule("int", 3, 60, "ROC 周期"),
    "动量.rsi_period":              FieldRule("int", 5, 40, "RSI 周期"),
    "动量.rsi_overbought":          FieldRule("int", 55, 90, "RSI 超买线"),
    "动量.rsi_oversold":            FieldRule("int", 10, 45, "RSI 超卖线"),
    "动量.macd_fast":               FieldRule("int", 3, 30, "MACD 快线"),
    "动量.macd_slow":               FieldRule("int", 10, 60, "MACD 慢线"),
    "动量.macd_signal":             FieldRule("int", 3, 20, "MACD 信号线"),
    # 量能 2
    "量能.volume_ma":               FieldRule("int", 5, 60, "成交量均线周期"),
    "量能.volume_ratio_threshold":  FieldRule("float", 0.8, 5.0, "量比阈值"),
    # 评分权重 5
    "评分权重.timing":              FieldRule("float", 0.02, 0.60, "时机权重"),
    "评分权重.position":            FieldRule("float", 0.02, 0.60, "仓位权重"),
    "评分权重.stop":                FieldRule("float", 0.02, 0.60, "止损权重"),
    "评分权重.profit":              FieldRule("float", 0.02, 0.60, "止盈权重"),
    "评分权重.discipline":          FieldRule("float", 0.02, 0.60, "纪律权重"),
    # 做T 3
    "做T.min_swing_pct":            FieldRule("float", 0.3, 8.0, "最小波幅(%)"),
    "做T.support_lookback":         FieldRule("int", 2, 30, "支撑回看天数"),
    "做T.t_position_ratio":         FieldRule("float", 0.05, 0.8, "做T仓位比例"),
}

GUARD_LABEL: dict[str, str] = {
    "ok": "可执行",
    "clamped": "已收敛",
    "not_whitelisted": "不可执行",
    "cooldown": "冷却中",
    "drift_limit": "已达累积上限",
    "invalid": "非法",
    "no_change": "无变化",
}


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _num(kind: str, v: float) -> float | int:
    """按字段类型规整数值(整数取整, 浮点定精度)."""
    if kind == "int":
        return int(round(v))
    return round(float(v), 4)


def _fmt(v: Any) -> str:
    if isinstance(v, float) and not v.is_integer():
        return f"{v:g}"
    return str(int(v)) if isinstance(v, (int, float)) else str(v)


def _factory_value(group: str, key: str) -> float | None:
    val = DEFAULT_CONFIG.get(group, {}).get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return float(val)


# ---------------------------------------------------------------- 全局校验
def validate_config(cfg: dict[str, Any]) -> list[str]:
    """跨字段全局校验, 返回错误列表(空表示合法). 与设置页规则保持一致."""
    errs: list[str] = []

    def g(name: str) -> dict:
        v = cfg.get(name)
        return v if isinstance(v, dict) else {}

    t, m, v_, r, p, w, tt = (g("趋势"), g("动量"), g("量能"), g("风控"),
                             g("仓位"), g(WEIGHT_GROUP), g("做T"))

    # 趋势
    ms, mm, ml = t.get("ma_short"), t.get("ma_mid"), t.get("ma_long")
    if all(isinstance(x, (int, float)) for x in (ms, mm, ml)):
        if not (ms < mm < ml):
            errs.append(f"均线周期须递增: 短{ms} < 中{mm} < 长{ml}")
    # 动量
    mf, msl = m.get("macd_fast"), m.get("macd_slow")
    if isinstance(mf, (int, float)) and isinstance(msl, (int, float)) and mf >= msl:
        errs.append(f"MACD 快线({mf})须小于慢线({msl})")
    ob, os_ = m.get("rsi_overbought"), m.get("rsi_oversold")
    if isinstance(ob, (int, float)) and isinstance(os_, (int, float)) and os_ >= ob:
        errs.append(f"RSI 超卖线({os_})须小于超买线({ob})")
    # 量能
    if isinstance(v_.get("volume_ma"), (int, float)) and v_["volume_ma"] <= 0:
        errs.append("成交量均线周期须为正")
    # 风控
    sp, tp = r.get("single_position_pct"), r.get("total_position_pct")
    if isinstance(sp, (int, float)) and isinstance(tp, (int, float)) and sp > tp:
        errs.append(f"单票仓位上限({sp}%)不得超过总仓位上限({tp}%)")
    # 仓位
    pr = p.get("pyramid_ratios")
    if isinstance(pr, list) and pr and abs(sum(float(x) for x in pr) - 1.0) > 1e-3:
        errs.append(f"金字塔加仓比例之和须为 1(当前 {sum(float(x) for x in pr):.3f})")
    tpr = p.get("take_profit_ratios")
    if isinstance(tpr, list) and tpr and sum(float(x) for x in tpr) > 1 + 1e-6:
        errs.append("各档减仓比例之和不得超过 1")
    for name in ("take_profit_levels", "atr_multipliers"):
        seq = p.get(name)
        if isinstance(seq, list) and len(seq) > 1:
            vals = [float(x) for x in seq]
            if vals != sorted(vals):
                errs.append(f"{name} 须递增")
    # 评分权重
    if w:
        total = sum(float(x) for x in w.values() if isinstance(x, (int, float)))
        if abs(total - 1.0) > 1e-3:
            errs.append(f"评分权重之和须为 1.000(当前 {total:.3f})")
    # 做T
    tr = tt.get("t_position_ratio")
    if isinstance(tr, (int, float)) and not (0 < tr <= 1):
        errs.append("做T仓位比例须在 0~1 之间")
    # 数据源
    ds_enabled = g("数据源").get("enabled")
    if isinstance(ds_enabled, dict) and not any(bool(x) for x in ds_enabled.values()):
        errs.append("至少需启用一个数据源")
    return errs


# ---------------------------------------------------------------- 冷却查询
def _last_active_change(group: str, key: str, session: Session | None = None) -> ConfigChange | None:
    """取该字段最近一条未撤销的变更."""
    def _q(s: Session) -> ConfigChange | None:
        stmt = (select(ConfigChange)
                .where(ConfigChange.group == group, ConfigChange.key == key,
                       ConfigChange.status == "active")
                .order_by(ConfigChange.time.desc()).limit(1))
        return s.exec(stmt).first()

    if session is not None:
        return _q(session)
    with db.session_scope() as s:
        return _q(s)


def days_since(time_str: str) -> float:
    """距离给定时间戳过去了多少天(解析失败视为极久远)."""
    try:
        then = dt.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return 1e9
    return (dt.datetime.now() - then).total_seconds() / 86400.0


_days_since = days_since  # 向后兼容内部调用


# ---------------------------------------------------------------- 权重归一化
def _weight_group_patch(key: str, target: float, cur_group: dict[str, Any]) -> dict[str, float]:
    """评分权重整组改: 目标项设为 target, 其余四项按原比例缩放到 1-target."""
    others = {k: float(v) for k, v in cur_group.items()
              if k != key and isinstance(v, (int, float))}
    rest = sum(others.values())
    remain = 1.0 - target
    if rest <= 0 or remain <= 0:
        raise ValueError("评分权重无法归一化(其余项之和为 0 或目标值过大)")
    out = {k: round(v / rest * remain, 3) for k, v in others.items()}
    out[key] = round(target, 3)
    # 归一化后可能有 0.001 级误差, 补到目标项之外最大的一项上
    drift = round(1.0 - sum(out.values()), 3)
    if abs(drift) >= 0.001:
        victim = max((k for k in out if k != key), key=lambda k: out[k])
        out[victim] = round(out[victim] + drift, 3)
    return out


# ---------------------------------------------------------------- 闸门评估
def evaluate_patch(patch: dict[str, Any], session: Session | None = None) -> dict[str, Any]:
    """对一条参数补丁跑完三道闸门 + 累积护栏, 返回可直接落库的评估结果.

    返回: {ok, guard, message, group, key, label, from, to, requested_to,
           patch_json, revert_json}
    """
    group = str(patch.get("group", "") or "")
    key = str(patch.get("key", "") or "")
    path = f"{group}.{key}"
    base: dict[str, Any] = {
        "ok": False, "guard": "invalid", "message": "",
        "group": group, "key": key, "label": key,
        "from": None, "to": None, "requested_to": patch.get("to"),
        "patch_json": "{}", "revert_json": "{}",
    }

    # ---- 闸门 1: 白名单
    rule = WHITELIST.get(path)
    if rule is None:
        reason = (f"「{group}」属受保护分组, 该组参数直接决定亏损与下单量, 不开放给 AI 修改"
                  if group in FORBIDDEN_GROUPS
                  else f"{path} 不在可调白名单内(枚举/开关类字段属换策略而非调参, 请到设置页手动改)")
        base.update(guard="not_whitelisted", message=reason)
        return base
    base["label"] = rule.label

    cfg = config_manager.get()
    cur_raw = cfg.get(group, {}).get(key)
    if isinstance(cur_raw, bool) or not isinstance(cur_raw, (int, float)):
        base.update(message=f"{path} 当前值非数值, 无法调整")
        return base
    cur = float(cur_raw)
    base["from"] = _num(rule.kind, cur)

    try:
        want = float(patch.get("to"))
    except (TypeError, ValueError):
        base.update(message="建议的目标值不是合法数值")
        return base

    notes: list[str] = []

    # ---- 闸门 2: 幅度上限(相对现值 ±20%) + 字段硬边界
    span_base = abs(cur) if cur != 0 else abs(_factory_value(group, key) or 1.0)
    lo_step, hi_step = cur - span_base * MAX_STEP_PCT, cur + span_base * MAX_STEP_PCT
    clamped = min(max(want, lo_step), hi_step)
    if abs(clamped - want) > 1e-9:
        notes.append(f"超出单次 ±{int(MAX_STEP_PCT * 100)}% 上限, 已收敛至 {_fmt(_num(rule.kind, clamped))}")
    hard = min(max(clamped, rule.lo), rule.hi)
    if abs(hard - clamped) > 1e-9:
        notes.append(f"超出字段合理区间 [{_fmt(rule.lo)}, {_fmt(rule.hi)}], 已收敛")
    to_val = _num(rule.kind, hard)
    base["to"] = to_val

    if abs(float(to_val) - cur) < (0.5 if rule.kind == "int" else 1e-6):
        base.update(guard="no_change",
                    message="收敛后与当前值相同(整数字段变动不足 1 档), 无需调整")
        return base

    # ---- 累积漂移护栏(相对出厂默认值 ±50%)
    factory = _factory_value(group, key)
    if factory:
        drift = abs(float(to_val) - factory) / abs(factory)
        if drift > MAX_DRIFT_PCT + 1e-9:
            base.update(
                guard="drift_limit",
                message=(f"相对出厂默认值 {_fmt(factory)} 已偏离 {drift * 100:.0f}%, "
                         f"超过 ±{int(MAX_DRIFT_PCT * 100)}% 累积上限, 需到设置页手动确认"),
            )
            return base

    # ---- 冷却护栏(同字段 7 天一次)
    last = _last_active_change(group, key, session)
    if last is not None:
        elapsed = _days_since(last.time)
        if elapsed < COOLDOWN_DAYS:
            base.update(
                guard="cooldown",
                message=(f"该参数 {last.time[:10]} 刚调整过({_fmt(last.from_value)}→{_fmt(last.to_value)}), "
                         f"冷却期还剩 {COOLDOWN_DAYS - elapsed:.1f} 天 —— 留出观察窗口才分得清是调参有效还是行情变了"),
            )
            return base

    # ---- 构造补丁(评分权重走整组归一化)
    if group == WEIGHT_GROUP:
        cur_group = {k: v for k, v in cfg.get(WEIGHT_GROUP, {}).items()
                     if isinstance(v, (int, float)) and not isinstance(v, bool)}
        try:
            new_group = _weight_group_patch(key, float(to_val), cur_group)
        except ValueError as exc:
            base.update(message=str(exc))
            return base
        applied = {WEIGHT_GROUP: new_group}
        revert = {WEIGHT_GROUP: copy.deepcopy(cur_group)}
        others = ", ".join(f"{k} {_fmt(cur_group[k])}→{_fmt(new_group[k])}"
                           for k in new_group if k != key)
        notes.append(f"权重和恒为 1, 已整组归一化({others})")
    else:
        applied = {group: {key: to_val}}
        revert = {group: {key: _num(rule.kind, cur)}}

    # ---- 闸门 3: 全局校验(整份配置必须整体合法)
    candidate = copy.deepcopy(cfg)
    for gk, gv in applied.items():
        candidate.setdefault(gk, {}).update(gv)
    errs = validate_config(candidate)
    if errs:
        base.update(guard="invalid",
                    message="应用后配置整体不合法: " + "; ".join(errs[:2]))
        return base

    base.update(
        ok=True,
        guard="clamped" if notes else "ok",
        message="; ".join(notes),
        patch_json=json.dumps(applied, ensure_ascii=False),
        revert_json=json.dumps(revert, ensure_ascii=False),
    )
    return base


# ---------------------------------------------------------------- 应用与回滚
def apply_patch(ev: dict[str, Any], source: str = "", review_id: int | None = None,
                suggestion_index: int | None = None,
                session: Session | None = None) -> ConfigChange:
    """执行已通过闸门的补丁: 热写回配置 + 落变更记录."""
    if not ev.get("ok"):
        raise ValueError(ev.get("message") or "补丁未通过校验")
    applied = json.loads(ev["patch_json"])
    config_manager.update(applied)  # 深合并 + 写库 + 通知监听器(热生效)

    change = ConfigChange(
        group=ev["group"], key=ev["key"], label=ev.get("label", ev["key"]),
        from_value=float(ev["from"]), to_value=float(ev["to"]),
        patch_json=ev["patch_json"], revert_json=ev["revert_json"],
        source=source, review_id=review_id, suggestion_index=suggestion_index,
        status="active", note=ev.get("message", ""),
    )
    if session is not None:
        session.add(change)
        session.commit()
        session.refresh(change)
    else:
        with db.session_scope() as s:
            s.add(change)
            s.commit()
            s.refresh(change)
    logger.info("参数调优生效: %s.%s %s -> %s (来源 %s)",
                change.group, change.key, _fmt(change.from_value), _fmt(change.to_value), source)
    return change


def revert_change(change_id: int, session: Session | None = None) -> ConfigChange:
    """一键撤销: 把配置恢复到该次变更之前的值."""
    def _do(s: Session) -> ConfigChange:
        change = s.get(ConfigChange, change_id)
        if change is None:
            raise ValueError("变更记录不存在")
        if change.status == "reverted":
            raise ValueError("该变更已撤销过")
        config_manager.update(json.loads(change.revert_json or "{}"))
        change.status = "reverted"
        change.reverted_at = _now()
        s.commit()
        s.refresh(change)
        logger.info("参数调优已撤销: %s.%s 恢复为 %s",
                    change.group, change.key, _fmt(change.from_value))
        return change

    if session is not None:
        return _do(session)
    with db.session_scope() as s:
        return _do(s)


def list_changes(limit: int = 50, session: Session | None = None) -> list[ConfigChange]:
    def _q(s: Session) -> list[ConfigChange]:
        return list(s.exec(select(ConfigChange)
                           .order_by(ConfigChange.time.desc()).limit(limit)).all())

    if session is not None:
        return _q(session)
    with db.session_scope() as s:
        return _q(s)


def count_applied_for_review(review_id: int, session: Session | None = None) -> int:
    """该次复盘已生效(未撤销)的补丁数, 用于 3 条上限."""
    def _q(s: Session) -> int:
        return len(list(s.exec(select(ConfigChange).where(
            ConfigChange.review_id == review_id, ConfigChange.status == "active")).all()))

    if session is not None:
        return _q(session)
    with db.session_scope() as s:
        return _q(s)


# ---------------------------------------------------------------- 规则通道
def _mk(group: str, key: str, ratio: float, text: str, source: str,
        cfg: dict[str, Any]) -> dict[str, Any] | None:
    """按相对比例生成一条候选补丁(ratio 为正=调高, 负=调低)."""
    rule = WHITELIST.get(f"{group}.{key}")
    cur = cfg.get(group, {}).get(key)
    if rule is None or isinstance(cur, bool) or not isinstance(cur, (int, float)):
        return None
    target = _num(rule.kind, float(cur) * (1 + ratio))
    if float(target) == float(cur):  # 整数字段比例太小导致无变化时, 至少动 1 档
        target = _num(rule.kind, float(cur) + (1 if ratio > 0 else -1))
    return {
        "text": text,
        "patch": {"group": group, "key": key, "from": _num(rule.kind, float(cur)), "to": target},
        "source": source,
    }


def suggest_from_issues(issues: list[dict[str, Any]], stats: dict[str, Any],
                        cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """规则通道: 把 diagnose() 的问题码确定性映射为带补丁的建议.

    不依赖 LLM, 无需 API Key —— 这是没配 Key 时唯一能产出可执行建议的通道。
    """
    cfg = cfg or config_manager.get()
    counts = Counter(i.get("code", "") for i in issues)
    closed = int(stats.get("closed", 0) or 0)
    win_rate = float(stats.get("win_rate", 0) or 0)

    cands: list[dict[str, Any] | None] = []

    n = counts.get("counter_trend", 0)
    if n >= 1:
        cands.append(_mk("趋势", "adx_threshold", 0.15,
                         f"近期有 {n} 笔逆势买入(买在均线空头排列时)。建议提高 ADX 趋势门槛, "
                         f"让选股与首仓信号只在趋势明确成立时才触发, 过滤震荡票。",
                         f"rule:counter_trend×{n}", cfg))

    n = counts.get("chase_high", 0)
    if n >= 2:
        cands.append(_mk("动量", "rsi_overbought", -0.10,
                         f"近期有 {n} 笔追高买入(成交价高于当日收盘 2% 以上)。建议下调 RSI 超买线, "
                         f"让系统更早把高位标的判为超买并给出风险提示。",
                         f"rule:chase_high×{n}", cfg))

    n = counts.get("stop_loss_ignored", 0)
    if n >= 1:
        cands.append(_mk(WEIGHT_GROUP, "stop", 0.20,
                         f"出现 {n} 次触发止损信号后未执行。建议提高复盘评分中「止损」维度的权重, "
                         f"让违反止损纪律在评分上被更重地扣分(仅影响复盘打分, 不改变交易参数)。",
                         f"rule:stop_loss_ignored×{n}", cfg))

    n = counts.get("over_trading", 0)
    if n >= 2:
        cands.append(_mk("做T", "min_swing_pct", 0.20,
                         f"有 {n} 个标的当日交易超过 2 笔。建议提高做T的最小波幅要求, "
                         f"只有振幅足够大才给做T信号, 减少来回摩擦成本。",
                         f"rule:over_trading×{n}", cfg))

    if closed >= 5 and win_rate < 40:
        cands.append(_mk("量能", "volume_ratio_threshold", 0.15,
                         f"近期已平仓 {closed} 笔, 胜率仅 {win_rate}%。建议提高量比阈值, "
                         f"要求上涨必须有量能配合, 提高选股门槛以减少无效信号。",
                         f"rule:low_win_rate({win_rate}%)", cfg))

    # 同一字段只保留优先级最高的一条(issues 已按严重度排序)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in cands:
        if c is None:
            continue
        path = f"{c['patch']['group']}.{c['patch']['key']}"
        if path in seen:
            continue
        seen.add(path)
        c["status"] = "pending"
        out.append(c)
    return out


# ---------------------------------------------------------------- LLM 辅助
def tunable_brief(cfg: dict[str, Any] | None = None) -> str:
    """给 LLM 的可调参数清单(含当前值与允许区间), 注入 prompt 用."""
    cfg = cfg or config_manager.get()
    lines: list[str] = []
    for path, rule in WHITELIST.items():
        group, key = path.split(".", 1)
        cur = cfg.get(group, {}).get(key)
        if not isinstance(cur, (int, float)) or isinstance(cur, bool):
            continue
        lo = max(rule.lo, float(cur) * (1 - MAX_STEP_PCT))
        hi = min(rule.hi, float(cur) * (1 + MAX_STEP_PCT))
        lines.append(f'  {{"group":"{group}","key":"{key}"}} {rule.label} 当前={_fmt(_num(rule.kind, float(cur)))} '
                     f'本次允许区间=[{_fmt(_num(rule.kind, lo))}, {_fmt(_num(rule.kind, hi))}]')
    return "\n".join(lines)


def sanitize_llm_patch(raw: Any) -> dict[str, Any] | None:
    """校验 LLM 吐出的 patch 结构, 非法直接丢弃(降级为纯文本建议)."""
    if not isinstance(raw, dict):
        return None
    group, key = str(raw.get("group", "")), str(raw.get("key", ""))
    if f"{group}.{key}" not in WHITELIST:
        return None
    try:
        to = float(raw.get("to"))
    except (TypeError, ValueError):
        return None
    cur = config_manager.get().get(group, {}).get(key)
    rule = WHITELIST[f"{group}.{key}"]
    return {"group": group, "key": key,
            "from": _num(rule.kind, float(cur)) if isinstance(cur, (int, float)) else None,
            "to": _num(rule.kind, to)}
