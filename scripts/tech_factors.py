#!/usr/bin/env python3
"""
tech_factors.py — 技术面因子独立模块 (Phase 1.1)

从 market_watcher.py 的 calc_indicators() + screen_all_stocks() 提取7个因子:
  trend        (均线趋势,   ±3.5)  MA5/MA10多头/空头排列连续评分
  momentum     (动量,       ±4.0)  5日涨跌幅 × 0.2
  position     (位置,       ±1.0)  10日高低位百分比映射
  volume       (量价配合,    ±1.5)  放量时方向×强度
  atr          (ATR波动率,   ±1.5)  14日ATR百分比, 高波动偏空
  distribution (筹码分布,    ±2.0)  成交量加权均价 vs 现价偏离
  sr           (支撑阻力,    ±1.5)  距10日高/低边界的反转潜力

接口:
  compute_tech_factors(duckdb_conn, ts_code, trade_date) -> dict
  compute_tech_factors_batch(duckdb_conn, ts_codes, trade_date) -> list[dict]
  _discover_duckdb() -> str | None

依赖: duckdb, pandas, numpy (无额外pip包)
"""

import os
import glob

import numpy as np
import pandas as pd

# ====== DuckDB 自动发现 ======

_DB_DIR = r"C:\Users\LarkMi\quant_20260525\每日A股日线行情数据duckDB（日更）"


def _discover_duckdb():
    """自动发现最新 DuckDB 文件（>100MB），回退硬编码路径"""
    try:
        files = sorted(glob.glob(os.path.join(_DB_DIR, "daily_adj_*.duckdb")))
        files = [f for f in files if os.path.getsize(f) > 100 * 1024 * 1024]
        if files:
            return files[-1]
    except Exception:
        pass
    # fallback
    fallback = os.path.join(_DB_DIR, "daily_adj_19901219_20260610.duckdb")
    if os.path.exists(fallback):
        return fallback
    return None


# ====== 数据加载 ======

def _load_stock_data(conn, ts_code: str, trade_date: str, days: int = 15):
    """加载单只股票最近N日数据 (≤trade_date), 按日期升序"""
    # 统一日期格式 YYYYMMDD
    td = trade_date.replace("-", "")
    try:
        df = conn.execute(f"""
            SELECT trade_date, open, high, low, close, vol, pct_chg
            FROM daily
            WHERE ts_code = '{ts_code}'
              AND trade_date <= '{td}'
            ORDER BY trade_date DESC
            LIMIT {days}
        """).df()
        if df.empty:
            return None
        return df.sort_values("trade_date", ascending=True).reset_index(drop=True)
    except Exception:
        return None


def _load_batch_data(conn, ts_codes: list[str], trade_date: str, days: int = 15):
    """批量加载多只股票数据, 返回 {ts_code: DataFrame}"""
    if not ts_codes:
        return {}
    td = trade_date.replace("-", "")
    # 用 IN 子句批量加载
    code_list = "','".join(ts_codes)
    try:
        df = conn.execute(f"""
            SELECT ts_code, trade_date, open, high, low, close, vol, pct_chg
            FROM daily
            WHERE ts_code IN ('{code_list}')
              AND trade_date <= '{td}'
            ORDER BY ts_code, trade_date DESC
        """).df()
        if df.empty:
            return {}
    except Exception:
        return {}

    result = {}
    for code, group in df.groupby("ts_code"):
        g = group.sort_values("trade_date", ascending=True).reset_index(drop=True)
        if len(g) >= 5:
            # 只保留最近 days 行
            result[code] = g.tail(days)
    return result


# ====== 因子1: 均线趋势 (weight=3) ======

def factor_trend(closes: np.ndarray) -> float:
    """MA5/MA10多头空头排列连续评分, 值域 ±3.5

    规则 (继承自 screen_all_stocks):
      - 多头排列 (close > MA5 > MA10): +1.5 + 偏离度×0.3, 上限+3.5
      - 空头排列 (close < MA5 < MA10): -1.5 + 偏离度×0.3, 下限-3.5
      - 仅站上MA5: +0.5 + 偏离度×0.3, 上限+2.0
      - 仅跌破MA5: -0.5 + 偏离度×0.3, 下限-2.0
      - 其他: 0
    """
    n = len(closes)
    if n < 5:
        return 0.0

    latest = float(closes[-1])
    ma5 = float(np.mean(closes[-5:]))
    ma10 = float(np.mean(closes[-10:])) if n >= 10 else ma5

    if ma5 == 0:
        return 0.0

    ma5_dist = (latest - ma5) / ma5 * 100  # 距MA5百分比

    if latest > ma5 > ma10:       # 多头排列
        return min(3.5, 1.5 + ma5_dist * 0.3)
    elif latest < ma5 < ma10:     # 空头排列
        return max(-3.5, -1.5 + ma5_dist * 0.3)
    elif latest > ma5:            # 仅站上MA5
        return min(2.0, 0.5 + ma5_dist * 0.3)
    elif latest < ma5:            # 仅跌破MA5
        return max(-2.0, -0.5 + ma5_dist * 0.3)
    else:
        return 0.0


# ====== 因子2: 动量 (weight=2) ======

def factor_momentum(chg_5d: float) -> float:
    """5日涨跌幅 × 0.2, 值域约 ±4"""
    return chg_5d * 0.2


# ====== 因子3: 位置 (weight=1) ======

def factor_position(close: float, high_10d: float, low_10d: float) -> float:
    """10日高低位百分比 → 连续评分, 值域 ±1

    公式: (pos% - 50) × 0.02
      - pos=100 (10日新高): +1.0  (趋势强势)
      - pos=50  (中位):    0.0
      - pos=0   (10日新低): -1.0  (趋势弱势)
    """
    if high_10d <= low_10d:
        return 0.0
    pos = (close - low_10d) / (high_10d - low_10d) * 100
    return (pos - 50) * 0.02


# ====== 因子4: 量价配合 (weight=1) ======

def factor_volume(vol_ratio: float, chg_1d: float) -> float:
    """放量时量价配合评分, 值域 ±1.5

    规则:
      - 放量 (>1.3倍均量) 且上涨 → +chg_1d×0.15, 上限+1.5
      - 放量 (>1.3倍均量) 且下跌 → +chg_1d×0.15, 下限-1.5
      - 缩量 → 0
    """
    if vol_ratio > 1.3:
        return max(-1.5, min(1.5, chg_1d * 0.15))
    return 0.0


# ====== 因子5: ATR波动率 (NEW, weight=1.5) ======

def factor_atr(atr_pct: float, chg_5d: float) -> float:
    """14日ATR百分比 → 波动率评分, 值域 ±1.5

    逻辑:
      - ATR居中(2-5%): 健康波动, 方向跟随 → ±0.5
      - ATR偏低(<2%): 横盘 → -0.3
      - ATR偏高(>5%): 高风险, 方向放大 → ±1.0
      - 连续映射: base = -0.3*(atr_pct-3.0), trend_bias = 0.08*sign(chg_5d)*|chg_5d|
    """
    # 以ATR=3%为中性的连续评分
    base = -0.3 * (atr_pct - 3.0)  # 偏离3%越多越负
    # 趋势方向加成
    if chg_5d > 0:
        trend_bias = min(0.8, chg_5d * 0.08)
    elif chg_5d < 0:
        trend_bias = max(-0.8, chg_5d * 0.08)
    else:
        trend_bias = 0.0

    score = base + trend_bias
    return max(-1.5, min(1.5, score))


# ====== 因子6: 筹码分布 (NEW, weight=1) ======

def factor_distribution(closes: np.ndarray, vols: np.ndarray) -> float:
    """成交量加权均价 vs 现价偏离, 值域 ±2.0

    公式:
      VWAP = Σ(close_i × vol_i) / Σ(vol_i)
      偏离% = (现价 - VWAP) / VWAP × 100
      评分 = clamp(偏离% × 0.3, -2.0, +2.0)

    含义:
      - 现价 > VWAP: 多头筹码盈利, 看涨 → 正分
      - 现价 < VWAP: 套牢盘压力, 看跌 → 负分
    """
    n = len(closes)
    if n < 5:
        return 0.0

    vwap = np.average(closes.astype(float), weights=vols.astype(float))
    if vwap <= 0:
        return 0.0

    latest = float(closes[-1])
    deviation = (latest - vwap) / vwap * 100
    return max(-2.0, min(2.0, deviation * 0.3))


# ====== 因子7: 支撑阻力位 (NEW, weight=1) ======

def factor_sr(close: float, high_10d: float, low_10d: float) -> float:
    """距10日高/低边界的反转潜力, 值域 ±1.5

    逻辑:
      - 接近低点 (<20%位): 支撑反弹潜力 → 正分
      - 接近高点 (>80%位): 阻力回落风险 → 负分
      - 中位区 (20-80%): 中性 → 0

    连续映射:
      - pos < 20: score = (20 - pos) / 20 × 1.5  (越接近低点分越高)
      - pos > 80: score = -(pos - 80) / 20 × 1.5 (越接近高点分越低)
      - else: 0
    """
    if high_10d <= low_10d:
        return 0.0
    pos = (close - low_10d) / (high_10d - low_10d) * 100
    if pos < 20:
        return (20 - pos) / 20 * 1.5
    elif pos > 80:
        return -(pos - 80) / 20 * 1.5
    else:
        return 0.0


# ====== 提取原始指标 (兼容 calc_indicators) ======

def _extract_indicators(df: pd.DataFrame) -> dict:
    """从DataFrame提取原始技术指标 (供调试/兼容)"""
    if df is None or len(df) < 5:
        return {}

    closes = df["close"].values.astype(float)
    vols = df["vol"].values.astype(float)
    highs = df["high"].values.astype(float)
    lows = df["low"].values.astype(float)
    chgs = df["pct_chg"].values.astype(float)
    latest = float(closes[-1])

    ma5 = float(np.mean(closes[-5:]))
    ma10 = float(np.mean(closes[-10:])) if len(closes) >= 10 else ma5

    high_10d = float(np.max(highs[-10:])) if len(highs) >= 10 else latest
    low_10d = float(np.min(lows[-10:])) if len(lows) >= 10 else latest
    pos = (latest - low_10d) / (high_10d - low_10d) * 100 if high_10d > low_10d else 50.0

    vol_ratio = float(vols[-1]) / float(np.mean(vols[-6:-1])) if len(vols) >= 6 and np.mean(vols[-6:-1]) > 0 else 1.0

    chg_1d = float(chgs[-1])
    chg_3d = float(np.sum(chgs[-3:])) if len(chgs) >= 3 else 0.0
    chg_5d = float(np.sum(chgs[-5:])) if len(chgs) >= 5 else 0.0

    # ATR计算
    if len(closes) >= 14:
        tr_vals = []
        for i in range(1, min(15, len(closes))):
            h, l, pc = highs[-i], lows[-i], closes[-i-1]
            tr_vals.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = float(np.mean(tr_vals))
        atr_pct = atr / latest * 100
    else:
        atr_pct = 5.0

    trend_str = "up" if latest > ma5 > ma10 else ("down" if latest < ma5 < ma10 else "neutral")

    return {
        "latest": round(latest, 2),
        "ma5": round(ma5, 2),
        "ma10": round(ma10, 2),
        "pos_10d": round(pos, 1),
        "vol_ratio": round(vol_ratio, 2),
        "atr_pct": round(atr_pct, 2),
        "chg_1d": round(chg_1d, 1),
        "chg_3d": round(chg_3d, 1),
        "chg_5d": round(chg_5d, 1),
        "trend": trend_str,
        "high_10d": round(high_10d, 2),
        "low_10d": round(low_10d, 2),
    }


# ====== 聚合接口 ======

def compute_tech_factors(conn, ts_code: str, trade_date: str) -> dict:
    """计算单只股票全部7个技术因子

    Args:
        conn: DuckDB 连接 (read_only)
        ts_code: 标的代码, 如 '603986.SH'
        trade_date: 交易日期 'YYYY-MM-DD' 或 'YYYYMMDD'

    Returns:
        {
            "trend": float,        # ±3.5
            "momentum": float,     # ±4.0
            "position": float,     # ±1.0
            "volume": float,       # ±1.5
            "atr": float,          # ±1.5
            "distribution": float, # ±2.0
            "sr": float,           # ±1.5
            "indicators": {...},   # 原始指标快照
        }
    """
    df = _load_stock_data(conn, ts_code, trade_date, days=15)

    # 空结果
    if df is None or len(df) < 5:
        return {
            "trend": 0.0, "momentum": 0.0, "position": 0.0,
            "volume": 0.0, "atr": 0.0, "distribution": 0.0, "sr": 0.0,
            "indicators": {},
        }

    closes = df["close"].values.astype(float)
    vols = df["vol"].values.astype(float)
    highs = df["high"].values.astype(float)
    lows = df["low"].values.astype(float)
    chgs = df["pct_chg"].values.astype(float)

    ind = _extract_indicators(df)

    # 各因子独立计算
    trend = round(float(factor_trend(closes)), 4)
    momentum = round(float(factor_momentum(ind["chg_5d"])), 4)
    position = round(float(factor_position(float(closes[-1]),
                                           float(np.max(highs[-10:])) if len(highs) >= 10 else float(closes[-1]),
                                           float(np.min(lows[-10:])) if len(lows) >= 10 else float(closes[-1]))), 4)
    volume = round(float(factor_volume(ind["vol_ratio"], ind["chg_1d"])), 4)
    atr = round(float(factor_atr(ind["atr_pct"], ind["chg_5d"])), 4)
    distribution = round(float(factor_distribution(closes, vols)), 4)
    sr = round(float(factor_sr(float(closes[-1]),
                                float(np.max(highs[-10:])) if len(highs) >= 10 else float(closes[-1]),
                                float(np.min(lows[-10:])) if len(lows) >= 10 else float(closes[-1]))), 4)

    return {
        "trend": trend,
        "momentum": momentum,
        "position": position,
        "volume": volume,
        "atr": atr,
        "distribution": distribution,
        "sr": sr,
        "indicators": ind,
    }


def compute_tech_factors_batch(conn, ts_codes: list[str], trade_date: str) -> list[dict]:
    """批量计算技术因子 (逐只, 容错)

    Args:
        conn: DuckDB 连接 (read_only)
        ts_codes: 标的代码列表
        trade_date: 交易日期 'YYYY-MM-DD' 或 'YYYYMMDD'

    Returns:
        [{ts_code, ...7因子...}, ...]
    """
    results = []
    for code in ts_codes:
        try:
            factors = compute_tech_factors(conn, code, trade_date)
            factors["ts_code"] = code
            results.append(factors)
        except Exception as e:
            # 单只崩溃不影响其他
            results.append({
                "ts_code": code,
                "trend": 0.0, "momentum": 0.0, "position": 0.0,
                "volume": 0.0, "atr": 0.0, "distribution": 0.0, "sr": 0.0,
                "indicators": {},
                "error": str(e),
            })
    return results


# ====== CLI 自检 ======

if __name__ == "__main__":
    import duckdb
    from datetime import datetime

    db_path = _discover_duckdb()
    if not db_path:
        print("X 未找到 DuckDB 文件")
        exit(1)

    print(f"DuckDB: {db_path}\n")
    conn = duckdb.connect(db_path, read_only=True)

    # 获取最新交易日期
    max_date = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
    today = datetime.strptime(max_date, "%Y%m%d").strftime("%Y-%m-%d")
    print(f"最新数据日期: {today}\n")

    test_codes = ["603986.SH", "000001.SZ", "600519.SH"]

    print("=== 单只因子测试 ===")
    for code in test_codes:
        f = compute_tech_factors(conn, code, today)
        ind = f.pop("indicators", {})
        print(f"\n--- {code} (收盘={ind.get('latest','?')}, 5日={ind.get('chg_5d','?')}%) ---")
        for k, v in f.items():
            bar = "█" * int(abs(v) * 3) if abs(v) > 0.1 else ""
            sign = "+" if v > 0 else ""
            print(f"  {k:14s}: {sign}{v:+.4f}  {bar}")

    print("\n=== 批量因子测试 ===")
    batch = compute_tech_factors_batch(conn, test_codes, today)
    print(f"{'代码':<14s} {'趋势':>8s} {'动量':>8s} {'位置':>8s} {'量价':>8s} {'ATR':>8s} {'筹码':>8s} {'SR':>8s}")
    print("-" * 80)
    for r in batch:
        print(f"{r['ts_code']:<14s} "
              f"{r['trend']:+8.3f} {r['momentum']:+8.3f} {r['position']:+8.3f} "
              f"{r['volume']:+8.3f} {r['atr']:+8.3f} "
              f"{r['distribution']:+8.3f} {r['sr']:+8.3f}")

    conn.close()
    print("\n完成")
