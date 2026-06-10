#!/usr/bin/env python3
"""
sentiment_factors.py — 情绪面因子独立模块

4个全市场统一因子（不逐只计算）:
  breadth    (市场宽度,  ±2.0)  上涨比例 = COUNT(pct_chg>0)/COUNT(*), 排除ST/BJ
  limit_ratio(涨跌停比,  ±1.5)  涨停数/(涨停+跌停), pct_chg≥9.9%=涨停
  northbound (北向资金,  ±1.5)  web抓取净买入额(亿), >30亿看多
  margin     (融资融券,  ±1.0)  web抓取融资余额变化

接口: compute_sentiment_factors(duckdb_conn, trade_date, cache_dir=None) -> dict

缓存策略:
  - 市场宽度/涨跌停比: 实时从DuckDB计算
  - 北向资金/融资融券: SQLite缓存2h过期; web抓取失败用缓存(≤24h)
  - 非交易日(周末/节假日): 返回缓存最后已知值

依赖: duckdb, pandas, numpy, sqlite3, urllib, json, datetime
"""

import os
import glob
import json
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta

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


# ====== 交易日判断 ======

def _is_weekend(date_str: str) -> bool:
    """判断是否周末 (YYYY-MM-DD 或 YYYYMMDD)"""
    try:
        if len(date_str) == 8:
            dt = datetime.strptime(date_str, '%Y%m%d')
        else:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
        return dt.weekday() >= 5  # 周六(5)/周日(6)
    except ValueError:
        return False


def _normalize_date(date_str: str) -> str:
    """统一日期格式为 YYYYMMDD"""
    date_str = date_str.strip().replace('-', '')
    return date_str


# ====== 因子1: 市场宽度 ======

def factor_breadth(conn, trade_date: str) -> float:
    """计算市场宽度因子 (值域 ±2.0)
    
    breadth = COUNT(pct_chg>0) / COUNT(*)  (排除ST/BJ)
    >0.6偏牛情绪, <0.4偏熊情绪
    线性映射: (breadth - 0.5) / 0.1 → clamp ±2.0
    """
    trade_date = _normalize_date(trade_date)
    
    try:
        df = conn.execute(f"""
            SELECT pct_chg
            FROM daily_adj
            WHERE trade_date = '{trade_date}'
              AND ts_code NOT LIKE '%ST%'
              AND ts_code NOT LIKE '%BJ%'
              AND ts_code NOT LIKE '8%'
              AND pct_chg IS NOT NULL
        """).df()
        
        if df.empty:
            return None
        
        up_count = int((df['pct_chg'] > 0).sum())
        total = len(df)
        raw = up_count / total if total > 0 else 0.5
        
        # 线性映射: (raw - 0.5) / 0.1 → ±2.0
        score = (raw - 0.5) / 0.1
        return float(np.clip(score, -2.0, 2.0))
    
    except Exception:
        return None


# ====== 因子2: 涨跌停比 ======

def factor_limit_ratio(conn, trade_date: str) -> float:
    """计算涨跌停比因子 (值域 ±1.5)
    
    涨停 ≥ 9.9%, 跌停 ≤ -9.9%
    比值 = 涨停数 / (涨停+跌停)
    映射: (ratio - 0.5) / 0.333 → clamp ±1.5
    """
    trade_date = _normalize_date(trade_date)
    
    try:
        df = conn.execute(f"""
            SELECT pct_chg
            FROM daily_adj
            WHERE trade_date = '{trade_date}'
              AND ts_code NOT LIKE '%ST%'
              AND ts_code NOT LIKE '%BJ%'
              AND ts_code NOT LIKE '8%'
              AND pct_chg IS NOT NULL
        """).df()
        
        if df.empty:
            return None
        
        limit_up = int((df['pct_chg'] >= 9.9).sum())
        limit_down = int((df['pct_chg'] <= -9.9).sum())
        total_limits = limit_up + limit_down
        
        if total_limits == 0:
            return 0.0  # 无涨跌停 → 中性
        
        raw = limit_up / total_limits
        
        # 映射: (raw - 0.5) / 0.333 → ±1.5
        score = (raw - 0.5) / 0.333
        return float(np.clip(score, -1.5, 1.5))
    
    except Exception:
        return None


# ====== 缓存层 (SQLite) ======

_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sentiment_cache (
    factor_name TEXT PRIMARY KEY,
    value       REAL,
    raw_data    TEXT,
    updated_at  TEXT DEFAULT (datetime('now', 'localtime'))
);
"""


def _get_cache(cache_dir: str, factor_name: str, max_age_hours: float = 24.0) -> dict | None:
    """读取缓存，超期返回None"""
    if not cache_dir:
        return None
    try:
        db_path = os.path.join(cache_dir, 'sentiment_cache.db')
        conn = sqlite3.connect(db_path)
        conn.execute(_CACHE_SCHEMA)
        cur = conn.execute(
            "SELECT value, raw_data, updated_at FROM sentiment_cache WHERE factor_name = ?",
            (factor_name,)
        )
        row = cur.fetchone()
        conn.close()
        
        if row is None:
            return None
        
        value, raw_data, updated_at = row
        age = (datetime.now() - datetime.strptime(updated_at, '%Y-%m-%d %H:%M:%S')).total_seconds() / 3600
        
        if age > max_age_hours:
            return None
        
        return {'value': value, 'raw_data': raw_data, 'updated_at': updated_at}
    except Exception:
        return None


def _set_cache(cache_dir: str, factor_name: str, value: float, raw_data: str = None):
    """写入缓存"""
    if not cache_dir:
        return
    try:
        db_path = os.path.join(cache_dir, 'sentiment_cache.db')
        conn = sqlite3.connect(db_path)
        conn.execute(_CACHE_SCHEMA)
        conn.execute(
            """INSERT OR REPLACE INTO sentiment_cache (factor_name, value, raw_data, updated_at)
               VALUES (?, ?, ?, datetime('now', 'localtime'))""",
            (factor_name, value, raw_data or '')
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _get_cached_value(cache_dir: str, factor_name: str, trade_date: str, max_age_hours: float = 2.0) -> float | None:
    """获取缓存值，2h过期回退24h"""
    # 先尝试2h新鲜缓存
    cache = _get_cache(cache_dir, factor_name, max_age_hours=2.0)
    if cache is not None:
        return cache['value']
    # 回退24h过期缓存
    cache = _get_cache(cache_dir, factor_name, max_age_hours=24.0)
    if cache is not None:
        return cache['value']
    return None


# ====== Web抓取工具 ======

def _fetch_url(url: str, timeout: int = 10) -> str | None:
    """HTTP GET抓取，返回文本或None"""
    try:
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Referer': 'https://data.eastmoney.com/',
            }
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='ignore')
    except Exception:
        return None


# ====== 因子3: 北向资金 ======

def factor_northbound(trade_date: str, cache_dir: str = None) -> float:
    """北向资金因子 (值域 ±1.5)
    
    抓取东方财富北向资金实时净流入(亿)
    >30亿看多, <-30亿看空
    映射: net / 30 * 0.5 → clamp ±1.5
    
    非交易日返回缓存值
    """
    trade_date = _normalize_date(trade_date)
    
    # 非交易日 → 返回缓存
    if _is_weekend(trade_date):
        cached = _get_cached_value(cache_dir, 'northbound', trade_date, max_age_hours=24.0)
        if cached is not None:
            return cached
        return 0.0  # 无缓存 → 中性
    
    # 检查新鲜缓存 (<2h)
    cached = _get_cached_value(cache_dir, 'northbound', trade_date, max_age_hours=2.0)
    if cached is not None:
        return cached
    
    # 抓取东方财富北向资金API
    net_flow = _fetch_northbound_net()
    if net_flow is not None:
        score = np.clip(net_flow / 30 * 0.5, -1.5, 1.5)
        _set_cache(cache_dir, 'northbound', float(score), f'net={net_flow:.2f}亿')
        return float(score)
    
    # 抓取失败 → 用旧缓存
    cached = _get_cached_value(cache_dir, 'northbound', trade_date, max_age_hours=24.0)
    if cached is not None:
        return cached
    
    return 0.0  # 完全无数据 → 中性


def _fetch_northbound_net() -> float | None:
    """抓取北向资金当日净流入(亿)
    
    数据源: 东方财富 沪深港通资金流向
    API: push2.eastmoney.com → 沪股通+深股通净买入合计
    """
    try:
        # 沪股通日级别最新一条
        url_sh = (
            "https://push2.eastmoney.com/api/qt/kamt.kline/get?"
            "fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56&"
            "klt=101&lmt=1"  # 101=日K
        )
        data_sh = _fetch_url(url_sh, timeout=8)
        if not data_sh:
            return None
        j_sh = json.loads(data_sh)
        klines_sh = j_sh.get('data', {}).get('klines', [])
        
        # 深股通
        url_sz = (
            "https://push2.eastmoney.com/api/qt/kamt.kline/get?"
            "fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56&"
            "klt=101&lmt=1"
        )
        data_sz = _fetch_url(url_sz, timeout=8)
        net_sz = 0.0
        if data_sz:
            j_sz = json.loads(data_sz)
            klines_sz = j_sz.get('data', {}).get('klines', [])
            if klines_sz:
                # f54=深股通净买入(亿)
                parts = klines_sz[0].split(',')
                if len(parts) >= 5:
                    net_sz = float(parts[4]) if parts[4] != '-' else 0.0
        
        net_sh = 0.0
        if klines_sh:
            parts = klines_sh[0].split(',')
            if len(parts) >= 5:
                net_sh = float(parts[4]) if parts[4] != '-' else 0.0
        
        return net_sh + net_sz
    
    except Exception:
        return None


# ====== 因子4: 融资融券 ======

def factor_margin(trade_date: str, cache_dir: str = None) -> float:
    """融资融券因子 (值域 ±1.0)
    
    抓取融资余额变化，余额上升=看多，下降=避险
    映射: 变化率(%) * 0.3 → clamp ±1.0
    
    非交易日返回缓存值
    """
    trade_date = _normalize_date(trade_date)
    
    # 非交易日 → 返回缓存
    if _is_weekend(trade_date):
        cached = _get_cached_value(cache_dir, 'margin', trade_date, max_age_hours=24.0)
        if cached is not None:
            return cached
        return 0.0
    
    # 检查新鲜缓存
    cached = _get_cached_value(cache_dir, 'margin', trade_date, max_age_hours=2.0)
    if cached is not None:
        return cached
    
    # 抓取融资余额变化
    change_pct = _fetch_margin_change()
    if change_pct is not None:
        score = np.clip(change_pct * 0.3, -1.0, 1.0)
        _set_cache(cache_dir, 'margin', float(score), f'change={change_pct:.2f}%')
        return float(score)
    
    # 失败 → 旧缓存
    cached = _get_cached_value(cache_dir, 'margin', trade_date, max_age_hours=24.0)
    if cached is not None:
        return cached
    
    return 0.0


def _fetch_margin_change() -> float | None:
    """抓取融资余额变化率(%)
    
    数据源: 东方财富融资融券数据
    API: datacenter.eastmoney.com → 最近2日融资余额对比
    """
    try:
        # 获取最近2条融资余额数据
        url = (
            "https://datacenter.eastmoney.com/securities/api/data/v1/get?"
            "reportName=RPTA_WEB_MARGIN_TRADEHIS&"
            "columns=TRADE_DATE,FIN_BALANCE&"
            "sortColumns=TRADE_DATE&sortTypes=-1&"
            "pageSize=2&pageNumber=1&"
            "source=WEB&client=WEB"
        )
        data = _fetch_url(url, timeout=10)
        if not data:
            return None
        
        j = json.loads(data)
        records = j.get('result', {}).get('data', [])
        if not records or len(records) < 2:
            return None
        
        # FIN_BALANCE单位: 元 → 转为亿
        today_bal = float(records[0].get('FIN_BALANCE', 0)) / 1e8
        yesterday_bal = float(records[1].get('FIN_BALANCE', 0)) / 1e8
        
        if yesterday_bal <= 0:
            return 0.0
        
        change_pct = (today_bal - yesterday_bal) / yesterday_bal * 100
        return float(change_pct)
    
    except Exception:
        return None


# ====== 主接口 ======

def compute_sentiment_factors(duckdb_conn, trade_date: str, cache_dir: str = None) -> dict:
    """计算情绪面4因子（全市场统一值）
    
    Args:
        duckdb_conn: DuckDB连接（daily_adj表）
        trade_date: 交易日期 'YYYY-MM-DD' 或 'YYYYMMDD'
        cache_dir: 缓存目录（默认None不缓存）
    
    Returns:
        {
            'breadth':      float | None,   # 市场宽度 ±2.0
            'limit_ratio':  float | None,   # 涨跌停比 ±1.5
            'northbound':   float | None,   # 北向资金 ±1.5
            'margin':       float | None,   # 融资融券 ±1.0
            'raw': {                        # 原始值
                'breadth_raw':      float,
                'limit_up_count':   int,
                'limit_down_count': int,
                'limit_ratio_raw':  float,
                'northbound_net':   str,
                'margin_change':    str,
            },
            'warnings':     [str],          # 警告信息
            'computed_at':  str,            # 计算时间
        }
    
    Example:
        >>> import duckdb
        >>> conn = duckdb.connect('daily_adj.duckdb', read_only=True)
        >>> factors = compute_sentiment_factors(conn, '20260609', cache_dir='./cache')
        >>> print(factors['breadth'], factors['northbound'])
    """
    trade_date = _normalize_date(trade_date)
    warnings = []
    
    # 默认缓存目录
    if cache_dir is None:
        cache_dir = os.path.join(os.path.dirname(__file__), '..', '.cache')
    
    if cache_dir and not os.path.exists(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
    
    # 因子1: 市场宽度
    breadth = factor_breadth(duckdb_conn, trade_date)
    if breadth is None:
        warnings.append("市场宽度计算失败(DuckDB无数据)")
    
    # 因子2: 涨跌停比
    limit_ratio = factor_limit_ratio(duckdb_conn, trade_date)
    if limit_ratio is None:
        warnings.append("涨跌停比计算失败(DuckDB无数据)")
    
    # 原始数据提取 (用于调试/回测)
    raw = _extract_raw_stats(duckdb_conn, trade_date)
    
    # 因子3: 北向资金
    northbound = factor_northbound(trade_date, cache_dir)
    
    # 因子4: 融资融券
    margin = factor_margin(trade_date, cache_dir)
    
    return {
        'breadth':      breadth,
        'limit_ratio':  limit_ratio,
        'northbound':   northbound,
        'margin':       margin,
        'raw':          raw,
        'warnings':     warnings,
        'computed_at':  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def _extract_raw_stats(conn, trade_date: str) -> dict:
    """提取原始统计数据"""
    trade_date = _normalize_date(trade_date)
    raw = {
        'breadth_raw':      None,
        'limit_up_count':   0,
        'limit_down_count': 0,
        'limit_ratio_raw':  None,
        'northbound_net':   'N/A',
        'margin_change':    'N/A',
    }
    try:
        df = conn.execute(f"""
            SELECT pct_chg
            FROM daily_adj
            WHERE trade_date = '{trade_date}'
              AND ts_code NOT LIKE '%ST%'
              AND ts_code NOT LIKE '%BJ%'
              AND ts_code NOT LIKE '8%'
              AND pct_chg IS NOT NULL
        """).df()
        
        if not df.empty:
            up_count = int((df['pct_chg'] > 0).sum())
            total = len(df)
            raw['breadth_raw'] = round(up_count / total, 4) if total > 0 else None
            raw['limit_up_count'] = int((df['pct_chg'] >= 9.9).sum())
            raw['limit_down_count'] = int((df['pct_chg'] <= -9.9).sum())
            limits = raw['limit_up_count'] + raw['limit_down_count']
            raw['limit_ratio_raw'] = round(raw['limit_up_count'] / limits, 4) if limits > 0 else None
    except Exception:
        pass
    return raw


# ====== 批量/便捷接口 ======

def compute_sentiment_factors_auto(trade_date: str = None, cache_dir: str = None) -> dict | None:
    """自动发现DuckDB + 计算情绪因子（无需手动传conn）
    
    Args:
        trade_date: 交易日期，默认DuckDB最新日期
        cache_dir: 缓存目录
    
    Returns:
        同 compute_sentiment_factors()
    """
    import duckdb
    
    db_path = _discover_duckdb()
    if db_path is None:
        return None
    
    conn = duckdb.connect(db_path, read_only=True)
    try:
        if trade_date is None:
            trade_date = conn.execute('SELECT MAX(trade_date) FROM daily_adj').fetchone()[0]
        
        return compute_sentiment_factors(conn, trade_date, cache_dir)
    finally:
        conn.close()


# ====== 自测入口 ======

if __name__ == '__main__':
    import duckdb
    
    db_path = _discover_duckdb()
    print(f"DB路径: {db_path}")
    
    if db_path is None:
        print("[!] 未找到DuckDB文件")
        exit(1)
    
    conn = duckdb.connect(db_path, read_only=True)
    latest = conn.execute('SELECT MAX(trade_date) FROM daily_adj').fetchone()[0]
    
    cache_dir = os.path.join(os.path.dirname(__file__), '..', '.cache')
    
    print(f"最新交易日: {latest}")
    print(f"缓存目录: {cache_dir}")
    print()
    
    result = compute_sentiment_factors(conn, latest, cache_dir)
    
    print("=" * 55)
    print("  情绪面因子计算结果")
    print("=" * 55)
    
    # 因子输出
    factors_display = [
        ('市场宽度 (breadth)',     result['breadth'],     '±2.0'),
        ('涨跌停比 (limit_ratio)', result['limit_ratio'], '±1.5'),
        ('北向资金 (northbound)',  result['northbound'],  '±1.5'),
        ('融资融券 (margin)',      result['margin'],      '±1.0'),
    ]
    
    total_score = 0.0
    weights = {'breadth': 0.8, 'limit_ratio': 0.5, 'northbound': 0.5, 'margin': 0.3}
    
    for name, value, limit in factors_display:
        status = "✓" if value is not None else "✗"
        val_str = f"{value:+.2f}" if value is not None else "N/A"
        score_part = "N/A"
        key = name.split('(')[1].rstrip(')')
        if value is not None:
            w = weights[key]
            weighted = value * w
            total_score += weighted
            score_part = f"{weighted:+.2f}"
        print(f"  {status} {name:<22s} {val_str:>6s}  [{limit}]  加权={score_part}")
    
    print("-" * 55)
    print(f"  加权总分: {total_score:+.2f}  (维度权重 25%)")
    
    # 原始数据
    print()
    print("原始统计数据:")
    raw = result['raw']
    print(f"  上涨比例: {raw.get('breadth_raw', 'N/A')}")
    print(f"  涨停数: {raw.get('limit_up_count', 0)}  跌停数: {raw.get('limit_down_count', 0)}")
    ratio_raw = raw.get('limit_ratio_raw')
    print(f"  涨跌停比: {ratio_raw:.2f}" if ratio_raw is not None else "  涨跌停比: N/A")
    
    # 警告
    if result['warnings']:
        print()
        print("⚠ 警告:")
        for w in result['warnings']:
            print(f"  - {w}")
    
    print()
    print(f"计算时间: {result['computed_at']}")
    
    conn.close()
    print("完成")
