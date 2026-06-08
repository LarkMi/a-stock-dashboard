#!/usr/bin/env python3
"""
market_watcher.py — A股20只标的实时监控 + 预测自进化系统
支持：盘前/盘中/盘后多阶段分析，自动跟踪预测准确率，每日轮换弱标的
"""

import duckdb
import sqlite3
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 导入事件库
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from news_event_db import init_event_db  # 回测+analyze_single_stock用
from regime_detector import detect_regime, get_adaptive_weights, get_sector_relative_strength, get_atr_expected_range, apply_regime_confidence_adjustment

# ====== 配置 ======
# 自动发现最新 DuckDB 文件（跟随 update_daily_db.py 的版本命名）
import glob
_DB_DIR = r"C:\Users\LarkMi\quant_20260525\每日A股日线行情数据duckDB（日更）"
_db_files = sorted(glob.glob(os.path.join(_DB_DIR, 'daily_adj_*.duckdb')))
_db_files = [f for f in _db_files if os.path.getsize(f) > 100*1024*1024]
DUCKDB_PATH = _db_files[-1] if _db_files else r"C:\Users\LarkMi\quant_20260525\每日A股日线行情数据duckDB（日更）\daily_adj_19901219_20260607.duckdb"
STATE_DB = r"C:\Users\LarkMi\AppData\Local\hermes\data\market_watcher_state.db"
TOP5_LONG = [
    {"code": "002384.SZ", "name": "东山精密", "reason": "A50纳入+FPC+汽车电子", "type": "stock"},
    {"code": "000338.SZ", "name": "潍柴动力", "reason": "A50纳入+重卡龙头+超跌", "type": "stock"},
    {"code": "600367.SH", "name": "红星发展", "reason": "新材料+锰钡涨价", "type": "stock"},
    {"code": "600403.SH", "name": "大有能源", "reason": "煤炭能源+涨停突破", "type": "stock"},
    {"code": "603078.SH", "name": "江化微",   "reason": "电子化学品+涨停", "type": "stock"},
]
TOP5_SHORT = [
    {"code": "600130.SH", "name": "波导股份", "reason": "连续破位"},
    {"code": "002940.SZ", "name": "昂利康",   "reason": "医药抛售"},
    {"code": "002329.SZ", "name": "皇氏集团", "reason": "乳业崩溃"},
    {"code": "003001.SZ", "name": "中岩大地", "reason": "基建弱势"},
    {"code": "600855.SH", "name": "航天长峰", "reason": "军工退潮"},
]


def get_hour_type():
    """判断当前时段类型: pre/ trading/ post"""
    now = datetime.now()
    h, m = now.hour, now.minute
    if h < 9 or (h == 9 and m < 25):
        return "pre", "盘前分析"
    elif (h == 9 and m >= 25) or h == 10 or (h == 11 and m <= 30):
        return "trading", f"盘中分析 ({now.strftime('%H:%M')})"
    elif h == 13 or h == 14 or (h == 15 and m == 0):
        return "trading", f"盘中分析 ({now.strftime('%H:%M')})"
    else:
        return "post", "盘后总结"


# ====== 新增：结构性优化因子 ======

def get_market_breadth(conn_duck):
    """市场宽度：全市场上涨比例。>60%偏牛，<40%偏熊"""
    try:
        r = conn_duck.execute("""
            SELECT 
                SUM(CASE WHEN pct_chg > 0 THEN 1 ELSE 0 END) as up_count,
                COUNT(*) as total
            FROM daily WHERE trade_date = (SELECT MAX(trade_date) FROM daily)
              AND ts_code NOT LIKE '%ST%' AND ts_code NOT LIKE '%BJ%'
        """).fetchone()
        if r and r[1] > 100:
            return round(r[0] / r[1], 3)
    except:
        pass
    return 0.5  # 默认中性


def get_gap_signal(conn_duck, ts_code):
    """缺口分析：昨日收盘 vs 今日开盘的跳空幅度。大缺口倾向于回补"""
    try:
        df = conn_duck.execute(f"""
            SELECT open, close, LAG(close) OVER (ORDER BY trade_date) as prev_close
            FROM daily WHERE ts_code='{ts_code}'
            ORDER BY trade_date DESC LIMIT 2
        """).df()
        if len(df) >= 2:
            today_open, today_close, yesterday_close = df.iloc[0]
            if yesterday_close and yesterday_close > 0:
                gap_pct = (today_open - yesterday_close) / yesterday_close * 100
                # 缺口>2%考虑回补
                if gap_pct > 2:
                    return "gap_up", round(gap_pct, 1)  # 跳空上涨→短期偏空
                elif gap_pct < -2:
                    return "gap_down", round(gap_pct, 1)  # 跳空下跌→短期偏多
    except:
        pass
    return None, 0


def get_consecutive_signal(conn_state, ts_code):
    """连续信号：检查上一轮预测是否同方向，同向=信号强化"""
    try:
        c = conn_state.cursor()
        c.execute("""SELECT direction FROM predictions 
                     WHERE ts_code=? ORDER BY id DESC LIMIT 1""", (ts_code,))
        row = c.fetchone()
        if row:
            return row[0]
    except:
        pass
    return None


def init_state_db():
    conn = sqlite3.connect(STATE_DB)
    c = conn.cursor()
    # 预测记录表
    c.execute('''CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT, stock_name TEXT, direction TEXT,
        pred_time TEXT, target_time TEXT, confidence REAL,
        pred_note TEXT, actual_close REAL, actual_time TEXT,
        correct INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # 标的表现跟踪
    c.execute('''CREATE TABLE IF NOT EXISTS stock_performance (
        ts_code TEXT PRIMARY KEY, stock_name TEXT,
        total_preds INTEGER DEFAULT 0, correct_preds INTEGER DEFAULT 0,
        accuracy REAL DEFAULT 0, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # 系统进化记录
    c.execute('''CREATE TABLE IF NOT EXISTS evolution_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT, description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # 当前标的池
    c.execute('''CREATE TABLE IF NOT EXISTS current_roster (
        ts_code TEXT PRIMARY KEY, stock_name TEXT, side TEXT, reason TEXT,
        active INTEGER DEFAULT 1, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # 初始化标的池（如果为空）
    c.execute("SELECT COUNT(*) FROM current_roster")
    if c.fetchone()[0] == 0:
        for s in TOP5_LONG:
            c.execute("INSERT OR IGNORE INTO current_roster VALUES (?,?,?,?,1,CURRENT_TIMESTAMP)",
                      (s['code'], s['name'], 'long', s['reason']))
        for s in TOP5_SHORT:
            c.execute("INSERT OR IGNORE INTO current_roster VALUES (?,?,?,?,1,CURRENT_TIMESTAMP)",
                      (s['code'], s['name'], 'short', s['reason']))
        # 自定义标的也加入roster（side=locked，会被分开显示）
        LOCKED = _load_custom_codes()
        locked_list = get_locked_stocks()
        for s in locked_list:
            if s['code'] in LOCKED:
                c.execute("INSERT OR IGNORE INTO current_roster VALUES (?,?,?,?,1,CURRENT_TIMESTAMP)",
                          (s['code'], s['name'], 'locked', s.get('reason','')))
    conn.commit()
    return conn


def load_price_data(conn_duck, ts_code, days=10):
    """加载个股价格数据"""
    try:
        df = conn_duck.execute(f'''
            SELECT trade_date, open, high, low, close, vol, pct_chg
            FROM daily WHERE ts_code='{ts_code}'
            ORDER BY trade_date DESC LIMIT {days}
        ''').df()
        return df.sort_values('trade_date', ascending=True) if len(df) > 0 else None
    except Exception as e:
        return None


def calc_indicators(df):
    """计算技术指标"""
    if df is None or len(df) < 5:
        return {}
    closes = df['close'].values
    vols = df['vol'].values
    highs = df['high'].values
    lows = df['low'].values
    latest = closes[-1]

    # 均线
    ma5 = closes[-5:].mean() if len(closes) >= 5 else latest
    ma10 = closes[-10:].mean() if len(closes) >= 10 else latest

    # 位置
    high_10d = highs[-10:].max() if len(highs) >= 10 else latest
    low_10d = lows[-10:].min() if len(lows) >= 10 else latest
    pos_10d = (latest - low_10d) / (high_10d - low_10d) * 100 if high_10d > low_10d else 50

    # 量比
    vol_ratio = vols[-1] / vols[-6:-1].mean() if len(vols) >= 6 else 1.0

    # ATR (14日)
    if len(df) >= 14:
        tr = []
        for i in range(1, min(15, len(df))):
            h, l, pc = highs[-i], lows[-i], closes[-i-1]
            tr.append(max(h-l, abs(h-pc), abs(l-pc)))
        atr = sum(tr) / len(tr)
        atr_pct = atr / latest * 100
    else:
        atr = latest * 0.05
        atr_pct = 5.0

    # 近期涨跌幅
    chg_1d = float(df['pct_chg'].iloc[-1]) if len(df) >= 1 else 0
    chg_5d = df['pct_chg'].tail(5).sum() if len(df) >= 5 else 0
    chg_3d = df['pct_chg'].tail(3).sum() if len(df) >= 3 else 0

    # 趋势判断
    trend = "up" if latest > ma5 > ma10 else ("down" if latest < ma5 < ma10 else "neutral")

    return {
        "latest": round(latest, 2), "ma5": round(ma5, 2), "ma10": round(ma10, 2),
        "pos_10d": round(pos_10d, 1), "vol_ratio": round(vol_ratio, 2),
        "atr_pct": round(atr_pct, 2), "chg_1d": round(chg_1d, 1),
        "chg_5d": round(chg_5d, 1), "chg_3d": round(chg_3d, 1), "trend": trend,
        "high_10d": round(high_10d, 2), "low_10d": round(low_10d, 2),
    }


def generate_trend_prediction(indicators, side, stock_name):
    """短期趋势预测（1-3日方向）"""
    if not indicators:
        return {"direction": "neutral", "confidence": 0.3, "note": "数据不足"}
    score = 0
    reasons = []
    # 均线趋势（权重2.5）
    if indicators["trend"] == "up":
        score += (2.5 if side == "long" else -2.5)
        reasons.append("均线多头")
    elif indicators["trend"] == "down":
        score += (-2.5 if side == "long" else 2.5)
        reasons.append("均线空头")
    # 位置（权重1.0）
    pos = indicators["pos_10d"]
    if pos > 80:
        score += (-1.0 if side == "long" else 1.0); reasons.append(f"高位超买({pos}%)")
    elif pos < 20:
        score += (1.0 if side == "long" else -1.0); reasons.append(f"低位超卖({pos}%)")
    # 量比（权重1.0）
    vr, chg_3d = indicators["vol_ratio"], indicators["chg_3d"]
    if vr > 1.5 and chg_3d > 0:
        score += (1.0 if side == "long" else -1.0); reasons.append(f"放量上涨(vr={vr})")
    elif vr > 1.5 and chg_3d < 0:
        score += (-1.0 if side == "long" else 1.0); reasons.append(f"放量下跌(vr={vr})")
    # 5日动量（权重1.5）
    if indicators["chg_5d"] > 10:
        score += (1.5 if side == "long" else -1.5); reasons.append("5日强动量")
    elif indicators["chg_5d"] < -10:
        score += (-1.5 if side == "long" else 1.5); reasons.append("5日弱动量")
    # 转换
    if score > 2:      direction = "strong_up" if side == "long" else "strong_down"
    elif score > 0.5:  direction = "up" if side == "long" else "down"
    elif score < -2:   direction = "strong_down" if side == "long" else "strong_up"
    elif score < -0.5: direction = "down" if side == "long" else "up"
    else:              direction = "neutral"
    confidence = min(0.85, max(0.25, abs(score) / 6.0))
    note = "; ".join(reasons) if reasons else "无明显信号"
    return {"direction": direction, "confidence": round(confidence, 2), "note": note, "score": round(score, 1)}


def generate_spot_prediction(indicators, side, hour_type, conn_duck=None, ts_code=None):
    """时点预测：下一时段方向（如9:00→9:30开盘方向、10:30→11:30）"""
    if not indicators:
        return {"direction": "flat", "confidence": 0.3, "note": "数据不足"}
    reasons = []
    score = 0.0
    chg_1d = indicators.get("chg_1d", 0)
    chg_3d = indicators.get("chg_3d", 0)
    pos = indicators.get("pos_10d", 50)
    trend = indicators.get("trend", "neutral")
    
    if hour_type == "pre":
        # 盘前：预测开盘方向（依赖昨日收盘+缺口信号+动量惯性）
        if chg_1d > 2:      score += 1.0; reasons.append("昨日强势→惯性高开")
        elif chg_1d < -2:   score -= 1.0; reasons.append("昨日弱势→惯性低开")
        if trend == "up":   score += 0.5; reasons.append("均线多头→倾向高开")
        elif trend == "down": score -= 0.5; reasons.append("均线空头→倾向低开")
        if pos > 85:        score -= 1.0; reasons.append("超买区→开盘回落风险")
        elif pos < 15:      score += 1.0; reasons.append("超卖区→开盘反弹")
    elif hour_type in ("trading", "post"):
        # 盘中/盘后：预测下一时段/明日方向（动量惯性为主，结合位置）
        if chg_1d > 1:      score += 0.8; reasons.append("当日走强→惯性延续")
        elif chg_1d < -1:   score -= 0.8; reasons.append("当日走弱→惯性延续")
        if trend == "up":   score += 0.3; reasons.append("趋势向上")
        elif trend == "down": score -= 0.3; reasons.append("趋势向下")
        if pos > 80:        score -= 0.5; reasons.append("高位预警")
        elif pos < 20:      score += 0.5; reasons.append("低位反弹")
    
    if score > 1.0:      direction = "up"
    elif score > 0.3:    direction = "up"
    elif score < -1.0:   direction = "down"
    elif score < -0.3:   direction = "down"
    else:                direction = "flat"
    
    confidence = min(0.8, max(0.25, abs(score) / 3.0))
    note = "; ".join(reasons) if reasons else ("惯性平开" if hour_type == "pre" else "方向不明")
    
    return {"direction": direction, "confidence": round(confidence, 2), "note": note, "score": round(score, 1)}


def save_prediction(conn_state, ts_code, stock_name, direction, confidence, note, spot_dir=None, spot_conf=None):
    """保存预测到状态库（含趋势+时点双预测）"""
    c = conn_state.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute('''INSERT INTO predictions (ts_code, stock_name, direction, pred_time, 
                 confidence, pred_note) VALUES (?,?,?,?,?,?)''',
              (ts_code, stock_name, direction, now, confidence, note))
    pred_id = c.lastrowid
    # 时点预测存note字段追加
    if spot_dir:
        spot_tag = f"[时点:{spot_dir} conf={spot_conf:.0%}]" if spot_conf else f"[时点:{spot_dir}]"
        c.execute("UPDATE predictions SET pred_note = pred_note || ? WHERE id=?",
                  (spot_tag, pred_id))
    conn_state.commit()
    return pred_id


def fetch_realtime_price(ts_code):
    """从新浪财经获取实时价格（盘中验证用）。仅交易时段返回有效数据。
    返回: {price, prev_close, change_pct} 或 None"""
    # 非交易时段直接跳过——Sina周末/盘后返回假数据
    if not is_market_hours():
        return None
    import urllib.request, re
    try:
        if ts_code.endswith('.SH'):
            sina_code = 'sh' + ts_code.split('.')[0]
        else:
            sina_code = 'sz' + ts_code.split('.')[0]
        url = f'http://hq.sinajs.cn/list={sina_code}'
        req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn'})
        resp = urllib.request.urlopen(req, timeout=5)
        text = resp.read().decode('gbk')
        # 解析: var hq_str_sh603986="name,open,prev_close,price,high,low,..."
        match = re.search(r'"([^"]*)"', text)
        if not match:
            return None
        parts = match.group(1).split(',')
        if len(parts) < 4:
            return None
        name = parts[0]
        prev_close = float(parts[2]) if parts[2] else 0
        price = float(parts[3]) if parts[3] else 0
        if prev_close > 0 and price > 0:
            change_pct = (price - prev_close) / prev_close * 100
            return {'price': price, 'prev_close': prev_close, 'change_pct': round(change_pct, 2), 'name': name}
    except Exception:
        pass
    return None


def fetch_realtime_prices(codes):
    """批量从新浪获取实时价格（一次HTTP请求）。仅交易时段返回有效数据。
    codes: ts_code列表, 如 ['603986.SH','000001.SZ']
    返回: {ts_code: {price, prev_close, change_pct, name}} """
    import urllib.request, re
    result = {}
    if not codes:
        return result
    # 非交易时段直接跳过，防止周末/盘后假数据污染
    if not is_market_hours():
        return result
    try:
        sina_codes = []
        code_map = {}  # sina_code -> ts_code
        for c in codes:
            if c.endswith('.SH'):
                sc = 'sh' + c.split('.')[0]
            else:
                sc = 'sz' + c.split('.')[0]
            sina_codes.append(sc)
            code_map[sc] = c
        url = 'http://hq.sinajs.cn/list=' + ','.join(sina_codes)
        req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn'})
        resp = urllib.request.urlopen(req, timeout=8)
        text = resp.read().decode('gbk')
        # 逐条解析
        for line in text.strip().split('\n'):
            match = re.search(r'hq_str_(\w+)="([^"]*)"', line)
            if not match:
                continue
            sina_code, data = match.group(1), match.group(2)
            ts_code = code_map.get(sina_code)
            if not ts_code:
                continue
            parts = data.split(',')
            if len(parts) < 4:
                continue
            prev_close = float(parts[2]) if parts[2] else 0
            price = float(parts[3]) if parts[3] else 0
            if prev_close > 0 and price > 0:
                change_pct = (price - prev_close) / prev_close * 100
                result[ts_code] = {
                    'price': price, 'prev_close': prev_close,
                    'change_pct': round(change_pct, 2), 'name': parts[0]
                }
    except Exception:
        pass
    return result


def is_trading_day(conn_duck=None):
    """判断今天是否为交易日。优先查DuckDB trade_date，回退weekday判断。
    返回: (is_trading, today_str)"""
    from datetime import datetime
    today_str = datetime.now().strftime('%Y%m%d')
    if conn_duck:
        try:
            cnt = conn_duck.execute(
                f"SELECT COUNT(*) FROM daily WHERE trade_date='{today_str}'"
            ).fetchone()[0]
            if cnt > 0:
                return True, today_str
        except:
            pass
    # 回退：周一至周五视为交易日（无法覆盖节假日但阻止周末假数据）
    return datetime.now().weekday() < 5, today_str


def is_market_hours():
    """判断当前是否在交易时段内（9:25-15:05，含集合竞价）"""
    from datetime import datetime
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    return (h == 9 and m >= 25) or h == 10 or (h == 11 and m <= 30) or h == 13 or h == 14 or (h == 15 and m <= 5)


def update_actuals(conn_state, conn_duck):
    """用最新数据回填预测结果。时点≥1h验证，趋势≥24h验证。
    非交易日/Sina假数据防护：周末/节假日直接跳过验证。
    返回: {trend_updated: N, spot_updated: N}"""
    import re
    from datetime import datetime
    c = conn_state.cursor()

    # === 交易日检查：非交易日跳过所有验证 ===
    is_trade, today_str = is_trading_day(conn_duck)
    if not is_trade:
        return {"trend_updated": 0, "spot_updated": 0, "skipped": "非交易日"}
    
    # 确保列存在
    for col in ['spot_correct', 'trend_verified']:
        try: c.execute(f"ALTER TABLE predictions ADD COLUMN {col} INTEGER DEFAULT NULL")
        except sqlite3.OperationalError: pass
    for col, typ in [("spot_total", "INTEGER DEFAULT 0"), ("spot_correct", "INTEGER DEFAULT 0"),
                     ("spot_accuracy", "REAL DEFAULT 0")]:
        try: c.execute(f"ALTER TABLE stock_performance ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError: pass
    
    # === 时点验证：≥1小时的未验预测 ===
    c.execute('''SELECT id, ts_code, direction, pred_note, pred_time FROM predictions 
                 WHERE spot_correct IS NULL 
                 AND pred_time <= datetime('now','localtime','-1 hours')''')
    spot_unverified = c.fetchall()
    
    # === 趋势验证：≥24小时的未验预测 ===
    c.execute('''SELECT id, ts_code, direction, pred_note, pred_time FROM predictions 
                 WHERE correct IS NULL 
                 AND pred_time <= datetime('now','localtime','-24 hours')''')
    trend_unverified = c.fetchall()
    
    # 合并去重（同一个预测可能同时在两个列表里）
    all_unverified = {(r[0], r[1], r[2], r[3], r[4]) for r in spot_unverified + trend_unverified}
    
    spot_updated = 0
    trend_updated = 0
    
    for pid, ts_code, direction, note, pred_time in all_unverified:
        # 优先用 DB 日线；DB 滞后则用新浪实时价
        df = load_price_data(conn_duck, ts_code, days=2)
        use_realtime = False
        
        if df is not None and len(df) >= 2:
            last_db_date = str(df['trade_date'].iloc[-1])
            pred_date = pred_time[:10].replace('-', '')  # '2026-06-08 09:30' -> '20260608'
            if last_db_date < pred_date:
                use_realtime = True  # DB 无当日数据 → 用实时价
        else:
            use_realtime = True
        
        if use_realtime:
            rt = fetch_realtime_price(ts_code)
            if rt and rt['price'] > 0 and rt['prev_close'] > 0:
                curr_close = rt['price']
                prev_close = rt['prev_close']
            else:
                continue  # 实时价也取不到，下轮再试
        else:
            prev_close = df['close'].iloc[-2]
            curr_close = df['close'].iloc[-1]
        
        if prev_close <= 0 or curr_close <= 0:
            continue
        actual_change = (curr_close - prev_close) / prev_close * 100
        actual_up = actual_change > 0.5
        actual_down = actual_change < -0.5
        
        # 计算预测距今小时数
        try:
            pred_dt = datetime.strptime(pred_time, '%Y-%m-%d %H:%M')
            hours_ago = (datetime.now() - pred_dt).total_seconds() / 3600
        except:
            hours_ago = 99  # 解析失败则按可验证处理
        
        new_correct = None
        new_spot = None
        
        # === 趋势验证：仅≥24h才判 ===
        if hours_ago >= 24 and direction != "neutral":
            pred_up = "up" in direction
            pred_down = "down" in direction
            new_correct = 1 if ((pred_up and actual_up) or (pred_down and actual_down)) else 0
            trend_updated += 1
        
        # === 时点验证：≥1h即判 ===
        spot_match = re.search(r'\[时点:(up|down|flat)\s+conf=(\d+)%\]', note or '')
        if spot_match and hours_ago >= 1:
            spot_dir = spot_match.group(1)
            new_spot = 1 if ((spot_dir == "up" and actual_up) or (spot_dir == "down" and actual_down)) else (None if spot_dir == "flat" else 0)
            # spot_updated 在下方 stock_performance 写入段统一计数，避免重复
        
        # 仅当有可验证项时才更新
        if new_correct is None and new_spot is None:
            continue
        
        c.execute('''UPDATE predictions SET actual_close=?, actual_time=date('now'),
                     correct=COALESCE(?,correct), spot_correct=COALESCE(?,spot_correct) WHERE id=?''',
                  (round(curr_close, 2), new_correct, new_spot, pid))
        
        # 更新趋势统计
        if new_correct is not None:
            code = ts_code.split('.')[0]
            c.execute('''INSERT INTO stock_performance (ts_code, total_preds, correct_preds)
                         VALUES (?,1,?) ON CONFLICT(ts_code) DO UPDATE SET
                         total_preds=total_preds+1, correct_preds=correct_preds+?,
                         accuracy=ROUND(CAST(correct_preds+? AS REAL)/CAST(total_preds+1 AS REAL),2)''',
                      (code, new_correct, new_correct, new_correct))
        
        # 更新时点统计
        if new_spot is not None:
            code = ts_code.split('.')[0]
            c.execute('''INSERT INTO stock_performance (ts_code, spot_total, spot_correct)
                         VALUES (?,1,?) ON CONFLICT(ts_code) DO UPDATE SET
                         spot_total=spot_total+1, spot_correct=spot_correct+?''',
                      (code, new_spot, new_spot))
            spot_updated += 1
    
    # 计算时点准确率
    c.execute('''UPDATE stock_performance SET 
                 spot_accuracy = ROUND(CAST(spot_correct AS REAL)/CAST(spot_total AS REAL),2)
                 WHERE spot_total > 0''')
    
    conn_state.commit()
    return {"trend_updated": trend_updated, "spot_updated": spot_updated}


def _load_custom_codes():
    """从JSON读取自定义标的代码列表"""
    import json
    fpath = r"C:\Users\LarkMi\AppData\Local\hermes\data\custom_stocks.json"
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {s["code"] for s in data.get("locked", [])}
    except Exception:
        return {"603986.SH"}  # fallback

def get_custom_stocks():
    """返回完整自定义标的信息（供dashboard使用）"""
    import json
    fpath = r"C:\Users\LarkMi\AppData\Local\hermes\data\custom_stocks.json"
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            return json.load(f).get("locked", [])
    except Exception:
        return []

def rotate_stocks(conn_state, conn_duck):
    """根据表现轮换1-2只标的（自定义标的不参与轮换）"""
    LOCKED = _load_custom_codes()
    c = conn_state.cursor()
    c.execute('''SELECT ts_code, stock_name, accuracy FROM stock_performance 
                 WHERE total_preds >= 3 ORDER BY accuracy ASC LIMIT 2''')
    worst = c.fetchall()
    rotated = []
    for ts_code, name, acc in worst:
        if ts_code in LOCKED:
            continue  # 跳过自定义标的
        if acc is not None and acc < 0.4:
            # 从候选池找替代
            c.execute("SELECT side FROM current_roster WHERE ts_code=?", (ts_code,))
            side_row = c.fetchone()
            if side_row:
                side = side_row[0]
                c.execute('''UPDATE current_roster SET active=0 WHERE ts_code=?''', (ts_code,))
                rotated.append(f"{name}({ts_code}) 准确率{acc}→已轮换")
                c.execute('''INSERT INTO evolution_log (event_type, description) 
                             VALUES (?,?)''', ("rotate_out", f"{name}({ts_code}) acc={acc}"))
    conn_state.commit()
    return rotated


def get_acc_adjustment(conn_state, ts_code):
    """读取历史准确率，返回趋势/时点的置信度调整因子。
    acc>60%: +0.1, acc<40%: -0.2, 未知: 0"""
    try:
        c = conn_state.cursor()
        code = ts_code.split('.')[0]
        c.execute('''SELECT COALESCE(accuracy,0), COALESCE(spot_accuracy,0), total_preds, spot_total
                     FROM stock_performance WHERE ts_code=?''', (code,))
        r = c.fetchone()
        if r and r[2] >= 3:  # 至少3次验证才信
            trend_adj = 0.1 if r[0] > 0.6 else (-0.2 if r[0] < 0.4 else 0)
            spot_adj = 0.1 if r[1] > 0.6 else (-0.2 if r[1] < 0.4 else 0)
            return trend_adj, spot_adj
    except:
        pass
    return 0, 0


def run_analysis():
    """主分析流程"""
    conn_state = init_state_db()
    conn_duck = duckdb.connect(DUCKDB_PATH, read_only=True)

    hour_type, phase_name = get_hour_type()

    # 先更新之前的预测结果
    update_result = update_actuals(conn_state, conn_duck)
    updated = update_result.get("trend_updated", 0) + update_result.get("spot_updated", 0)

    # 只在盘后做轮换
    rotations = []
    if hour_type == "post":
        rotations = rotate_stocks(conn_state, conn_duck)

    results = {"long": [], "short": [], "locked": [], "meta": {}}
    
    # === 全局因子 ===
    breadth = get_market_breadth(conn_duck)
    breadth_label = "普涨" if breadth > 0.6 else ("普跌" if breadth < 0.4 else "分化")
    regime, regime_label = detect_regime(conn_duck)
    adaptive_w = get_adaptive_weights(regime)
    
    # === 全市场海选 Top5 做多+做空 ===
    locked_codes = _load_custom_codes()
    screened = screen_all_stocks(conn_duck, top_n=5, exclude_codes=locked_codes)
    
    # === 盘中批量拉取实时价格（覆盖DB滞后数据）===
    all_codes = [s['code'] for s in screened.get('long',[]) + screened.get('short',[])]
    all_codes += [s['code'] for s in get_custom_stocks()]
    rt_prices = fetch_realtime_prices(all_codes) if all_codes else {}
    
    for s in screened['long'] + screened['short']:
        ts_code = s['code']
        name = s['name']
        side = s['side']
        # 用实时价更新（盘中）
        rt = rt_prices.get(ts_code)
        display_price = rt['price'] if rt else s['price']
        # 用海选已算好的指标做时点预测（latest 用实时价）
        ind = {
            'latest': display_price, 'chg_1d': s.get('chg_1d', 0),
            'chg_5d': s['chg_5d'], 'chg_3d': s.get('chg_3d', 0),
            'trend': s['trend'], 'pos_10d': s['pos'],
            'vol_ratio': s.get('vol_ratio', 1.0), 'atr_pct': s['atr'],
        }
        spot_pred = generate_spot_prediction(ind, side, hour_type)
        # 历史准确率修正置信度
        trend_adj, spot_adj = get_acc_adjustment(conn_state, ts_code)
        adj_conf = min(0.85, max(0.25, s['conf'] + trend_adj))
        adj_spot_conf = min(0.8, max(0.2, spot_pred['confidence'] + spot_adj))
        note = s.get('note', '') + f" | 📍{spot_pred['direction']}({adj_spot_conf:.0%})"
        save_prediction(conn_state, ts_code, name, s['pred'], adj_conf, note,
                        spot_dir=spot_pred['direction'], spot_conf=adj_spot_conf)
        results[side].append({
            "code": ts_code, "name": name, "reason": s.get('reason', ''), "side": side,
            "price": display_price, "chg_5d": s['chg_5d'], "trend": s['trend'],
            "pos": s['pos'], "atr": s['atr'],
            "pred": s['pred'], "conf": adj_conf,
            "spot_dir": spot_pred['direction'], "spot_conf": adj_spot_conf,
            "note": note,
            # 🆕 技术面补充（供看板弹窗展示）
            "ma5": s.get('ma5', 0), "ma10": s.get('ma10', 0),
            "chg_1d": s.get('chg_1d', 0), "chg_3d": s.get('chg_3d', 0),
            "vol_ratio": s.get('vol_ratio', 1.0), "score": s.get('score', 0),
            "high_10d": s.get('high_10d', display_price), "low_10d": s.get('low_10d', display_price),
        })
    
    # === 自定义标的单独分析 ===
    for stock_info in get_custom_stocks():
        ts_code = stock_info['code']
        name = stock_info.get('name', ts_code)
        df = load_price_data(conn_duck, ts_code, days=15)
        ind = calc_indicators(df)
        # 盘中用实时价覆盖
        rt = rt_prices.get(ts_code)
        display_price = rt['price'] if rt else (ind.get('latest', 'N/A') if ind else 'N/A')
        if rt and ind:
            ind['latest'] = display_price
        if not ind:
            results['locked'].append({
                "code": ts_code, "name": name, "reason": stock_info.get('reason', ''),
                "price": "N/A", "chg_5d": "N/A", "trend": "N/A",
                "pos": "N/A", "pred": "pending", "conf": 0.5,
                "spot_dir": "flat", "spot_conf": 0.3, "note": "数据不足",
            })
            continue
        side_long = 'long' if ind.get('trend') != 'down' else 'short'
        trend_pred = generate_trend_prediction(ind, side_long, name)
        spot_pred = generate_spot_prediction(ind, side_long, hour_type)
        # 历史准确率修正置信度
        trend_adj2, spot_adj2 = get_acc_adjustment(conn_state, ts_code)
        adj_trend_conf = min(0.85, max(0.25, trend_pred['confidence'] + trend_adj2))
        adj_spot_conf2 = min(0.8, max(0.2, spot_pred['confidence'] + spot_adj2))
        note = trend_pred['note'] + f" | 📍{spot_pred['direction']}({adj_spot_conf2:.0%})"
        save_prediction(conn_state, ts_code, name, trend_pred['direction'], adj_trend_conf, note,
                        spot_dir=spot_pred['direction'], spot_conf=adj_spot_conf2)
        results['locked'].append({
            "code": ts_code, "name": name, "reason": stock_info.get('reason', ''),
            "price": display_price, "chg_5d": ind.get("chg_5d", "N/A"),
            "trend": ind.get("trend", "N/A"), "pos": ind.get("pos_10d", "N/A"),
            "atr": ind.get("atr_pct", "N/A"),
            "pred": trend_pred['direction'], "conf": adj_trend_conf,
            "spot_dir": spot_pred['direction'], "spot_conf": adj_spot_conf2,
            "note": note,
            # 🆕 技术面补充
            "ma5": ind.get("ma5", 0), "ma10": ind.get("ma10", 0),
            "chg_1d": ind.get("chg_1d", 0), "chg_3d": ind.get("chg_3d", 0),
            "vol_ratio": ind.get("vol_ratio", 1.0), "score": trend_pred.get("score", 0),
            "high_10d": ind.get("high_10d", display_price), "low_10d": ind.get("low_10d", display_price),
        })
    
    # Meta信息
    results["meta"] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "phase": phase_name,
        "phase_type": hour_type,
        "updated_predictions": updated,
        "rotations": rotations,
        "total_stocks": len(screened.get('long',[])) + len(screened.get('short',[])) + len(results['locked']),
        "market_breadth": f"{int(breadth*100)}%上涨 ({breadth_label})",
        "market_regime": f"{regime_label}",
        "adaptive_weights": adaptive_w,
        "factors_active": ["均线趋势(权重3)", "5日动量(权重2)", "10日位置(权重1)", "量价配合(权重1)",
                            f"阶段权重:{regime_label}", f"宽度:{breadth_label}(仅标注)", "ATR波动"],
    }

    # ==== 自动记录自进化事件 ====
    if updated > 0:
        record_evolution("accuracy_update", f"回填{updated}条预测结果", conn_state)
    if rotations:
        for r in rotations:
            record_evolution("rotation", r, conn_state)
    record_evolution("analysis_complete", f"{phase_name} | 做多{len(results['long'])}只 做空{len(results['short'])}只 自定义{len(results['locked'])}只", conn_state)

    conn_duck.close()
    conn_state.close()
    return results


# ====== 历史回测模式 ======

def load_price_data_before(conn_duck, ts_code, cutoff_date, days=10):
    """加载个股价格数据（只含cutoff_date之前的数据，用于回测）"""
    try:
        df = conn_duck.execute(f'''
            SELECT trade_date, open, high, low, close, vol, pct_chg
            FROM daily WHERE ts_code='{ts_code}' AND trade_date <= '{cutoff_date}'
            ORDER BY trade_date DESC LIMIT {days}
        ''').df()
        return df.sort_values('trade_date', ascending=True) if len(df) > 0 else None
    except:
        return None


def get_next_day_close(conn_duck, ts_code, after_date):
    """获取after_date之后第一个交易日的收盘价"""
    try:
        r = conn_duck.execute(f'''
            SELECT trade_date, close, open FROM daily WHERE ts_code='{ts_code}' 
            AND trade_date > '{after_date}'
            ORDER BY trade_date ASC LIMIT 1
        ''').fetchone()
        return (r[0], r[1], r[2]) if r else (None, None, None)
    except:
        return (None, None, None)


def run_backtest(cutoff_str, test_codes=None, random_count=3):
    """历史回测：用cutoff之前的数据预测，与cutoff之后实际结果对比。
    
    Args:
        cutoff_str: 截断日期 'YYYY-MM-DD'，用≤该日的数据做预测
        test_codes: 指定标的列表，默认None时用Top5+Top5+3只随机
        random_count: 随机抽取主板的标的数量
    
    Returns: dict with predictions, actuals, accuracy, and analysis
    """
    import random
    conn_duck = duckdb.connect(DUCKDB_PATH, read_only=True)
    
    # cutoff_date是预测截止日（用≤该日数据），实际结果看下一个交易日
    cutoff_date = cutoff_str[:10]
    
    # 确定测试标的：全市场海选（回测模式，只用≤cutoff_date数据）+自定义+随机
    if test_codes is None:
        test_codes = []
        # 全市场海选（对齐生产逻辑，使用截止日数据）
        locked_codes = _load_custom_codes()
        screened = screen_all_stocks(conn_duck, top_n=5, exclude_codes=locked_codes, cutoff_date=cutoff_date)
        for s in screened.get('long', []) + screened.get('short', []):
            test_codes.append((s['code'], s['name'], s['side'], s.get('reason', '')))
        # 加自定义标的
        for s in get_custom_stocks():
            test_codes.append((s['code'], s['name'], 'locked', s.get('reason', '')))
        # 随机抽取N只主板标的（排除已测试的）
        existing = {t[0] for t in test_codes}
        try:
            all_codes = conn_duck.execute('''
                SELECT DISTINCT ts_code FROM daily 
                WHERE trade_date = (SELECT MAX(trade_date) FROM daily)
                AND ts_code LIKE '%.SH' AND ts_code NOT LIKE '%ST%'
                AND ts_code NOT LIKE '%BJ%'
                LIMIT 500
            ''').fetchall()
            pool = [r[0] for r in all_codes if r[0] not in existing]
            random.shuffle(pool)
            for code in pool[:random_count]:
                test_codes.append((code, code.split('.')[0], 'long', '随机抽检'))
        except:
            pass
    
    results = []
    
    for ts_code, name, side, reason in test_codes:
        # 只取截止日及之前的数据
        df = load_price_data_before(conn_duck, ts_code, cutoff_date, days=15)
        ind = calc_indicators(df)
        if not ind:
            results.append({"code": ts_code, "name": name, "side": side,
                          "pred": "no_data", "conf": 0, "note": "无数据",
                          "actual_chg": None, "correct": None, "error": "无历史数据"})
            continue
        
        # 生成预测（和实时分析完全一致的逻辑）
        pred = generate_trend_prediction(ind, side, name)
        
        # 查下一个交易日的实际收盘价
        next_date, next_close, next_open = get_next_day_close(conn_duck, ts_code, cutoff_date)
        
        if next_close is None or next_open is None:
            results.append({"code": ts_code, "name": name, "side": side,
                          "pred": pred["direction"], "conf": pred["confidence"],
                          "note": pred["note"],
                          "actual_chg": None, "correct": None, "error": "无后续数据"})
            continue
        
        # 计算实际涨跌
        prev_close = ind.get("latest", next_open)
        actual_chg = (next_close - prev_close) / prev_close * 100 if prev_close and prev_close > 0 else 0
        
        # 判断预测是否正确
        pred_up = "up" in pred["direction"]
        pred_down = "down" in pred["direction"]
        actual_up = actual_chg > 0.5
        actual_down = actual_chg < -0.5
        
        if pred["direction"] == "neutral":
            correct = None  # 中性=不做判断，不参与准确率计算
        elif pred_up and actual_up:
            correct = 1
        elif pred_down and actual_down:
            correct = 1
        else:
            correct = 0
        
        results.append({
            "code": ts_code, "name": name, "side": side, "reason": reason,
            "pred": pred["direction"], "conf": pred["confidence"],
            "note": pred["note"],
            "price_at_cutoff": prev_close,
            "next_date": next_date,
            "next_open": next_open, "next_close": next_close,
            "actual_chg": round(actual_chg, 2),
            "correct": correct,
        })
    
    # 统计
    valid = [r for r in results if r["correct"] is not None]
    correct_count = sum(r["correct"] for r in valid)
    total_valid = len(valid)
    accuracy = round(correct_count / total_valid, 3) if total_valid > 0 else 0
    
    # 按方向统计
    dir_stats = {}
    for r in valid:
        d = r["pred"]
        if d not in dir_stats:
            dir_stats[d] = {"total": 0, "correct": 0}
        dir_stats[d]["total"] += 1
        dir_stats[d]["correct"] += r["correct"]
    for d in dir_stats:
        dir_stats[d]["acc"] = round(dir_stats[d]["correct"] / dir_stats[d]["total"], 2)
    
    # 按置信度分段统计
    conf_buckets = {"高(≥0.65)": [], "中(0.45-0.64)": [], "低(<0.45)": []}
    for r in valid:
        if r["conf"] >= 0.65:
            conf_buckets["高(≥0.65)"].append(r["correct"])
        elif r["conf"] >= 0.45:
            conf_buckets["中(0.45-0.64)"].append(r["correct"])
        else:
            conf_buckets["低(<0.45)"].append(r["correct"])
    conf_acc = {k: round(sum(v)/len(v), 2) if v else 0 for k, v in conf_buckets.items()}
    
    miss = [r for r in valid if r["correct"] == 0]
    
    conn_duck.close()
    
    return {
        "cutoff_date": cutoff_date,
        "total_tested": len(results),
        "total_valid": total_valid,
        "correct": correct_count,
        "accuracy": accuracy,
        "dir_stats": dir_stats,
        "conf_accuracy": conf_acc,
        "misses": [{"code": m["code"], "name": m["name"], "pred": m["pred"], 
                     "conf": m["conf"], "actual_chg": m["actual_chg"],
                     "note": m["note"]} for m in miss],
        "details": results,
    }


def format_backtest(bt):
    """格式化回测结果为可读文本"""
    emoji = {"strong_up": "🚀", "up": "📈", "neutral": "➡️", "down": "📉", "strong_down": "💥"}
    
    lines = [
        f"## 🔬 历史回测报告 — 截止日: {bt['cutoff_date']}",
        f"📊 测试{bt['total_tested']}只 | 有效{bt['total_valid']}只 | 正确{bt['correct']}只 | 准确率 **{bt['accuracy']*100:.1f}%**",
        "",
        "### 📈 按预测方向统计",
        "| 方向 | 总数 | 正确 | 准确率 |",
        "|------|------|------|--------|",
    ]
    for d, s in sorted(bt["dir_stats"].items()):
        lines.append(f"| {emoji.get(d,d)} {d} | {s['total']} | {s['correct']} | {s['acc']*100:.0f}% |")
    
    lines += [
        "",
        "### 🎯 按置信度统计",
        "| 置信度段 | 准确率 |",
        "|----------|--------|",
    ]
    for k, v in bt["conf_accuracy"].items():
        lines.append(f"| {k} | {v*100:.0f}% |")
    
    if bt["misses"]:
        lines += [
            "",
            "### ❌ 预测错误",
            "| 标的 | 预测 | 置信度 | 实际涨跌 | 信号 |",
            "|------|------|--------|----------|------|",
        ]
        for m in bt["misses"][:10]:
            sign = "+" if m["actual_chg"] > 0 else ""
            lines.append(f"| {m['name']} | {emoji.get(m['pred'],'')} {m['pred']} | {m['conf']} | {sign}{m['actual_chg']}% | {m['note'][:50]} |")
    
    return "\n".join(lines)


# ====== 全市场海选 ======

ETF_POOL = [
    {"code": "510050", "name": "上证50ETF", "reason": "大盘蓝筹"},
    {"code": "510300", "name": "沪深300ETF", "reason": "市场基准"},
    {"code": "510500", "name": "中证500ETF", "reason": "中盘成长"},
    {"code": "159915", "name": "创业板ETF", "reason": "创业板"},
    {"code": "588000", "name": "科创50ETF", "reason": "科创板"},
    {"code": "512880", "name": "证券ETF", "reason": "券商板块"},
    {"code": "512100", "name": "1000ETF", "reason": "小盘股"},
]

def screen_all_stocks(conn_duck, top_n=5, exclude_codes=None, cutoff_date=None):
    """全市场海选TopN做多+TopN做空（单次SQL+pandas批量打分）。
    排除 exclude_codes 中的标的（如自定义标的已在单独分析）。
    cutoff_date: 可选回测截止日(YYYY-MM-DD)，只使用≤该日的数据
    做空要求score≤0才入选（避免牛市强拉劣标）
    返回: {long: [...], short: [...]} 各 top_n 只
    """
    import pandas as pd, numpy as np, json
    from datetime import datetime, timedelta
    exclude = set(exclude_codes or [])
    
    # 加载股票名称映射
    LOOKUP_FILE = os.path.join(os.path.dirname(STATE_DB), "stock_lookup.json")
    name_map = {}
    try:
        with open(LOOKUP_FILE, "r", encoding="utf-8") as f:
            name_map = json.load(f)
    except Exception:
        pass
    # 从DuckDB补充名称（退市/ST股可能不在lookup中，fallback会变成纯数字导致过滤失效）
    try:
        name_rows = conn_duck.execute(
            "SELECT ts_code, name FROM stock_basic"
        ).fetchall()
        for row in name_rows:
            if row[0] not in name_map:
                name_map[row[0]] = row[1]
    except Exception:
        pass
    
    # 确定数据截止日期
    if cutoff_date:
        max_date = cutoff_date.replace('-', '')
    else:
        max_date = conn_duck.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
    dt = datetime.strptime(max_date, '%Y%m%d')
    cutoff = (dt - timedelta(days=20)).strftime('%Y%m%d')
    
    df = conn_duck.execute(f"""
        WITH ranked AS (
            SELECT ts_code, trade_date, close, high, low, vol, pct_chg,
                   ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) as rn
            FROM daily
            WHERE trade_date >= '{cutoff}' AND trade_date <= '{max_date}'
              AND ts_code NOT LIKE '%ST%'
              AND ts_code NOT LIKE '%BJ%'
              AND ts_code NOT LIKE '688%'
              AND ts_code NOT LIKE '300%'
              AND ts_code NOT LIKE '8%'
        )
        SELECT ts_code, trade_date, close, high, low, vol, pct_chg
        FROM ranked WHERE rn <= 15
        ORDER BY ts_code, trade_date
    """).df()
    
    if df.empty:
        return {'long': [], 'short': []}
    
    results = []
    for ts_code, group in df.groupby('ts_code'):
        if ts_code in exclude:
            continue
        if len(group) < 5:
            continue
        closes = group['close'].values.astype(float)
        vols = group['vol'].values.astype(float)
        highs = group['high'].values.astype(float)
        lows = group['low'].values.astype(float)
        chgs = group['pct_chg'].values.astype(float)
        latest = closes[-1]
        if latest <= 0:
            continue
        
        # MA5/MA10
        ma5 = closes[-5:].mean() if len(closes) >= 5 else latest
        ma10 = closes[-10:].mean() if len(closes) >= 10 else latest
        # 10日位置
        h10 = highs[-10:].max() if len(highs) >= 10 else latest
        l10 = lows[-10:].min() if len(lows) >= 10 else latest
        pos = (latest - l10) / (h10 - l10) * 100 if h10 > l10 else 50
        # 量比(相对前5日均量)
        vol_ratio = vols[-1] / vols[-6:-1].mean() if len(vols) >= 6 and vols[-6:-1].mean() > 0 else 1.0
        # 涨跌幅
        chg_1d = float(chgs[-1])
        chg_5d = float(chgs[-5:].sum()) if len(chgs) >= 5 else 0.0
        chg_3d = float(chgs[-3:].sum()) if len(chgs) >= 3 else 0.0  # 3日涨跌幅（用于量价信号）
        # ATR
        tr_sum = 0.0
        n_tr = min(15, len(closes)) - 1
        for i in range(1, n_tr + 1):
            h, l, pc = highs[-i], lows[-i], closes[-i-1]
            tr_sum += max(h-l, abs(h-pc), abs(l-pc))
        atr_pct = (tr_sum / n_tr) / latest * 100 if n_tr > 0 else 3.0
        
        # === 4因子连续评分（避免离散档位全同分）===
        score = 0.0
        # 1. 趋势 (权重3): MA偏离度做连续评分
        ma5_dist = (latest - ma5) / ma5 * 100 if ma5 > 0 else 0  # 距MA5百分比
        if latest > ma5 > ma10:
            score += min(3.5, 1.5 + ma5_dist * 0.3)
        elif latest < ma5 < ma10:
            score += max(-3.5, -1.5 + ma5_dist * 0.3)
        elif latest > ma5:
            score += min(2.0, 0.5 + ma5_dist * 0.3)
        elif latest < ma5:
            score += max(-2.0, -0.5 + ma5_dist * 0.3)
        # 2. 动量 (权重2): 5日涨跌幅 × 0.2（范围约-4~+4）
        score += chg_5d * 0.2
        # 3. 位置 (权重1): 0-100 映射到 -1~+1 连续值
        score += (pos - 50) * 0.02
        # 4. 量价配合 (权重1): 放量时方向×强度
        if vol_ratio > 1.3:
            score += min(1.5, chg_1d * 0.15) if chg_1d > 0 else max(-1.5, chg_1d * 0.15)
        
        name = name_map.get(ts_code, ts_code.split('.')[0])
        # 过滤退市、ST、*ST、高风险标的
        if name.startswith('退市') or name.startswith('*ST') or name.startswith('ST') or '退' in name[:2]:
            continue
        trend = "up" if latest > ma5 > ma10 else ("down" if latest < ma5 < ma10 else "neutral")
        results.append({
            'code': ts_code, 'name': name, 'score': round(score, 1),
            'price': round(latest, 2), 'chg_5d': round(chg_5d, 1),
            'chg_1d': round(chg_1d, 1), 'chg_3d': round(chg_3d, 1),
            'trend': trend, 'pos': round(pos, 1),
            'atr': round(atr_pct, 2), 'vol_ratio': round(vol_ratio, 2),
            'ma5': round(ma5, 2), 'ma10': round(ma10, 2),
            'high_10d': round(h10, 2), 'low_10d': round(l10, 2),
        })
    
    # 排序取 TopN — 动态归一化置信度
    results.sort(key=lambda x: x['score'], reverse=True)
    max_abs = max((abs(r['score']) for r in results), default=7)
    norm = max(max_abs, 7)  # 归一化分母，至少7
    seen = set()
    long_picks, short_picks = [], []
    
    for r in results:
        if r['code'] in seen: continue
        if len(long_picks) < top_n and r['score'] >= 1:
            seen.add(r['code'])
            r['side'] = 'long'
            r['pred'] = 'strong_up' if r['score'] >= max(4, norm * 0.4) else ('up' if r['score'] >= 2 else 'neutral')
            r['conf'] = round(min(0.85, abs(r['score']) / norm), 2)
            r['reason'] = f"趋势{r['trend']} 动量{r['chg_5d']}% pos{r['pos']}%"
            r['note'] = f"score={r['score']} MA5={r['ma5']} MA10={r['ma10']} | vol={r['vol_ratio']}"
            long_picks.append(r)
    
    for r in reversed(results):
        if r['code'] in seen: continue
        if r['score'] <= 0 and len(short_picks) < top_n:  # 须score≤0才纳入做空（避免牛市强选劣标）
            seen.add(r['code'])
            r['side'] = 'short'
            r['pred'] = 'strong_down' if r['score'] <= max(-4, -norm * 0.4) else 'down'
            r['conf'] = round(min(0.85, max(0.25, abs(r['score']) / norm)), 2)
            r['reason'] = f"趋势{r['trend']} 动量{r['chg_5d']}% pos{r['pos']}%"
            r['note'] = f"score={r['score']} MA5={r['ma5']} MA10={r['ma10']} | vol={r['vol_ratio']}"
            short_picks.append(r)
    
    return {'long': long_picks, 'short': short_picks, 'total_screened': len(results)}


def analyze_single_stock(ts_code):
    """单只标的独立分析（供server.py添加自定义标的时调用）"""
    import duckdb, sqlite3, json
    conn_duck = duckdb.connect(DUCKDB_PATH, read_only=True)
    conn_state = sqlite3.connect(STATE_DB)
    
    # 查中文名
    stock_name = ts_code
    try:
        with open(LOOKUP_FILE, "r", encoding="utf-8") as f:
            lookup = json.load(f)
        stock_name = lookup.get(ts_code, ts_code)
    except:
        pass
    
    df = load_price_data(conn_duck, ts_code, days=15)
    ind = calc_indicators(df)
    # 试探性预测：long和short各算一遍，取更匹配的
    pred_long = generate_trend_prediction(ind, "long", stock_name)
    pred_short = generate_trend_prediction(ind, "short", stock_name)
    # 选置信度更高的方向
    if pred_long["confidence"] >= pred_short["confidence"]:
        side, pred = "long", pred_long
    else:
        side, pred = "short", pred_short
    
    spot_pred = generate_spot_prediction(ind, side, "trading")
    save_prediction(conn_state, ts_code, stock_name, pred["direction"], pred["confidence"], pred["note"],
                    spot_dir=spot_pred["direction"], spot_conf=spot_pred["confidence"])
    record_evolution("custom_analyze", f"自定义{stock_name}({ts_code}): 趋势{pred['direction']} 时点{spot_pred['direction']} conf={pred['confidence']}", conn_state)
    
    result = {
        "code": ts_code, "name": stock_name, "side": side,
        "price": ind.get("latest", "N/A"), "chg_5d": ind.get("chg_5d", "N/A"),
        "trend": ind.get("trend", "N/A"), "pos": ind.get("pos_10d", "N/A"),
        "atr": ind.get("atr_pct", "N/A"),
        "pred": pred["direction"], "conf": pred["confidence"],
        "spot_dir": spot_pred["direction"], "spot_conf": spot_pred["confidence"],
        "note": pred["note"],
    }
    conn_duck.close()
    conn_state.close()
    return result


# ====== 技能自进化日志 ======

def record_evolution(event_type, description, conn_state=None):
    """记录自进化事件到state_db.evolution_log"""
    auto_close = conn_state is None
    if auto_close:
        conn_state = sqlite3.connect(STATE_DB)
    try:
        conn_state.cursor().execute(
            "INSERT INTO evolution_log (event_type, description) VALUES (?,?)",
            (event_type, description))
        conn_state.commit()
    except Exception:
        pass
    finally:
        if auto_close:
            conn_state.close()


def get_evolution_log(limit=20):
    """获取最近的自进化日志，供dashboard展示"""
    try:
        conn = sqlite3.connect(STATE_DB)
        rows = conn.execute(
            "SELECT event_type, description, created_at FROM evolution_log "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [{"type": r[0], "desc": r[1], "time": str(r[2])[:19]} for r in rows]
    except Exception:
        return []


def get_history_stats():
    """导出历史预测数据供看板使用（含时点准确率）"""
    import sqlite3
    conn = sqlite3.connect(STATE_DB)
    c = conn.cursor()
    
    # 各标的准确率（趋势+时点）
    try:
        c.execute('''SELECT ts_code, stock_name, total_preds, correct_preds, accuracy,
                            COALESCE(spot_total,0), COALESCE(spot_correct,0),
                            COALESCE(spot_accuracy,0)
                     FROM stock_performance ORDER BY accuracy DESC''')
        perf = []
        for r in c.fetchall():
            perf.append({
                "code": r[0], "name": r[1] or r[0],
                "total": r[2], "correct": r[3], "acc": round(r[4] or 0, 2),
                "spot_total": r[5], "spot_correct": r[6], "spot_acc": round(r[7] or 0, 2),
            })
    except sqlite3.OperationalError:
        c.execute('''SELECT ts_code, stock_name, total_preds, correct_preds, accuracy 
                     FROM stock_performance ORDER BY accuracy DESC''')
        perf = [{"code": r[0], "name": r[1] or r[0], "total": r[2], "correct": r[3],
                 "acc": round(r[4] or 0, 2), "spot_total": 0, "spot_correct": 0, "spot_acc": 0}
                for r in c.fetchall()]
    
    # 已验证的预测
    c.execute('''SELECT ts_code, stock_name, direction, pred_time, confidence, 
                        actual_close, correct, spot_correct, pred_note
                 FROM predictions WHERE actual_close IS NOT NULL 
                 ORDER BY pred_time DESC LIMIT 20''')
    verified = []
    for r in c.fetchall():
        verified.append({
            "code": r[0], "name": r[1], "pred": r[2], "time": str(r[3])[:16],
            "conf": r[4], "actual": r[5], "correct": r[6], "spot_correct": r[7],
            "note": str(r[8])[:80] if r[8] else "",
        })
    
    # 整体趋势统计
    c.execute("SELECT COUNT(*) FROM predictions WHERE actual_close IS NOT NULL AND correct IS NOT NULL")
    total_verified = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM predictions WHERE correct=1 AND actual_close IS NOT NULL")
    total_correct = c.fetchone()[0]
    overall_acc = round(total_correct / total_verified, 2) if total_verified > 0 else None
    
    # 整体时点统计
    c.execute("SELECT COUNT(*) FROM predictions WHERE spot_correct IS NOT NULL")
    spot_verified = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM predictions WHERE spot_correct=1 AND actual_close IS NOT NULL")
    spot_correct_total = c.fetchone()[0]
    spot_acc = round(spot_correct_total / spot_verified, 2) if spot_verified > 0 else None
    
    # 按方向统计
    c.execute('''SELECT direction, COUNT(*) as cnt, 
                        SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) as ok
                 FROM predictions WHERE actual_close IS NOT NULL AND correct IS NOT NULL
                 GROUP BY direction''')
    dir_stats = {}
    for r in c.fetchall():
        dir_stats[r[0]] = {"total": r[1], "correct": r[2], "acc": round(r[2]/r[1], 2) if r[1] > 0 else 0}
    
    conn.close()
    return {
        "perf": perf,
        "verified": verified,
        "total_verified": total_verified,
        "overall_acc": overall_acc,
        "spot_verified": spot_verified,
        "spot_acc": spot_acc,
        "dir_stats": dir_stats,
    }


def format_output(results):
    """格式化输出为可读文本"""
    meta = results["meta"]
    lines = [
        f"## 📊 {meta['phase']} — {meta['time']}",
        f"📌 追踪标的: {meta['total_stocks']}只 | 回填预测: {meta['updated_predictions']}条",
        "",
    ]
    if meta["rotations"]:
        lines.append("🔄 轮换记录:")
        for r in meta["rotations"]:
            lines.append(f"  • {r}")
        lines.append("")

    # 做多标的
    lines.append("### 🟢 做多标的 Top 5")
    lines.append("| # | 名称 | 现价 | 5日% | 趋势预测 | 置信度 | 时点方向 | 信号 |")
    lines.append("|---|------|------|------|----------|--------|----------|------|")
    emoji_map = {
        "strong_up": "🚀强力看多", "up": "📈看多", "neutral": "➡️中性",
        "down": "📉看空", "strong_down": "💥强力看空"
    }
    spot_emoji = {"up": "🔺涨", "down": "🔻跌", "flat": "➖平"}
    for i, s in enumerate(results["long"], 1):
        spot = spot_emoji.get(s.get("spot_dir", ""), "?")
        lines.append(f"| {i} | {s['name']} | {s['price']} | {s['chg_5d']}% | {emoji_map.get(s['pred'], s['pred'])} | {s['conf']} | {spot} | {s['note'][:30]} |")

    # 做空标的
    lines.append("")
    lines.append("### 🔴 做空标的 Top 5")
    lines.append("| # | 名称 | 现价 | 5日% | 趋势预测 | 置信度 | 时点方向 | 信号 |")
    lines.append("|---|------|------|------|----------|--------|----------|------|")
    for i, s in enumerate(results["short"], 1):
        spot = spot_emoji.get(s.get("spot_dir", ""), "?")
        lines.append(f"| {i} | {s['name']} | {s['price']} | {s['chg_5d']}% | {emoji_map.get(s['pred'], s['pred'])} | {s['conf']} | {spot} | {s['note'][:30]} |")

    # 总结
    lines.append("")
    lines.append("### 📋 综合判断")
    long_up = sum(1 for s in results["long"] if "up" in s["pred"])
    short_down = sum(1 for s in results["short"] if "down" in s["pred"])
    avg_conf_long = sum(s["conf"] for s in results["long"]) / len(results["long"]) if results["long"] else 0
    avg_conf_short = sum(s["conf"] for s in results["short"]) / len(results["short"]) if results["short"] else 0
    lines.append(f"- 做多方向: {long_up}/5 看多 (均置信度 {avg_conf_long:.2f})")
    lines.append(f"- 做空方向: {short_down}/5 看空 (均置信度 {avg_conf_short:.2f})")
    lines.append(f"- 阶段: {meta['phase']} | 下一轮: {'30分钟后' if meta['phase_type'] == 'trading' else '按计划'}")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", type=str, metavar="YYYY-MM-DD",
                    help="历史回测模式：用截止日之前数据预测，与之后实际结果对比")
    ap.add_argument("--random", type=int, default=3,
                    help="回测时随机抽检的标的数量 (默认3)")
    ap.add_argument("--codes", type=str, nargs="*",
                    help="指定回测标的代码 (如 002384.SZ 600403.SH)")
    args = ap.parse_args()
    
    if args.backtest:
        test_codes = None
        if args.codes:
            test_codes = [(c, c, 'long', '指定') for c in args.codes]
        bt = run_backtest(args.backtest, test_codes=test_codes, random_count=args.random)
        print(format_backtest(bt))
    else:
        try:
            results = run_analysis()
            print(format_output(results))
        except Exception as e:
            print(f"❌ 分析出错: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            sys.exit(1)
