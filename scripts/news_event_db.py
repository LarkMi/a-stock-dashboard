#!/usr/bin/env python3
"""
news_event_db.py — 新闻/事件/研报情绪评分引擎
双层架构: 静态事件库(已知) + 动态评分(agent搜索)
每个事件有: 日期/标的/类型/影响分/来源
"""

import sqlite3
from datetime import datetime

DB_PATH = r"C:\Users\LarkMi\AppData\Local\hermes\data\news_sentiment.db"

# ====== 事件类型定义 + 影响分 ======
EVENT_TYPES = {
    # 强利好
    "insider_buy":        {"score": +3.0, "label": "大股东增持", "decay_days": 20},
    "earnings_beat":      {"score": +2.0, "label": "业绩超预期", "decay_days": 10},
    "analyst_upgrade":    {"score": +2.0, "label": "券商上调评级", "decay_days": 7},
    "major_contract":     {"score": +1.5, "label": "重大合同/订单", "decay_days": 7},
    "buyback":            {"score": +1.5, "label": "公司回购", "decay_days": 14},
    "policy_positive":    {"score": +1.0, "label": "行业利好政策", "decay_days": 10},
    "index_inclusion":    {"score": +1.5, "label": "纳入重要指数", "decay_days": 14},
    # 强利空
    "insider_sell":       {"score": -2.5, "label": "大股东减持", "decay_days": 30},
    "analyst_downgrade":  {"score": -2.0, "label": "券商下调评级", "decay_days": 7},
    "earnings_miss":      {"score": -2.0, "label": "业绩不及预期", "decay_days": 10},
    "regulatory_probe":   {"score": -3.0, "label": "立案调查/监管处罚", "decay_days": 60},
    "lockup_expiry":      {"score": -1.5, "label": "大额限售解禁", "decay_days": 7},
    "policy_negative":    {"score": -1.0, "label": "行业利空政策", "decay_days": 10},
    # 中性
    "earnings_pre":       {"score": 0.0,  "label": "业绩预告(待确认)", "decay_days": 5},
    "restructuring":      {"score": 0.0,  "label": "重大资产重组", "decay_days": 30},
}

# ====== 已知事件（静态预加载） ======
KNOWN_EVENTS = [
    # A50纳入 — 6/18生效
    {"ts_code": "603986.SH", "event_date": "20260618", "type": "index_inclusion", "note": "富时A50纳入6/18生效"},
    {"ts_code": "002384.SZ", "event_date": "20260618", "type": "index_inclusion", "note": "富时A50纳入6/18生效"},
    {"ts_code": "000338.SZ", "event_date": "20260618", "type": "index_inclusion", "note": "富时A50纳入6/18生效"},
    # 朱一明减持
    {"ts_code": "603986.SH", "event_date": "20260527", "type": "insider_sell", "note": "朱一明拟减持1.60%(约28亿)"},
    # 兆易创新 — ESG评级AA（正面但影响小）
    {"ts_code": "603986.SH", "event_date": "20260605", "type": "policy_positive", "note": "华证ESG评级AA,行业第4"},
]


def init_event_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT, event_date TEXT, event_type TEXT,
        score REAL, label TEXT, note TEXT,
        source TEXT DEFAULT 'static',
        active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(ts_code, event_date, event_type)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sentiment_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code TEXT, check_time TEXT,
        total_score REAL, active_events TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # 预加载已知事件（先去重）
    seen = set()
    for ev in KNOWN_EVENTS:
        key = (ev["ts_code"], ev["event_date"], ev["type"])
        if key in seen:
            continue
        seen.add(key)
        etype = EVENT_TYPES.get(ev["type"], {"score": 0, "label": ev["type"]})
        c.execute('''INSERT OR IGNORE INTO events 
            (ts_code, event_date, event_type, score, label, note)
            VALUES (?,?,?,?,?,?)''',
            (ev["ts_code"], ev["event_date"], ev["type"],
             etype["score"], etype["label"], ev["note"]))
    conn.commit()
    return conn


def get_active_events(conn, ts_code, as_of_date=None):
    """获取某标的所有活跃事件的总分（含前瞻事件）"""
    if as_of_date is None:
        as_of_date = datetime.now().strftime("%Y%m%d")
    as_of_dt = datetime.strptime(as_of_date, "%Y%m%d")
    c = conn.cursor()
    # 查所有事件（不限日期），在Python里过滤
    c.execute('''SELECT event_type, score, label, note, event_date FROM events
                 WHERE ts_code=? AND active=1 ORDER BY event_date''', (ts_code,))
    all_events = c.fetchall()
    
    total = 0.0
    active_labels = []
    for etype, score, label, note, edate_str in all_events:
        edate = datetime.strptime(edate_str, "%Y%m%d")
        decay = EVENT_TYPES.get(etype, {}).get("decay_days", 14)
        days_diff = (as_of_dt - edate).days  # 正=已过去, 负=未来
        
        # 前瞻事件（如指数纳入）：生效前 anticip_days 天开始计入
        anticip_days = 14 if etype == "index_inclusion" else 0
        if days_diff < -anticip_days:
            continue  # 太早，还没到前瞻窗口
        
        # 衰减计算
        if days_diff > decay * 3:
            continue  # 完全过期
        if days_diff > 0:
            decay_factor = max(0.2, 1.0 - days_diff / (decay * 3))
        else:
            # 前瞻事件：越接近生效日权重越高
            decay_factor = max(0.3, 1.0 + days_diff / anticip_days) if anticip_days > 0 else 1.0
        
        effective_score = score * decay_factor
        total += effective_score
        marker = "→生效" if days_diff < 0 else ""
        active_labels.append(f"{label}{marker}({effective_score:+.1f})")
    
    sentiment = "利好" if total > 1 else ("利空" if total < -1 else "中性")
    return round(total, 2), active_labels, sentiment


def apply_event_modifier(base_score, ts_code, side, conn_event):
    """将事件评分叠加到技术评分上"""
    total, labels, sentiment = get_active_events(conn_event, ts_code)
    if total == 0:
        return base_score, "", 0
    
    # 事件分 → 叠加到技术评分
    # 做多标的: 利空事件打压得分，利好事件提升得分
    modifier = total if side == "long" else -total
    adjusted = base_score + modifier
    
    label_str = "; ".join(labels)
    return adjusted, label_str, total


def log_sentiment(conn, ts_code, total_score, events_str):
    c = conn.cursor()
    c.execute('''INSERT INTO sentiment_log (ts_code, check_time, total_score, active_events)
                 VALUES (?,datetime('now'),?,?)''', (ts_code, total_score, events_str))
    conn.commit()


if __name__ == "__main__":
    # 测试
    conn = init_event_db()
    print("=== 已知事件库 ===")
    for ts in ["603986.SH", "002384.SZ", "000338.SZ", "600403.SH"]:
        score, labels, sent = get_active_events(conn, ts)
        print(f"  {ts}: 总分={score:+.1f} ({sent}) — {labels}")
    
    print("\n=== 测试事件叠加 ===")
    for ts, side in [("603986.SH","long"),("002384.SZ","long"),("600403.SH","long")]:
        base = 2.0  # 假设技术面看多
        adj, lbl, _ = apply_event_modifier(base, ts, side, conn)
        print(f"  {ts}({side}): 技术={base:+.1f} + 事件={lbl} → 综合={adj:+.1f}")
    conn.close()
