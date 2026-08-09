"""重现 600111 北方稀土选股评分链路, 验证 total 与明细之差的根因.

用法: 在 backend/ 目录下运行 (backend/.venv/Scripts/python.exe scripts/reproduce_600111_score.py)
"""
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.indicators import compute_all  # noqa: E402
from app.core.screener.engine import score_indicators  # noqa: E402
from app.core.fundamentals import (  # noqa: E402
    evaluate_events,
    evaluate_quality,
    load_fundamentals_map,
    load_recent_events,
)

DB = Path(__file__).resolve().parent.parent / "data" / "trading.db"
con = sqlite3.connect(DB)
cur = con.cursor()

row = cur.execute("SELECT ohlcv_json FROM klinecache WHERE symbol='600111' AND period='daily'").fetchone()
bars = json.loads(row[0])
df = pd.DataFrame(bars)
for c in ("open", "high", "low", "close", "volume", "amount"):
    df[c] = pd.to_numeric(df[c], errors="coerce")
print(f"K线根数: {len(df)}  最后一根: {df.iloc[-1]['date']}")

cfg = json.loads(cur.execute("SELECT data_json FROM configrow WHERE id=1").fetchone()[0])
ind = compute_all(
    df,
    ma_short=cfg["趋势"]["ma_short"], ma_mid=cfg["趋势"]["ma_mid"], ma_long=cfg["趋势"]["ma_long"],
    macd_fast=cfg["动量"]["macd_fast"], macd_slow=cfg["动量"]["macd_slow"], macd_signal=cfg["动量"]["macd_signal"],
    rsi_period=cfg["动量"]["rsi_period"], roc_period=cfg["动量"]["roc_period"],
    volume_ma=cfg["量能"]["volume_ma"],
)
score = score_indicators(ind, cfg)
keys = ("trend_score", "momentum_score", "volume_score", "total", "adx", "roc", "rsi",
        "volume_ratio", "bias", "consistency", "stage", "stage_bonus", "stage_penalty", "attention")
print("三因子评分:", {k: score[k] for k in keys})
print("detail(趋势):", score.get("detail", {}).get("趋势", "")[:200])

q_cfg = cfg["基本面因子"]
fund_map = load_fundamentals_map(["600111"])
q = evaluate_quality(fund_map.get("600111"), q_cfg)
print("基本面质量:", {k: q[k] for k in ("score", "passed", "has_data", "tags", "risks")} if isinstance(q, dict) else q)

e_cfg = cfg["业绩事件"]
event_map = load_recent_events(["600111"], int(e_cfg.get("lookback_days", 3)))
ev = evaluate_events(event_map.get("600111", []), e_cfg)
print("业绩事件:", {k: ev[k] for k in ("delta", "tags", "notes")} if isinstance(ev, dict) else ev)

q_delta = q["score"] if (q_cfg.get("enabled") and q_cfg.get("mode") in ("score", "both") and q.get("has_data")) else 0.0
e_delta = ev["delta"] if e_cfg.get("enabled") else 0.0
delta = q_delta + e_delta
base = round(score["trend_score"] + score["momentum_score"] + score["volume_score"]
             + score["stage_bonus"] - score["stage_penalty"], 1)
print(f"\n对账: 三因子+阶段 = {base} | 因子加分 delta = {delta} | 最终 total = {round(base + delta, 1)}")
