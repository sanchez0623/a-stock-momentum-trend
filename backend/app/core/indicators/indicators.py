"""技术指标库(纯函数).

约定:
- 输入 DataFrame 必须含列: date, open, high, low, close, volume, amount
- 全部为纯函数: 输入行情 -> 输出带新列的 DataFrame, 无副作用
- 输出列名统一小写, 如 ma10 / rsi14 / adx14
- 指标需足够历史数据才出值, 前段为 NaN, 由调用方处理

参考设计方案 §4.2.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ================================================================ 趋势类
def ma(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """简单移动平均."""
    df = df.copy()
    df[f"ma{n}"] = df["close"].rolling(n).mean()
    return df


def ema(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """指数移动平均."""
    df = df.copy()
    df[f"ema{n}"] = df["close"].ewm(span=n, adjust=False).mean()
    return df


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """平均趋向指数 ADX( Wilder ). 返回 adx_n / pdi_n / mdi_n."""
    df = df.copy()
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    # Wilder 平滑 (alpha=1/n)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df[f"adx{n}"] = dx.ewm(alpha=1 / n, adjust=False).mean()
    df[f"pdi{n}"] = plus_di
    df[f"mdi{n}"] = minus_di
    return df


def donchian(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """唐奇安通道: 近 n 日最高/最低."""
    df = df.copy()
    df[f"dc_upper{n}"] = df["high"].rolling(n).max()
    df[f"dc_lower{n}"] = df["low"].rolling(n).min()
    return df


# ================================================================ 动量类
def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD. 返回 dif / dea / macd_hist(柱=2*(dif-dea), 国内口径)."""
    df = df.copy()
    close = df["close"]
    dif = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=signal, adjust=False).mean()
    df["dif"] = dif
    df["dea"] = dea
    df["macd_hist"] = (dif - dea) * 2
    return df


def rsi(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """相对强弱指标( Wilder 平滑 )."""
    df = df.copy()
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_series = 100 - 100 / (1 + rs)
    # avg_loss=0 且 avg_gain>0 时视为 100
    rsi_series = rsi_series.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    df[f"rsi{n}"] = rsi_series
    return df


def roc(df: pd.DataFrame, n: int = 12) -> pd.DataFrame:
    """变动速率: (今收 - n日前收) / n日前收 * 100."""
    df = df.copy()
    df[f"roc{n}"] = df["close"].pct_change(n) * 100
    return df


# ================================================================ 波动类
def bollinger(df: pd.DataFrame, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    """布林带."""
    df = df.copy()
    mid = df["close"].rolling(n).mean()
    std = df["close"].rolling(n).std()
    df[f"boll_mid{n}"] = mid
    df[f"boll_upper{n}"] = mid + k * std
    df[f"boll_lower{n}"] = mid - k * std
    return df


def atr(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """平均真实波幅(简单均值口径, 与 ADX 内部 Wilder 口径不同, 注意区分)."""
    df = df.copy()
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    df[f"atr{n}"] = tr.rolling(n).mean()
    return df


# ================================================================ 量能类
def volume_ratio(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """量比: 当日成交量 / 前 n 日均量(不含当日). 大于 1 为放量."""
    df = df.copy()
    baseline = df["volume"].rolling(n).mean().shift(1)
    df[f"volume_ratio{n}"] = df["volume"] / baseline.replace(0, np.nan)
    return df


# ================================================================ 聚合入口
def compute_all(
    df: pd.DataFrame,
    *,
    ma_short: int = 10,
    ma_mid: int = 20,
    ma_long: int = 60,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    rsi_period: int = 14,
    adx_period: int = 14,
    roc_period: int = 12,
    boll_period: int = 20,
    boll_k: float = 2.0,
    donchian_period: int = 20,
    volume_ma: int = 20,
    atr_period: int = 14,
) -> pd.DataFrame:
    """按配置参数一次算全所有指标, 返回含全部指标列的 DataFrame."""
    df = df.copy()
    df = ma(df, ma_short)
    df = ma(df, ma_mid)
    df = ma(df, ma_long)
    df = ema(df, ma_short)
    df = ema(df, ma_mid)
    df = macd(df, macd_fast, macd_slow, macd_signal)
    df = rsi(df, rsi_period)
    df = adx(df, adx_period)
    df = roc(df, roc_period)
    df = bollinger(df, boll_period, boll_k)
    df = donchian(df, donchian_period)
    df = volume_ratio(df, volume_ma)
    df = atr(df, atr_period)
    return df
