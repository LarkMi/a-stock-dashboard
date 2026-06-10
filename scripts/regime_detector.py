#!/usr/bin/env python3
"""
regime_detector.py — 市场阶段检测 + 动态权重切换 + 行业相对强弱
基于 dual-analyst-stock-review skill 的自检结果优化
"""

import duckdb
from datetime import datetime

DB = r"C:\Users\LarkMi\quant_20260525\每日A股日线行情数据duckDB（日更）\daily_adj_19901219_20260607.duckdb"

# ===== 行业基准映射 =====
SECTOR_BENCHMARKS = {
    "煤炭":     ["600403.SH", "601666.SH"],
    "化工":     ["603938.SH", "600426.SH", "603078.SH", "600367.SH"],
    "风电/重工": ["002487.SZ", "000338.SZ"],
    "医药":     ["002940.SZ"],
    "科技/电子": ["603986.SH", "002384.SZ"],
    "消费/食品": ["002329.SZ", "002082.SZ"],
    "建材/基建": ["002785.SZ", "003001.SZ"],
    "军工":     ["600855.SH"],
    "家电":     ["600839.SH"],
    "通信":     ["600130.SH"],
}

def detect_regime(conn):
    """
    检测市场阶段: trending / mean_reverting / choppy
    方法: 比较5日均涨跌绝对值 vs 标准差。趋势市=方向一致且波动小
    """
    try:
        df = conn.execute("""
            SELECT trade_date, AVG(pct_chg) as avg_chg, 
                   STDDEV(pct_chg) as std_chg,
                   SUM(CASE WHEN pct_chg>0 THEN 1 ELSE 0 END)*100.0/COUNT(*) as up_pct
            FROM daily WHERE ts_code NOT LIKE '%ST%' AND ts_code NOT LIKE '%BJ%'
              AND trade_date >= '20260501'
            GROUP BY trade_date ORDER BY trade_date DESC LIMIT 10
        """).df()
        if len(df) < 5: return "choppy", "震荡"
        
        # 计算最近5天的平均涨跌方向一致性
        avg_abs_chg = df['avg_chg'].abs().mean()  # 市场平均涨跌绝对值
        avg_std = df['std_chg'].mean()  # 个股间标准差
        
        # 趋势强度 = 市场方向一致性 / 个股离散度
        direction_consistency = 1.0 if abs(df['avg_chg'].sum()) > 5 else abs(df['avg_chg'].sum()) / 5
        dispersion = avg_std / max(avg_abs_chg, 0.1)
        
        if direction_consistency > 0.6 and dispersion < 3:
            direction = "bull" if df['avg_chg'].sum() > 0 else "bear"
            label = "强趋势(牛)" if direction == "bull" else "强趋势(熊)"
            return direction, label
        elif dispersion > 5:
            return "choppy", "高波动震荡"
        else:
            direction = "slightly_bull" if df['avg_chg'].sum() > 0 else "slightly_bear"
            label = "弱趋势偏多" if direction == "slightly_bull" else "弱趋势偏空"
            return direction, label
    except Exception as e:
        return "unknown", f"检测失败:{e}"


def get_adaptive_weights(regime):
    """
    根据市场阶段动态调整因子权重。
    趋势市: 趋势+动量权重↑（强者恒强）
    震荡市: 位置+量比权重↑（均值回归）
    """
    base = {'trend': 2.5, 'pos': 1.0, 'vol': 1.0, 'momentum': 1.5}
    
    if regime in ("bull", "bear"):
        # 趋势市：趋势和动量更重要
        return {'trend': 3.0, 'pos': 0.5, 'vol': 0.5, 'momentum': 2.0}
    elif regime in ("slightly_bull", "slightly_bear"):
        # 弱趋势：保持默认
        return base
    elif regime == "choppy":
        # 震荡市：位置和量比更重要（反转交易）
        return {'trend': 1.5, 'pos': 2.0, 'vol': 1.5, 'momentum': 0.5}
    else:
        return base


def get_sector_relative_strength(conn, ts_code, days=5):
    """
    计算个股相对板块强度: 个股5日涨跌 - 板块平均5日涨跌
    正值=跑赢板块，负值=跑输板块
    """
    sector = None
    for sname, members in SECTOR_BENCHMARKS.items():
        if ts_code in members:
            sector = sname
            break
    if not sector:
        return 0.0, ""
    
    members = SECTOR_BENCHMARKS[sector]
    if len(members) < 2:
        return 0.0, ""
    
    # 逐个查询每个成员的5日涨跌
    pct_map = {}
    for m in members:
        try:
            r = conn.execute(f"""
                SELECT SUM(pct_chg) FROM (
                    SELECT pct_chg FROM daily 
                    WHERE ts_code='{m}' 
                    ORDER BY trade_date DESC LIMIT {days}
                )
            """).fetchone()
            if r and r[0] is not None:
                pct_map[m] = float(r[0])
        except:
            pass
    
    if ts_code not in pct_map or len(pct_map) < 2:
        return 0.0, ""
    
    stock_chg = pct_map[ts_code]
    others = [v for k, v in pct_map.items() if k != ts_code]
    sector_avg = sum(others) / len(others)
    
    rs = round(stock_chg - sector_avg, 1)
    label = f"vs{sector}{rs:+.1f}%" if abs(rs) > 1 else ""
    return rs, label


def get_atr_expected_range(conn, ts_code, days=14):
    """ATR预期日波动范围"""
    try:
        df = conn.execute(f"""
            SELECT high, low, close, LAG(close) OVER(ORDER BY trade_date) as prev_close
            FROM daily WHERE ts_code='{ts_code}'
            ORDER BY trade_date DESC LIMIT {days+1}
        """).df()
        if len(df) < days: return 0.0, ""
        
        tr_list = []
        for i in range(min(days, len(df)-1)):
            h = float(df.iloc[i]['high'])
            l = float(df.iloc[i]['low'])
            pc = float(df.iloc[i+1]['close'])
            tr = max(h-l, abs(h-pc), abs(l-pc))
            tr_list.append(tr)
        
        atr = sum(tr_list) / len(tr_list)
        latest_close = float(df.iloc[0]['close'])
        atr_pct = round(atr / latest_close * 100, 1)
        return atr_pct, f"预期波动±{atr_pct}%"
    except:
        return 0.0, ""


def apply_regime_confidence_adjustment(conf, regime):
    """市场阶段对置信度的影响"""
    if regime in ("bull", "bear"):
        # 趋势市：方向预判置信度+5%
        return min(0.9, conf + 0.05)
    elif regime == "choppy":
        # 震荡市：方向预判难度大，降5%
        return max(0.25, conf - 0.05)
    return conf


def apply_sector_rs_adjustment(pred_direction, side, rs):
    """行业相对强弱修正"""
    if abs(rs) < 2:
        return pred_direction, ""
    
    # 跑赢板块5%+ → 独立强势，增强信号
    if rs > 5 and side == "long":
        return pred_direction, f"跑赢板块{rs:+.1f}%→增强"
    elif rs < -5 and side == "short":
        return pred_direction, f"跑输板块{rs:+.1f}%→增强"
    # 与板块背离 → 可能是独立行情或补涨/补跌
    elif rs > 5 and side == "short":
        return pred_direction, f"逆板块上涨→谨慎"
    elif rs < -5 and side == "long":
        return pred_direction, f"逆板块下跌→谨慎"
    return pred_direction, ""


if __name__ == "__main__":
    conn = duckdb.connect(DB, read_only=True)
    
    print("=== 市场阶段检测 ===")
    regime, label = detect_regime(conn)
    weights = get_adaptive_weights(regime)
    print(f"  阶段: {label} ({regime})")
    print(f"  动态权重: {weights}")
    
    print("\n=== 行业相对强弱 ===")
    for stock in ["603986.SH", "600403.SH", "600426.SH", "002487.SZ"]:
        rs, label = get_sector_relative_strength(conn, stock)
        atr, atr_label = get_atr_expected_range(conn, stock)
        print(f"  {stock}: RS={rs:+.1f}% {label} | ATR={atr}%")
    
    print("\n=== 各阶段权重对比 ===")
    for r in ["bull", "bear", "slightly_bull", "slightly_bear", "choppy"]:
        w = get_adaptive_weights(r)
        print(f"  {r:15s}: trend={w['trend']} pos={w['pos']} vol={w['vol']} mom={w['momentum']}")
    
    conn.close()
