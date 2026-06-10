#!/usr/bin/env python3
"""
tech_factors.py — 技术面因子独立模块

7个公开因子函数，每因子值域各不同（按任务规格）
接口: compute_tech_factors(conn, ts_code, cutoff_date=None) -> dict

因子列表:
  trend        (均线趋势,   ±3.5)   MA5/MA10多空排列×偏离度
  momentum     (5日动量,    ±4.0)   SUM(pct_chg[-5:]) × 0.2, 剔除涨跌停
  position     (10日位置,   ±1.0)   0-100 映射到 -1~+1
  volume       (量价配合,   ±1.5)   放量(vol_ratio>1.3) × 方向
  atr          (ATR波动率,  ±1.0)   14日ATR% → 高波动衰减, 中波动可交易
  distribution (筹码分布,   ±0.8)   60日区间分位 + VWAP偏离
  sr           (支撑阻力,   ±0.8)   20/60日高低点 + 成交量密集区

边界处理:
  - 新股(<5日数据): 各因子返回0(中性)
  - 停牌/退市(close≤0): 返回None跳过
  - 涨跌停(±9.9%): 动量计算中剔除

依赖: duckdb, pandas, numpy
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
        files = sorted(glob.glob(os.path.join(_DB_DIR, 'daily_adj_*.duckdb')))
        files = [f for f in files if os.path.getsize(f) > 100 * 1024 * 1024]
        if files:
            return files[-1]
    except Exception:
        pass
    fallback = os.path.join(_DB_DIR, 'daily_adj_19901219_20260610.duckdb')
    if os.path.exists(fallback):
        return fallback
    return None


# ====== 数据加载 ======

def _load_stock_data(conn, ts_code, cutoff_date=None, days=65):
    """加载个股OHLCV数据（按trade_date升序）
    
    Args:
        conn: DuckDB连接
        ts_code: 标的代码
        cutoff_date: 可选截止日 'YYYY-MM-DD'
        days: 加载天数（默认65，覆盖60日分布+14日ATR）
    
    Returns:
        DataFrame(升序) 或 None(数据不足)
    """
    if cutoff_date:
        cutoff = cutoff_date.replace('-', '')
        where = f"AND trade_date <= '{cutoff}'"
    else:
        where = ""

    try:
        df = conn.execute(f"""
            SELECT trade_date, open, high, low, close, vol, pct_chg
            FROM daily_adj
            WHERE ts_code = '{ts_code}' {where}
            ORDER BY trade_date DESC
            LIMIT {days}
        """).df()
        if df.empty or len(df) < 3:
            return None
        return df.sort_values('trade_date', ascending=True).reset_index(drop=True)
    except Exception:
        return None


def _is_limit_hit(pct_chg_val):
    """判断涨跌停（±9.9% 阈值）"""
    return abs(pct_chg_val) >= 9.9


def _check_invalid(latest):
    """检测无效标的（停牌/退市：close≤0 或 NaN）"""
    return latest <= 0 or np.isnan(latest)


# ====== 因子1: 均线趋势 (trend, ±3.5) ======

def factor_trend(closes, latest):
    """均线趋势因子：MA5/MA10多空排列 × 偏离度。
    
    值域: -3.5 ~ +3.5
    逻辑: price > ma5 > ma10 = 多头, price < ma5 < ma10 = 空头
          偏离度 = (close - MA5) / MA5 * 100, 放大信号
    新股(<5日): 返回0
    """
    n = len(closes)
    if n < 5 or _check_invalid(latest):
        return 0.0

    ma5 = float(np.mean(closes[-5:]))
    ma10 = float(np.mean(closes[-10:])) if n >= 10 else float(np.mean(closes))
    if ma5 <= 0:
        return 0.0

    # 距MA5百分比偏离
    ma5_dist = (latest - ma5) / ma5 * 100

    if latest > ma5 > ma10:
        # 完全多头：基础+1.0，偏离放大
        score = 1.0 + ma5_dist * 0.35
    elif latest < ma5 < ma10:
        # 完全空头：基础-1.0
        score = -1.0 + ma5_dist * 0.35  # ma5_dist 为负
    elif latest > ma5:
        # 弱多头：MA5上方但MA5未超MA10
        score = 0.3 + ma5_dist * 0.25
    elif latest < ma5:
        # 弱空头
        score = -0.3 + ma5_dist * 0.25
    else:
        score = 0.0

    return float(np.clip(score, -3.5, 3.5))


# ====== 因子2: 5日动量 (momentum, ±4.0) ======

def factor_momentum(chgs):
    """5日动量因子：累计涨跌幅 × 0.2, 剔除涨跌停日。
    
    值域: -4.0 ~ +4.0 (对应±20%涨跌幅)
    涨跌停日(±9.9%)不参与计算, 避免失真
    数据不足: 返回0
    """
    n = len(chgs)
    if n < 3:
        return 0.0

    # 取最近5日, 剔除涨跌停
    recent = chgs[-5:] if n >= 5 else chgs
    valid = [c for c in recent if not _is_limit_hit(c)]
    if not valid:
        return 0.0

    chg_5d = float(sum(valid))
    score = chg_5d * 0.2
    return float(np.clip(score, -4.0, 4.0))


# ====== 因子3: 10日位置 (position, ±1.0) ======

def factor_position(highs, lows, latest):
    """10日位置因子：当前价在10日高-低区间中的分位映射。
    
    值域: -1.0 ~ +1.0
    逻辑: pos_pct = (close - low_10d) / (high_10d - low_10d) * 100
          score = (pos_pct - 50) / 50  → 中位=0, 顶=+1, 底=-1
    """
    n = len(highs)
    if n < 3 or _check_invalid(latest):
        return 0.0

    h10 = float(np.max(highs[-10:])) if n >= 10 else float(np.max(highs))
    l10 = float(np.min(lows[-10:])) if n >= 10 else float(np.min(lows))
    if h10 <= l10:
        return 0.0

    pos_pct = (latest - l10) / (h10 - l10) * 100
    score = (pos_pct - 50) / 50
    return float(np.clip(score, -1.0, 1.0))


# ====== 因子4: 量价配合 (volume, ±1.5) ======

def factor_volume(vols, chgs):
    """量价配合因子：放量确认方向。
    
    值域: -1.5 ~ +1.5
    逻辑: vol_ratio > 1.3 时, score = chg_1d * 0.15
          缩量或正常量 → 0
    涨跌停日: 返回0(无量价意义)
    """
    n = len(vols)
    if n < 6:
        return 0.0

    chg_1d = float(chgs[-1])
    # 涨跌停日无量价配合意义
    if _is_limit_hit(chg_1d):
        return 0.0

    # 量比 = 今日量 / 前5日均量
    avg_vol = float(np.mean(vols[-6:-1]))
    if avg_vol <= 0:
        return 0.0
    vol_ratio = float(vols[-1]) / avg_vol

    if vol_ratio > 1.3:
        score = chg_1d * 0.15
    else:
        score = 0.0

    return float(np.clip(score, -1.5, 1.5))


# ====== 因子5: ATR波动率 (atr, ±1.0) ======

def factor_atr(highs, lows, closes, latest):
    """ATR波动率因子：14日真实波幅%。
    
    值域: -1.0 ~ +1.0
    逻辑:
      - ATR > 5%: 高波动风险, score ≈ -1.0 (衰减)
      - ATR 2-4%: 适中可交易, score ≈ +0.3~+0.8 (正偏)
      - ATR < 2%: 低波动横盘, score ≈ 0~+0.3
      - 趋势方向调制: 价在MA5上方则正偏,下方则负偏
    """
    n = len(closes)
    if n < 5 or _check_invalid(latest):
        return 0.0

    # 14日 True Range 均值
    n_tr = min(n - 1, 14)
    tr_sum = 0.0
    for i in range(1, n_tr + 1):
        h = float(highs[-i])
        l = float(lows[-i])
        pc = float(closes[-i - 1])
        tr_sum += max(h - l, abs(h - pc), abs(l - pc))

    atr_val = tr_sum / n_tr if n_tr > 0 else latest * 0.03
    atr_pct = atr_val / latest * 100

    # 方向调制：价格 vs MA5
    ma5 = float(np.mean(closes[-5:]))
    direction = 1.0 if latest > ma5 else (-1.0 if latest < ma5 else 0.0)

    # 波动率→信号映射
    if atr_pct > 5:
        # 高波动: 风险衰减, 方向放大风险
        intensity = max(0.3, 1.0 - (atr_pct - 5) * 0.15)
        score = -direction * intensity
    elif atr_pct >= 2:
        # 适中波动(2-4%): 可交易区间, 正值
        intensity = min(0.8, (atr_pct - 1.5) * 0.3)
        score = direction * abs(intensity)
    else:
        # 低波动(<2%): 横盘弱信号
        score = direction * 0.2

    return float(np.clip(score, -1.0, 1.0))


# ====== 因子6: 筹码分布 (distribution, ±0.8) ======

def factor_distribution(closes, vols, latest):
    """筹码分布因子：60日区间分位 + VWAP偏离。
    
    值域: -0.8 ~ +0.8
    逻辑:
      - position_60d: 当前价在60日高-低区间中的分位(0-100)
      - vwap_dev: 近20日成交量加权均价与现价的偏离%
      - 价在VWAP上方+中高分位 → 筹码锁定好, 支撑强 → 正偏
      - 价在VWAP下方+低分位 → 套牢盘压力 → 负偏
    """
    n = len(closes)
    if n < 10 or _check_invalid(latest):
        return 0.0

    # 60日(或全部)区间分位
    lookback = min(n, 60)
    c_window = closes[-lookback:].astype(float)
    h_max = float(np.max(c_window))
    l_min = float(np.min(c_window))
    if h_max <= l_min:
        return 0.0
    pos_60 = (latest - l_min) / (h_max - l_min) * 100  # 0-100

    # 20日VWAP
    vw_n = min(n, 20)
    c_vw = closes[-vw_n:].astype(float)
    v_vw = vols[-vw_n:].astype(float)
    total_vol = float(np.sum(v_vw))
    if total_vol <= 0:
        return 0.0
    vwap = float(np.sum(c_vw * v_vw) / total_vol)
    if vwap <= 0:
        return 0.0
    vwap_dev = (latest - vwap) / vwap * 100  # 偏离%

    # 综合评分: VWAP偏离主信号 + 区间分位辅助
    score = vwap_dev * 0.06  # VWAP偏差权重
    # 高位区 + 价在VWAP上 → 加正偏
    if pos_60 > 70 and vwap_dev > 0:
        score += 0.15
    elif pos_60 > 70 and vwap_dev < 0:
        score -= 0.1
    # 低位区 + 价在VWAP下 → 加负偏(套牢)
    elif pos_60 < 30 and vwap_dev < 0:
        score -= 0.15
    elif pos_60 < 30 and vwap_dev > 0:
        score += 0.1  # 低位反弹潜力

    return float(np.clip(score, -0.8, 0.8))


# ====== 因子7: 支撑阻力位 (sr, ±0.8) ======

def factor_sr(highs, lows, closes, vols, latest):
    """支撑阻力因子：识别20/60日关键价位。
    
    值域: -0.8 ~ +0.8
    逻辑:
      - 支撑: 20日最低 + 60日最低 中的显著低点
      - 阻力: 20日最高 + 60日最高 中的显著高点
      - 成交量密集区: 近20日VWAP作为动态支撑/阻力
      - 距支撑<3% → 做多偏向(反弹预期)
      - 距阻力<3% → 做空偏向(回调预期)
    """
    n = len(highs)
    if n < 5 or _check_invalid(latest):
        return 0.0

    # 20日高低
    n20 = min(n, 20)
    h20 = float(np.max(highs[-n20:]))
    l20 = float(np.min(lows[-n20:]))

    # 60日高低
    n60 = min(n, 60)
    h60 = float(np.max(highs[-n60:]))
    l60 = float(np.min(lows[-n60:]))

    if h60 <= l60:
        return 0.0

    # 成交量密集区: 20日VWAP
    vw_n = min(n, 20)
    c_vw = closes[-vw_n:].astype(float)
    v_vw = vols[-vw_n:].astype(float)
    total_vol = float(np.sum(v_vw))
    if total_vol > 0:
        vwap = float(np.sum(c_vw * v_vw) / total_vol)
    else:
        vwap = latest

    # 计算距各关键位的距离(%)
    dist_to_high20 = (h20 - latest) / latest * 100
    dist_to_low20 = (latest - l20) / latest * 100
    dist_to_high60 = (h60 - latest) / latest * 100
    dist_to_low60 = (latest - l60) / latest * 100
    dist_to_vwap = (latest - vwap) / latest * 100  # + = 在VWAP上方

    # 找到最近的关键位
    dist_to_support = min(dist_to_low20, dist_to_low60)
    dist_to_resistance = min(dist_to_high20, dist_to_high60)

    # 评分逻辑: 距支撑近 > 距阻力近
    if dist_to_support < 3 and dist_to_resistance > 5:
        # 紧贴支撑, 反弹概率高
        score = 0.8 - dist_to_support * 0.2
    elif dist_to_resistance < 3 and dist_to_support > 5:
        # 紧贴阻力, 回调概率高
        score = -0.8 + dist_to_resistance * 0.2
    else:
        # 中间位置: 距支撑相对距离决定方向
        # 距支撑越近越偏多
        rel = dist_to_resistance - dist_to_support  # + = 更近支撑
        score = rel * 0.05
        # VWAP 微调: 在VWAP上方加正偏
        if dist_to_vwap > 2:
            score += 0.1
        elif dist_to_vwap < -2:
            score -= 0.1

    return float(np.clip(score, -0.8, 0.8))


# ====== 主接口 ======

def compute_tech_factors(conn, ts_code, cutoff_date=None):
    """计算单只标的的7个技术面因子。
    
    Args:
        conn: DuckDB连接对象
        ts_code: 标的代码 (如 '603986.SH')
        cutoff_date: 可选回测截止日 'YYYY-MM-DD'
    
    Returns:
        dict: {
            'trend': 均线趋势 (-3.5~+3.5),
            'momentum': 5日动量 (-4.0~+4.0),
            'position': 10日位置 (-1.0~+1.0),
            'volume': 量价配合 (-1.5~+1.5),
            'atr': ATR波动 (-1.0~+1.0),
            'distribution': 筹码分布 (-0.8~+0.8),
            'sr': 支撑阻力 (-0.8~+0.8),
            'indicators': {原始指标字典},
        }
        若数据不足(停牌/退市), 所有factor=None
    """
    df = _load_stock_data(conn, ts_code, cutoff_date)
    if df is None:
        return _empty_result()

    closes = df['close'].values.astype(float)
    vols = df['vol'].values.astype(float)
    highs = df['high'].values.astype(float)
    lows = df['low'].values.astype(float)
    chgs = df['pct_chg'].values.astype(float)
    latest = float(closes[-1])

    if _check_invalid(latest):
        return _empty_result()

    # 新股检测: <5日数据 → 全部返回0(中性)
    is_new_stock = len(closes) < 5

    if is_new_stock:
        trend = 0.0
        momentum = 0.0
        position = 0.0
        volume = 0.0
        atr = 0.0
        distribution = 0.0
        sr = 0.0
    else:
        trend = factor_trend(closes, latest)
        momentum = factor_momentum(chgs)
        position = factor_position(highs, lows, latest)
        volume = factor_volume(vols, chgs)
        atr = factor_atr(highs, lows, closes, latest)
        distribution = factor_distribution(closes, vols, latest)
        sr = factor_sr(highs, lows, closes, vols, latest)

    # 原始指标
    n = len(closes)
    ma5 = float(np.mean(closes[-5:])) if n >= 5 else latest
    ma10 = float(np.mean(closes[-10:])) if n >= 10 else latest
    h10 = float(np.max(highs[-10:])) if n >= 10 else float(np.max(highs))
    l10 = float(np.min(lows[-10:])) if n >= 10 else float(np.min(lows))

    chg_1d = float(chgs[-1])
    chg_5d = float(np.sum(chgs[-5:])) if n >= 5 else float(np.sum(chgs))
    chg_3d = float(np.sum(chgs[-3:])) if n >= 3 else 0.0
    is_limit = _is_limit_hit(chg_1d)

    # 量比
    if n >= 6:
        avg_vol = float(np.mean(vols[-6:-1]))
        vol_ratio = float(vols[-1]) / avg_vol if avg_vol > 0 else 1.0
    else:
        vol_ratio = 1.0

    # ATR百分比
    n_tr = min(n - 1, 14)
    tr_sum = 0.0
    for i in range(1, n_tr + 1):
        tr_sum += max(float(highs[-i]) - float(lows[-i]),
                      abs(float(highs[-i]) - float(closes[-i - 1])),
                      abs(float(lows[-i]) - float(closes[-i - 1])))
    atr_val = tr_sum / n_tr if n_tr > 0 else latest * 0.03
    atr_pct = atr_val / latest * 100

    # 趋势方向 (兼容旧版)
    trend_dir = "up" if latest > ma5 > ma10 else ("down" if latest < ma5 < ma10 else "neutral")

    indicators = {
        'latest': round(latest, 2),
        'ma5': round(ma5, 2),
        'ma10': round(ma10, 2),
        'high_10d': round(h10, 2),
        'low_10d': round(l10, 2),
        'chg_1d': round(chg_1d, 1),
        'chg_3d': round(chg_3d, 1),
        'chg_5d': round(chg_5d, 1),
        'vol_ratio': round(vol_ratio, 2),
        'atr_pct': round(atr_pct, 2),
        'trend': trend_dir,
        'pos_10d': round((latest - l10) / (h10 - l10) * 100, 1) if h10 > l10 else 50.0,
        'is_limit': is_limit,
        'is_new': is_new_stock,
    }

    return {
        'trend': round(trend, 2),
        'momentum': round(momentum, 2),
        'position': round(position, 2),
        'volume': round(volume, 2),
        'atr': round(atr, 2),
        'distribution': round(distribution, 2),
        'sr': round(sr, 2),
        'indicators': indicators,
    }


def _empty_result():
    """停牌/退市 → 返回None"""
    return {
        'trend': None, 'momentum': None, 'position': None,
        'volume': None, 'atr': None, 'distribution': None, 'sr': None,
        'indicators': {},
    }


# ====== 批量接口 ======

def compute_tech_factors_batch(conn, ts_codes, cutoff_date=None):
    """批量计算多只标的的技术面因子。
    
    Args:
        conn: DuckDB连接
        ts_codes: 标的代码列表
        cutoff_date: 可选截止日
    
    Returns:
        dict: {ts_code: {...因子dict...}}
    """
    return {code: compute_tech_factors(conn, code, cutoff_date) for code in ts_codes}


# ====== 独立测试入口 ======

if __name__ == "__main__":
    import duckdb

    db_path = _discover_duckdb()
    if not db_path:
        print("X 未找到 DuckDB 文件")
        exit(1)

    print(f"DuckDB: {db_path}")
    print(f"文件大小: {os.path.getsize(db_path) / 1024 / 1024:.0f} MB\n")

    # 测试标的
    test_codes = ["603986.SH", "000001.SZ", "600519.SH", "000002.SZ"]
    conn = duckdb.connect(db_path, read_only=True)

    for code in test_codes:
        print(f"--- {code} ---")
        result = compute_tech_factors(conn, code)
        if result['trend'] is None:
            print("  [!] 停牌/退市/数据不足\n")
            continue

        ind = result['indicators']
        flag = ""
        if ind.get('is_limit'):
            flag += " [涨跌停]"
        if ind.get('is_new'):
            flag += " [新股]"

        print(f"  现价: {ind.get('latest')}  MA5: {ind.get('ma5')}  MA10: {ind.get('ma10')}{flag}")
        print(f"  趋势(trend):        {result['trend']:+.2f}  (range ±3.5)")
        print(f"  动量(momentum):     {result['momentum']:+.2f}  (range ±4.0)")
        print(f"  位置(position):     {result['position']:+.2f}  (range ±1.0)")
        print(f"  量价(volume):       {result['volume']:+.2f}  (range ±1.5)")
        print(f"  ATR(atr):           {result['atr']:+.2f}  (range ±1.0)")
        print(f"  筹码(distribution): {result['distribution']:+.2f}  (range ±0.8)")
        print(f"  支撑阻力(sr):       {result['sr']:+.2f}  (range ±0.8)")
        print(f"  5日涨跌: {ind.get('chg_5d')}%  量比: {ind.get('vol_ratio')}  ATR%: {ind.get('atr_pct')}")
        print()

    conn.close()
    print("测试完成")
