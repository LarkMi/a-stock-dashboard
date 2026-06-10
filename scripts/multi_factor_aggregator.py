#!/usr/bin/env python3
"""
multi_factor_aggregator.py — 多因子聚合模块 (Phase 5)

聚合4维度因子并生成综合评分与预测方向:
  技术面 35%: trend, momentum, position, volume, atr, distribution, sr
  情绪面 25%: breadth, limit_ratio, northbound, margin  (全市场统一)
  宏观面 20%: sp500, nasdaq, fx, vix                   (全市场统一)
  消息面 20%: announcement, policy, dragon_tiger

核心接口:
  aggregate_all(duckdb_conn, ts_codes, trade_date, cache_dir=None) -> list[dict]
  aggregate_single(duckdb_conn, ts_code, trade_date, cache_dir=None) -> dict

依赖: tech_factors, sentiment_factors, macro_factors, news_factors
弹性: 任一因子不可用时返回0(中性)，不崩溃
"""

import os
import sys
import json
import time
import sqlite3
from datetime import datetime

# 确保脚本目录在 path 中以导入同目录模块
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from tech_factors import compute_tech_factors, compute_tech_factors_batch
from sentiment_factors import compute_sentiment_factors
from macro_factors import compute_macro_factors
from news_factors import compute_news_factors, compute_news_factors_batch

# ─── 常量 ────────────────────────────────────────────────

# 维度权重
W_TECH = 0.35
W_SENTIMENT = 0.25
W_MACRO = 0.20
W_NEWS = 0.20

# 技术面子因子权重
TECH_W = dict(trend=0.30, momentum=0.30, position=0.15,
              volume=0.15, atr=0.05, distribution=0.03, sr=0.02)

# 情绪面子因子权重
SENT_W = dict(breadth=0.8, limit_ratio=0.5, northbound=0.5, margin=0.3)

# 宏观面子因子权重
MACRO_W = dict(sp500=0.8, nasdaq=0.5, fx=0.4, vix=0.5)

# 消息面子因子权重
NEWS_W = dict(announcement=0.8, policy=0.5, dragon_tiger=0.5)

# 总得分归一化范围
SCORE_MIN, SCORE_MAX = -5.0, 5.0

# 默认缓存目录
_DEFAULT_CACHE_DIR = os.path.join(os.path.dirname(_SCRIPT_DIR), "data", "cache")


# ─── 辅助函数 ────────────────────────────────────────────

def _safe(val, default=0.0):
    """None → default, 否则 float()"""
    if val is None:
        return float(default)
    return float(val)


def _weighted_sum(factors: dict, weights: dict) -> float:
    """加权求和: sum(factors[k] * weights[k]), 缺失因子用0"""
    total = 0.0
    for k, w in weights.items():
        total += _safe(factors.get(k)) * w
    return total


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _map_direction(score: float) -> str:
    """总得分 → 预测方向字符串"""
    if score >= 2.0:
        return "strong_up"
    elif score >= 0.5:
        return "up"
    elif score <= -2.0:
        return "strong_down"
    elif score <= -0.5:
        return "down"
    else:
        return "neutral"


def _compute_confidence(tech_s, sent_s, macro_s, news_s) -> float:
    """基于4维度方向一致性计算置信度 [0, 1]

    规则:
      - 4个同向 (全正/全负) → 1.0
      - 3同1异 → 0.7
      - 2同2异 → 0.5
      - 完全分歧 → 0.3
    """
    signs = [
        _sign(tech_s), _sign(sent_s), _sign(macro_s), _sign(news_s)
    ]
    pos_count = signs.count(+1)
    neg_count = signs.count(-1)
    same = max(pos_count, neg_count)
    if same == 4:
        return 1.0
    elif same == 3:
        return 0.7
    elif same == 2:
        return 0.5
    else:
        return 0.3


def _sign(v: float) -> int:
    """符号: +1 / 0 / -1, 小值视作0"""
    if v > 0.15:
        return +1
    elif v < -0.15:
        return -1
    return 0


def _generate_signals(tech_f: dict, sent_f: dict, macro_f: dict, news_f: dict) -> list[str]:
    """基于因子值生成可读信号标签"""
    sigs = []

    # 技术面信号
    t = tech_f or {}
    if _safe(t.get('trend')) > 1.0:
        sigs.append("均线多头")
    elif _safe(t.get('trend')) < -1.0:
        sigs.append("均线空头")
    if _safe(t.get('momentum')) > 1.5:
        sigs.append("动量增强")
    elif _safe(t.get('momentum')) < -1.5:
        sigs.append("动量衰减")
    if _safe(t.get('volume')) > 0.5:
        sigs.append("放量上涨")
    elif _safe(t.get('volume')) < -0.5:
        sigs.append("放量下跌")

    # 情绪面信号
    s = sent_f or {}
    if _safe(s.get('breadth')) > 0.8:
        sigs.append("市场偏牛")
    elif _safe(s.get('breadth')) < -0.8:
        sigs.append("市场偏熊")
    if _safe(s.get('northbound')) > 0.5:
        sigs.append("北向流入")
    elif _safe(s.get('northbound')) < -0.5:
        sigs.append("北向流出")
    if _safe(s.get('margin')) > 0.5:
        sigs.append("融资增加")
    elif _safe(s.get('margin')) < -0.5:
        sigs.append("融资减少")

    # 宏观面信号
    m = macro_f or {}
    if _safe(m.get('sp500')) > 0.5:
        sigs.append("美股走强")
    elif _safe(m.get('sp500')) < -0.5:
        sigs.append("美股走弱")
    if _safe(m.get('vix')) < -0.5:
        sigs.append("VIX低恐慌")
    elif _safe(m.get('vix')) > 0.5:
        sigs.append("VIX高恐慌")

    # 消息面信号
    n = news_f or {}
    if _safe(n.get('announcement')) > 0.5:
        sigs.append("利好公告")
    elif _safe(n.get('announcement')) < -0.5:
        sigs.append("利空公告")
    if _safe(n.get('policy')) > 0.5:
        sigs.append("政策利好")
    elif _safe(n.get('policy')) < -0.5:
        sigs.append("政策收紧")
    if _safe(n.get('dragon_tiger')) > 0.5:
        sigs.append("游资做多")
    elif _safe(n.get('dragon_tiger')) < -0.5:
        sigs.append("游资出逃")

    return sigs


def _get_stock_name(conn, ts_code: str) -> str:
    """从 DuckDB 获取股票名称, 失败返回空串"""
    try:
        row = conn.execute(
            "SELECT name FROM daily_adj WHERE ts_code = ? LIMIT 1",
            [ts_code]
        ).fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def _batch_stock_names(conn, ts_codes: list[str]) -> dict:
    """批量获取股票名称"""
    if not ts_codes:
        return {}
    try:
        placeholders = ','.join(['?' for _ in ts_codes])
        rows = conn.execute(
            f"SELECT DISTINCT ts_code, name FROM daily_adj "
            f"WHERE ts_code IN ({placeholders})",
            ts_codes
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {c: "" for c in ts_codes}


# ─── 快照表 ──────────────────────────────────────────────

def _init_snapshot_db(db_path: str) -> sqlite3.Connection:
    """建表 + 返回连接"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS factor_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_code         TEXT NOT NULL,
            stock_name      TEXT DEFAULT '',
            tech_score      REAL DEFAULT 0,
            message_score   REAL DEFAULT 0,
            sentiment_score REAL DEFAULT 0,
            macro_score     REAL DEFAULT 0,
            total_score     REAL DEFAULT 0,
            pred_direction  TEXT DEFAULT 'neutral',
            confidence      REAL DEFAULT 0,
            snapshot_time   TEXT NOT NULL,
            tech_factors    TEXT DEFAULT '{}',
            message_factors TEXT DEFAULT '{}',
            sentiment_factors TEXT DEFAULT '{}',
            macro_factors   TEXT DEFAULT '{}'
        )
    """)
    conn.commit()
    return conn


def _write_snapshots(db_path: str, results: list[dict]) -> int:
    """将聚合结果批量写入 factor_snapshots 表, 返回写入行数"""
    conn = _init_snapshot_db(db_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    count = 0
    try:
        for r in results:
            dims = r.get("dimensions", {})
            details = r.get("factor_details", {})
            conn.execute("""
                INSERT INTO factor_snapshots
                    (ts_code, stock_name,
                     tech_score, message_score, sentiment_score, macro_score,
                     total_score, pred_direction, confidence, snapshot_time,
                     tech_factors, message_factors, sentiment_factors, macro_factors)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?)
            """, (
                r["ts_code"], r.get("name", ""),
                round(dims.get("tech", 0), 4),
                round(dims.get("news", 0), 4),
                round(dims.get("sentiment", 0), 4),
                round(dims.get("macro", 0), 4),
                round(r["total_score"], 4),
                r["pred_direction"],
                round(r["confidence"], 4),
                now,
                json.dumps(details.get("tech", {}), ensure_ascii=False),
                json.dumps(details.get("news", {}), ensure_ascii=False),
                json.dumps(details.get("sentiment", {}), ensure_ascii=False),
                json.dumps(details.get("macro", {}), ensure_ascii=False),
            ))
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count


# ─── 单只聚合 ────────────────────────────────────────────

def aggregate_single(duckdb_conn, ts_code: str, trade_date: str,
                     cache_dir: str = None,
                     # 可选: 预计算的全市场因子 (批量场景复用)
                     _sentiment_cache: dict = None,
                     _macro_cache: dict = None) -> dict:
    """单只标的的多因子聚合。

    Args:
        duckdb_conn: DuckDB 连接 (read_only)
        ts_code: 标的代码, 如 '603986.SH'
        trade_date: 交易日期 'YYYY-MM-DD'
        cache_dir: 缓存目录, 默认 data/cache/
        _sentiment_cache: 预计算的情绪结果 (复用)
        _macro_cache: 预计算的宏观结果 (复用)

    Returns:
        {
            "ts_code": "603986.SH",
            "name": "兆易创新",
            "total_score": 2.35,
            "pred_direction": "up",
            "confidence": 0.72,
            "dimensions": {"tech": 2.1, "sentiment": 1.2, "macro": -0.3, "news": 1.8},
            "factor_details": {"tech": {...}, "news": {...}, ...},
            "signals": ["均线多头", "北向流入", ...],
        }
    """
    cd = cache_dir or _DEFAULT_CACHE_DIR
    macro_db = os.path.join(cd, "macro_cache.db")

    # ── 技术面 (per-stock) ──
    tech_f = {}
    tech_dim = 0.0
    try:
        tech_f = compute_tech_factors(duckdb_conn, ts_code, trade_date)
        # _empty_result → 所有值为None → _safe 全转0
        tech_dim = _weighted_sum(tech_f, TECH_W)
    except Exception as e:
        tech_f = {"error": str(e)}
        tech_dim = 0.0

    # ── 情绪面 (全市场, 复用缓存) ──
    sent_f = _sentiment_cache
    sent_dim = 0.0
    if sent_f is None:
        try:
            sent_f = compute_sentiment_factors(duckdb_conn, trade_date, cd)
        except Exception as e:
            sent_f = {"error": str(e)}
    sent_dim = _weighted_sum(sent_f or {}, SENT_W)

    # ── 宏观面 (全市场, 复用缓存) ──
    macro_f = _macro_cache
    macro_dim = 0.0
    if macro_f is None:
        try:
            macro_f = compute_macro_factors(trade_date, macro_db)
        except Exception as e:
            macro_f = {"error": str(e)}
    # macro_f 中带着 _meta key, 排除它
    macro_f_clean = {k: v for k, v in (macro_f or {}).items() if k != "_meta"}
    macro_dim = _weighted_sum(macro_f_clean, MACRO_W)

    # ── 消息面 (per-stock) ──
    news_db = os.path.join(cd, "news_factors_cache.db")
    news_f = {}
    news_dim = 0.0
    try:
        news_f = compute_news_factors(ts_code, news_db)
    except Exception as e:
        news_f = {"error": str(e)}
    news_dim = _weighted_sum(news_f or {}, NEWS_W)

    # ── 加权聚合 ──
    raw_total = (tech_dim * W_TECH +
                 sent_dim * W_SENTIMENT +
                 macro_dim * W_MACRO +
                 news_dim * W_NEWS)
    total_score = _clamp(raw_total, SCORE_MIN, SCORE_MAX)
    direction = _map_direction(total_score)
    confidence = _compute_confidence(tech_dim, sent_dim, macro_dim, news_dim)
    name = _get_stock_name(duckdb_conn, ts_code)
    signals = _generate_signals(tech_f, sent_f, macro_f, news_f)

    return {
        "ts_code": ts_code,
        "name": name,
        "total_score": round(total_score, 4),
        "pred_direction": direction,
        "confidence": round(confidence, 4),
        "dimensions": {
            "tech": round(tech_dim, 4),
            "sentiment": round(sent_dim, 4),
            "macro": round(macro_dim, 4),
            "news": round(news_dim, 4),
        },
        "factor_details": {
            "tech": {k: _safe(v) for k, v in (tech_f or {}).items() if k != "indicators"},
            "sentiment": {k: _safe(v) for k, v in (sent_f or {}).items()
                          if k not in ("raw", "warnings", "computed_at")},
            "macro": {k: _safe(v) for k, v in (macro_f_clean or {}).items()},
            "news": {k: _safe(v) for k, v in (news_f or {}).items()},
        },
        "signals": signals,
    }


# ─── 全量聚合 ────────────────────────────────────────────

def aggregate_all(duckdb_conn, ts_codes: list[str], trade_date: str,
                  cache_dir: str = None,
                  write_snapshots: bool = True) -> list[dict]:
    """批量多因子聚合，结果按 total_score 降序排列。

    Args:
        duckdb_conn: DuckDB 连接 (read_only)
        ts_codes: 标的代码列表
        trade_date: 交易日期 'YYYY-MM-DD'
        cache_dir: 缓存目录
        write_snapshots: 是否写入 factor_snapshots 表

    Returns:
        list[dict]: 按 total_score 降序排列的聚合结果

    性能:
        全市场因子(情绪+宏观)仅计算1次, 逐只复用。
        目标: 20只 <60s。
    """
    cd = cache_dir or _DEFAULT_CACHE_DIR
    macro_db = os.path.join(cd, "macro_cache.db")
    t0 = time.time()

    # ── 1. 计算全市场因子 (仅一次) ──
    sent_result = None
    try:
        sent_result = compute_sentiment_factors(duckdb_conn, trade_date, cd)
    except Exception:
        sent_result = {}

    macro_result = None
    try:
        macro_result = compute_macro_factors(trade_date, macro_db)
    except Exception:
        macro_result = {}

    # ── 2. 批量股票名 ──
    names = _batch_stock_names(duckdb_conn, ts_codes)

    # ── 3. 逐只聚合 ──
    results = []
    for ts_code in ts_codes:
        try:
            r = aggregate_single(
                duckdb_conn, ts_code, trade_date, cd,
                _sentiment_cache=sent_result,
                _macro_cache=macro_result,
            )
            # 用批量查到的名称覆盖 (批量更全)
            if names.get(ts_code):
                r["name"] = names[ts_code]
            results.append(r)
        except Exception as e:
            # 极端情况: 单只崩溃不应影响整体
            results.append({
                "ts_code": ts_code,
                "name": names.get(ts_code, ""),
                "total_score": 0.0,
                "pred_direction": "neutral",
                "confidence": 0.0,
                "dimensions": {"tech": 0, "sentiment": 0, "macro": 0, "news": 0},
                "factor_details": {},
                "signals": [],
                "error": str(e),
            })

    # ── 4. 排序 ──
    results.sort(key=lambda x: x["total_score"], reverse=True)

    # ── 5. 写入快照 ──
    if write_snapshots and results:
        snap_db = os.path.join(cd, "factor_snapshots.db")
        try:
            n = _write_snapshots(snap_db, results)
        except Exception:
            n = 0

    elapsed = time.time() - t0
    # 在第一条结果上附加元信息
    if results:
        results[0]["_meta"] = {
            "stock_count": len(results),
            "elapsed_sec": round(elapsed, 2),
            "cache_dir": cd,
            "aggregated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    return results


# ─── CLI 自检入口 ────────────────────────────────────────

if __name__ == "__main__":
    import duckdb

    db_path = None
    # 尝试自动发现 DuckDB
    try:
        from tech_factors import _discover_duckdb
        db_path = _discover_duckdb()
    except Exception:
        pass

    if not db_path:
        print("X 未找到 DuckDB 文件")
        exit(1)

    print(f"DuckDB: {db_path}")
    conn = duckdb.connect(db_path, read_only=True)

    today = datetime.now().strftime("%Y-%m-%d")
    test_codes = ["603986.SH", "000001.SZ", "600519.SH"]

    print(f"\n=== 单只聚合测试 ({today}) ===\n")
    for code in test_codes:
        result = aggregate_single(conn, code, today)
        print(f"--- {code} {result['name']} ---")
        print(f"  总分: {result['total_score']:+.2f}  方向: {result['pred_direction']}  置信: {result['confidence']:.2f}")
        print(f"  维度: 技术={result['dimensions']['tech']:+.2f} "
              f"情绪={result['dimensions']['sentiment']:+.2f} "
              f"宏观={result['dimensions']['macro']:+.2f} "
              f"消息={result['dimensions']['news']:+.2f}")
        if result['signals']:
            print(f"  信号: {', '.join(result['signals'])}")
        print()

    print(f"=== 批量聚合测试 ({len(test_codes)}只) ===\n")
    results = aggregate_all(conn, test_codes, today)
    for r in results:
        meta = r.pop("_meta", None)
        print(f"  {r['ts_code']:12s} {r['name']:6s}  "
              f"总分={r['total_score']:+.2f}  "
              f"方向={r['pred_direction']:12s}  "
              f"置信={r['confidence']:.2f}")
    if meta:
        print(f"\n  耗时: {meta.get('elapsed_sec', '?')}s")
        print(f"  快照目录: {meta.get('cache_dir')}")

    conn.close()
    print("\n完成")
