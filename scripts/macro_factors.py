#!/usr/bin/env python3
"""
macro_factors.py — 宏观面4因子独立模块

美股隔夜(标普500+纳斯达克) + 人民币汇率 + VIX恐慌指数。
全市场统一值，不逐只计算。

因子列表 (架构文档 第四节):
  sp500    (标普500隔夜, 权重0.8, ±2.0)  隔夜涨跌幅% → 直接映射
  nasdaq   (纳斯达克隔夜, 权重0.5, ±1.5)  同上
  fx       (人民币汇率,   权重0.4, ±1.0)  USDCNY变化%, 贬值>0.3%=利空
  vix      (VIX恐慌指数,  权重0.5, ±1.5)  VIX>25恐慌,<15平静

核心接口:
  compute_macro_factors(trade_date, cache_db_path=None) -> dict

缓存策略:
  - SQLite表 us_overnight_cache, 默认路径 data/macro_cache.db
  - 每日9:00刷新, 缓存≤48h可用, 超时返回0(中性)
  - 非交易时段(周末): 自动跳过, 用缓存
  - web_search失败: 用缓存, 无缓存返回0

数据源(≥2个交叉验证):
  - 主源: Yahoo Finance v8 API (免费, 无认证)
  - 辅源: web_search (Google/Bing 搜索结果)
  - 交叉验证: 两源差异>20% → 标记警告, 用均值

依赖: Python 3.10+, 标准库 (urllib + sqlite3 + json)
"""

import os
import sys
import json
import sqlite3
import urllib.request
import urllib.error
import re
import time
from datetime import datetime, timedelta

# ====== 路径 & 常量 ======

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_BASE_DIR)
_DEFAULT_CACHE_DB = os.path.join(_PROJECT_DIR, "data", "macro_cache.db")

# Yahoo Finance v8 chart API 模板
_YAHOO_CHART_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
    "{symbol}?interval=1d&range=5d"
)

# 因子规格: {key: (权重, 值域范围, yahoo_code, 搜索query)}
FACTOR_SPECS = {
    "sp500":  {"weight": 0.8, "range": 2.0, "yahoo": "^GSPC",  "query": "S&P 500 index today"},
    "nasdaq": {"weight": 0.5, "range": 1.5, "yahoo": "^IXIC",  "query": "NASDAQ composite index today"},
    "fx":     {"weight": 0.4, "range": 1.0, "yahoo": "CNY=X",  "query": "USD CNY exchange rate today"},
    "vix":    {"weight": 0.5, "range": 1.5, "yahoo": "^VIX",   "query": "CBOE VIX volatility index today"},
}

# HTTP 请求头
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}

# ====== DB 初始化 ======

def init_cache_db(db_path: str = None):
    """建表 + 迁移, 返回 sqlite3.Connection"""
    path = db_path or _DEFAULT_CACHE_DB
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS us_overnight_cache (
            trade_date   TEXT PRIMARY KEY,
            sp500_chg    REAL,
            nasdaq_chg   REAL,
            vix          REAL,
            usd_cny      REAL,
            usd_cny_prev REAL,
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            source_info  TEXT
        )
    """)
    conn.commit()
    return conn


# ====== 数据获取层 ======

def _http_get_json(url: str, timeout: int = 10) -> dict:
    """HTTP GET → JSON, 带重试"""
    req = urllib.request.Request(url, headers=_HEADERS)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1)


def _fetch_yahoo_change(symbol: str) -> dict | None:
    """
    从Yahoo Finance获取最近2日收盘价, 计算涨跌幅%
    返回: {"chg_pct": 1.23, "close": 5678.90, "prev_close": 5600.00, "date": "2026-06-09"}
    失败返回 None
    """
    try:
        url = _YAHOO_CHART_URL.format(symbol=symbol)
        data = _http_get_json(url, timeout=10)
        result = data["chart"]["result"][0]
        meta = result["meta"]
        quotes = result.get("indicators", {}).get("quote", [{}])[0]
        timestamps = result.get("timestamp", [])

        # 优先用meta中的常规交易时段数据
        current = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")

        if current and prev:
            chg_pct = (current - prev) / prev * 100
            return {
                "chg_pct": round(chg_pct, 4),
                "close": current,
                "prev_close": prev,
                "date": datetime.fromtimestamp(timestamps[-1]).strftime("%Y-%m-%d") if timestamps else "",
            }

        # 回退: 从quote数据中提取
        closes = [q for q in (quotes.get("close") or []) if q is not None]
        if len(closes) >= 2:
            prev, curr = closes[-2], closes[-1]
            chg_pct = (curr - prev) / prev * 100
            return {
                "chg_pct": round(chg_pct, 4),
                "close": curr,
                "prev_close": prev,
                "date": datetime.fromtimestamp(timestamps[-1]).strftime("%Y-%m-%d") if timestamps else "",
            }
        return None
    except Exception as e:
        print(f"[macro_factors] Yahoo {symbol} 获取失败: {e}", file=sys.stderr)
        return None


def _fetch_yahoo_vix() -> dict | None:
    """获取VIX当前值"""
    try:
        url = _YAHOO_CHART_URL.format(symbol="^VIX")
        data = _http_get_json(url, timeout=10)
        result = data["chart"]["result"][0]
        meta = result["meta"]
        vix = meta.get("regularMarketPrice")
        prev = meta.get("previousClose")
        timestamps = result.get("timestamp", [])

        if vix is not None:
            return {
                "vix": vix,
                "prev_vix": prev,
                "date": datetime.fromtimestamp(timestamps[-1]).strftime("%Y-%m-%d") if timestamps else "",
            }
        return None
    except Exception as e:
        print(f"[macro_factors] VIX 获取失败: {e}", file=sys.stderr)
        return None


def _fetch_yahoo_usdcny() -> dict | None:
    """获取USDCNY汇率"""
    result = _fetch_yahoo_change("CNY=X")
    if result is None:
        return None
    # Yahoo Forex 的 price 即汇率本身
    return {
        "rate": result["close"],
        "prev_rate": result["prev_close"],
        "chg_pct": result["chg_pct"],
        "date": result["date"],
    }


# ====== web_search 交叉验证 ======

def _try_hermes_search(query: str) -> str | None:
    """尝试使用Hermes的web_search工具获取摘要文本"""
    try:
        from hermes_tools import web_search
        results = web_search(query, limit=3)
        web_data = results.get("data", {}).get("web", [])
        if web_data:
            # 合并所有搜索结果片段
            snippets = []
            for item in web_data:
                title = item.get("title", "")
                desc = item.get("description", "")
                snippets.append(f"{title}: {desc}")
            return " | ".join(snippets)
    except ImportError:
        pass
    except Exception:
        pass
    return None


def _parse_pct_from_text(text: str, keywords: list[str]) -> float | None:
    """
    从文本中提取百分比变化值
    匹配模式: ±X.XX%, ±X.X%, change of ±X.XX 等
    """
    if not text:
        return None
    # 在关键词附近查找百分比
    for kw in keywords:
        # 找关键词周围的数字
        idx = text.lower().find(kw.lower())
        if idx < 0:
            continue
        window = text[max(0, idx - 60):idx + 80]
        # 匹配 ±数字% 模式
        m = re.search(r'([+-]?\d+\.?\d*)\s*%', window)
        if m:
            return float(m.group(1))
        # 匹配 changed by ±数字 模式
        m = re.search(r'changed?\s*(?:by)?\s*([+-]?\d+\.?\d*)', window, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def _cross_validate_sp500(yahoo_chg_pct: float) -> tuple[float, int, str]:
    """
    用web_search交叉验证标普500变化
    返回: (最终值, 数据源数量, 备注)
    """
    search_text = _try_hermes_search("S&P 500 index change today")
    if not search_text:
        return yahoo_chg_pct, 1, "仅YahooFinance"

    parsed = _parse_pct_from_text(search_text, ["s&p 500", "sp500", "s&p500"])
    if parsed is None:
        return yahoo_chg_pct, 1, "YahooFinance(搜索无匹配数值)"

    diff = abs(yahoo_chg_pct - parsed)
    if diff > max(abs(yahoo_chg_pct) * 0.2, 0.3):
        # 差异>20% 或 >0.3个百分点, 使用均值
        avg = (yahoo_chg_pct + parsed) / 2
        return round(avg, 4), 2, f"交叉验证(差异{diff:.2f}%, 取均值)"
    return yahoo_chg_pct, 2, "YahooFinance+搜索一致"


def _cross_validate_nasdaq(yahoo_chg_pct: float) -> tuple[float, int, str]:
    """同上, NASDAQ版本"""
    search_text = _try_hermes_search("NASDAQ composite change today")
    if not search_text:
        return yahoo_chg_pct, 1, "仅YahooFinance"

    parsed = _parse_pct_from_text(search_text, ["nasdaq", "nasdaq composite"])
    if parsed is None:
        return yahoo_chg_pct, 1, "YahooFinance(搜索无匹配数值)"

    diff = abs(yahoo_chg_pct - parsed)
    if diff > max(abs(yahoo_chg_pct) * 0.2, 0.3):
        avg = (yahoo_chg_pct + parsed) / 2
        return round(avg, 4), 2, f"交叉验证(差异{diff:.2f}%, 取均值)"
    return yahoo_chg_pct, 2, "YahooFinance+搜索一致"


def _cross_validate_usdcny(yahoo_rate: float) -> tuple[float, int, str]:
    """交叉验证USDCNY汇率"""
    search_text = _try_hermes_search("USD CNY exchange rate today")
    if not search_text:
        return yahoo_rate, 1, "仅YahooFinance"

    # 搜索USDCNY数值: 7.xxxx
    m = re.search(r'(?:USD/?CNY|美元.*?人民币).*?(\d\.\d{4})', search_text)
    if not m:
        m = re.search(r'(\d\.\d{4})', search_text)
    if m:
        parsed = float(m.group(1))
        diff_pct = abs(yahoo_rate - parsed) / yahoo_rate * 100
        if diff_pct > 0.3:
            avg = (yahoo_rate + parsed) / 2
            return round(avg, 6), 2, f"交叉验证(差异{diff_pct:.2f}%, 取均值)"
        return yahoo_rate, 2, "YahooFinance+搜索一致"
    return yahoo_rate, 1, "YahooFinance(搜索无匹配数值)"


def _cross_validate_vix(yahoo_vix: float) -> tuple[float, int, str]:
    """交叉验证VIX"""
    search_text = _try_hermes_search("CBOE VIX index level today")
    if not search_text:
        return yahoo_vix, 1, "仅YahooFinance"

    m = re.search(r'(?:VIX|volatility).*?(\d{1,2}\.?\d*)', search_text, re.IGNORECASE)
    if m:
        parsed = float(m.group(1))
        if parsed > 50 and yahoo_vix < 50:
            # 可能匹配到了其他数字
            return yahoo_vix, 1, "YahooFinance(搜索匹配异常)"
        diff = abs(yahoo_vix - parsed)
        if diff > max(yahoo_vix * 0.2, 2.0):
            avg = (yahoo_vix + parsed) / 2
            return round(avg, 2), 2, f"交叉验证(差异{diff:.1f}, 取均值)"
        return yahoo_vix, 2, "YahooFinance+搜索一致"
    return yahoo_vix, 1, "YahooFinance(搜索无匹配数值)"


# ====== 交易日判断 ======

def _is_weekend(date_str: str) -> bool:
    """判断是否为周末"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.weekday() >= 5  # 5=Sat, 6=Sun
    except ValueError:
        return False


def _is_a_stock_trading_date(date_str: str) -> bool:
    """
    判断是否为A股交易日(简化版: 非周末)
    注: 完整版需A股交易日历, 此处仅排除周末
    """
    return not _is_weekend(date_str)


# ====== 缓存层 ======

def _get_cached(conn: sqlite3.Connection, trade_date: str) -> dict | None:
    """从缓存读取, 检查48h过期"""
    row = conn.execute(
        "SELECT * FROM us_overnight_cache WHERE trade_date = ?",
        (trade_date,)
    ).fetchone()
    if not row:
        return None

    # 列名获取 (避免硬编码索引)
    cols = [desc[0] for desc in conn.execute(
        "SELECT * FROM us_overnight_cache LIMIT 0"
    ).description]
    d = dict(zip(cols, row))

    # 检查时效性: updated_at 距今≤48h
    updated = d.get("updated_at", "")
    try:
        updated_dt = datetime.strptime(str(updated)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None  # 无法解析 → 视为过期

    if (datetime.now() - updated_dt) > timedelta(hours=48):
        return None  # 过期

    return d


def _set_cache(conn: sqlite3.Connection, trade_date: str,
               sp500_chg: float, nasdaq_chg: float,
               vix: float, usd_cny: float, usd_cny_prev: float,
               source_info: str):
    """写入/更新缓存(使用UPSERT, SQLite 3.24+)"""
    conn.execute("""
        INSERT INTO us_overnight_cache
            (trade_date, sp500_chg, nasdaq_chg, vix,
             usd_cny, usd_cny_prev, updated_at, source_info)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'), ?)
        ON CONFLICT(trade_date) DO UPDATE SET
            sp500_chg = excluded.sp500_chg,
            nasdaq_chg = excluded.nasdaq_chg,
            vix = excluded.vix,
            usd_cny = excluded.usd_cny,
            usd_cny_prev = excluded.usd_cny_prev,
            updated_at = datetime('now', 'localtime'),
            source_info = excluded.source_info
    """, (trade_date, sp500_chg, nasdaq_chg, vix,
          usd_cny, usd_cny_prev, source_info))
    conn.commit()


# ====== 因子计算 ======

def _compute_sp500_factor(chg_pct: float) -> float:
    """标普500变化 → 因子分数, 钳位±2.0"""
    return max(-2.0, min(2.0, chg_pct))


def _compute_nasdaq_factor(chg_pct: float) -> float:
    """纳斯达克变化 → 因子分数, 钳位±1.5"""
    return max(-1.5, min(1.5, chg_pct))


def _compute_fx_factor(usd_cny_chg_pct: float) -> float:
    """
    USDCNY变化% → 因子分数, 钳位±1.0
    贬值(delta>0) → 利空A股 → 负分
    升值(delta<0) → 利多A股 → 正分
    阈值: ±0.3%对应±1.0分
    """
    # chg_pct 是小数, 如 0.003 = 0.3%
    score = -usd_cny_chg_pct / 0.003  # 0.3% → -1.0
    return max(-1.0, min(1.0, score))


def _compute_vix_factor(vix: float) -> float:
    """
    VIX → 因子分数, 钳位±1.5
    VIX<15:平静→正分, VIX=20:中性→0, VIX>25:恐慌→负分
    线性映射: score = 1.5 * (20 - VIX) / 15
    """
    score = 1.5 * (20 - vix) / 15
    return max(-1.5, min(1.5, score))


# ====== 主接口 ======

def compute_macro_factors(trade_date: str,
                          cache_db_path: str = None) -> dict:
    """
    计算宏观面4因子 — 全市场统一值

    Args:
        trade_date: A股交易日 'YYYY-MM-DD'
        cache_db_path: 缓存DB路径, 默认 data/macro_cache.db

    Returns:
        {"sp500": 0.8, "nasdaq": 1.2, "fx": -0.3, "vix": -0.5,
         "_meta": {"sources": 2, "cache_hit": false, "warnings": []}}
    """
    db_path = cache_db_path or _DEFAULT_CACHE_DB
    conn = init_cache_db(db_path)
    meta = {"sources": 0, "cache_hit": False, "warnings": [], "source_info": ""}

    # ---- 1. 非交易日: 返回中性 ----
    if not _is_a_stock_trading_date(trade_date):
        meta["warnings"].append(f"{trade_date} 非A股交易日, 返回中性")
        return {"sp500": 0.0, "nasdaq": 0.0, "fx": 0.0, "vix": 0.0, "_meta": meta}

    # ---- 2. 尝试缓存 ----
    cached = _get_cached(conn, trade_date)
    if cached and cached.get("sp500_chg") is not None:
        meta["cache_hit"] = True
        meta["source_info"] = cached.get("source_info", "缓存")
        sp500 = _compute_sp500_factor(cached["sp500_chg"])
        nasdaq = _compute_nasdaq_factor(cached["nasdaq_chg"] or 0)
        # 从缓存的usd_cny/usd_cny_prev计算变化率
        _usd = cached.get("usd_cny") or 7.2
        _usd_prev = cached.get("usd_cny_prev") or _usd
        _fx_chg = (_usd - _usd_prev) / _usd_prev if _usd_prev else 0
        fx = _compute_fx_factor(_fx_chg)
        vix = _compute_vix_factor(cached["vix"] or 20)
        conn.close()
        return {"sp500": round(sp500, 4), "nasdaq": round(nasdaq, 4),
                "fx": round(fx, 4), "vix": round(vix, 4), "_meta": meta}

    # ---- 3. 实时获取数据 ----
    # 3a. 标普500
    sp500_data = _fetch_yahoo_change("^GSPC")
    if sp500_data is None:
        # 回退到缓存(可能过期但仍在48h内)
        if cached:
            meta["warnings"].append("SP500获取失败, 用旧缓存")
            sp500_chg = cached.get("sp500_chg", 0)
        else:
            meta["warnings"].append("SP500获取失败, 无缓存, 返回0")
            sp500_chg = 0.0
    else:
        yahoo_chg = sp500_data["chg_pct"]
        sp500_chg, n_src, note = _cross_validate_sp500(yahoo_chg)
        meta["sources"] = max(meta["sources"], n_src)
        if n_src == 1:
            meta["warnings"].append(f"SP500: {note}")

    # 3b. 纳斯达克
    nasdaq_data = _fetch_yahoo_change("^IXIC")
    if nasdaq_data is None:
        if cached:
            meta["warnings"].append("NASDAQ获取失败, 用旧缓存")
            nasdaq_chg = cached.get("nasdaq_chg", 0)
        else:
            meta["warnings"].append("NASDAQ获取失败, 无缓存, 返回0")
            nasdaq_chg = 0.0
    else:
        yahoo_chg = nasdaq_data["chg_pct"]
        nasdaq_chg, n_src, note = _cross_validate_nasdaq(yahoo_chg)
        meta["sources"] = max(meta["sources"], n_src)
        if n_src == 1:
            meta["warnings"].append(f"NASDAQ: {note}")

    # 3c. USDCNY
    fx_data = _fetch_yahoo_usdcny()
    if fx_data is None:
        if cached:
            meta["warnings"].append("USDCNY获取失败, 用旧缓存")
            usd_cny_rate = cached.get("usd_cny", 7.2)
            usd_cny_prev = cached.get("usd_cny_prev", 7.2)
        else:
            meta["warnings"].append("USDCNY获取失败, 用默认值7.2")
            usd_cny_rate = 7.2
            usd_cny_prev = 7.2
    else:
        yahoo_rate = fx_data["rate"]
        usd_cny_rate, _, _ = _cross_validate_usdcny(yahoo_rate)
        usd_cny_prev = fx_data["prev_rate"]
        if usd_cny_prev is None or usd_cny_prev == 0:
            usd_cny_prev = usd_cny_rate
    usd_cny_chg_pct = (usd_cny_rate - usd_cny_prev) / usd_cny_prev if usd_cny_prev else 0

    # 3d. VIX
    vix_data = _fetch_yahoo_vix()
    if vix_data is None:
        if cached:
            meta["warnings"].append("VIX获取失败, 用旧缓存")
            vix_val = cached.get("vix", 20)
        else:
            meta["warnings"].append("VIX获取失败, 用默认值20")
            vix_val = 20.0
    else:
        yahoo_vix = vix_data["vix"]
        vix_val, _, _ = _cross_validate_vix(yahoo_vix)

    # ---- 4. 写入缓存 ----
    source_info = f"YahooFinance + {'搜索' if meta['sources'] >= 2 else '单源'}"
    _set_cache(conn, trade_date,
               sp500_chg, nasdaq_chg, vix_val,
               usd_cny_rate, usd_cny_prev, source_info)

    # ---- 5. 计算因子分 ----
    sp500 = _compute_sp500_factor(sp500_chg)
    nasdaq = _compute_nasdaq_factor(nasdaq_chg)
    fx = _compute_fx_factor(usd_cny_chg_pct)
    vix = _compute_vix_factor(vix_val)

    conn.close()

    return {
        "sp500": round(sp500, 4),
        "nasdaq": round(nasdaq, 4),
        "fx": round(fx, 4),
        "vix": round(vix, 4),
        "_meta": meta,
    }


# ====== 批量/便捷接口 ======

def compute_macro_factors_batch(trade_dates: list[str],
                                 cache_db_path: str = None) -> list[dict]:
    """批量计算多日宏观因子"""
    return [compute_macro_factors(d, cache_db_path) for d in trade_dates]


def get_raw_data(trade_date: str, cache_db_path: str = None) -> dict:
    """
    获取原始宏观数据(未经因子映射), 用于调试/回测
    返回: {sp500_chg, nasdaq_chg, vix, usd_cny, usd_cny_chg_pct}
    """
    result = compute_macro_factors(trade_date, cache_db_path)
    meta = result.pop("_meta", {})
    # 从缓存/数据库重建原始值
    db_path = cache_db_path or _DEFAULT_CACHE_DB
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT * FROM us_overnight_cache WHERE trade_date = ?",
        (trade_date,)
    ).fetchone()
    conn.close()

    if row:
        cols = ["trade_date", "sp500_chg", "nasdaq_chg", "vix",
                "usd_cny", "usd_cny_prev", "updated_at", "source_info"]
        d = dict(zip(cols, row))
        if d.get("usd_cny") and d.get("usd_cny_prev"):
            d["usd_cny_chg_pct"] = (d["usd_cny"] - d["usd_cny_prev"]) / d["usd_cny_prev"]
        else:
            d["usd_cny_chg_pct"] = 0.0
        d["_meta"] = meta
        return d
    return {"_meta": meta, "error": "无缓存数据"}


def reset_cache(trade_date: str, cache_db_path: str = None):
    """清除指定日期的缓存, 强制重新获取"""
    db_path = cache_db_path or _DEFAULT_CACHE_DB
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM us_overnight_cache WHERE trade_date = ?", (trade_date,))
    conn.commit()
    conn.close()


# ====== 自检/CLI入口 ======

if __name__ == "__main__":
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== 宏观面因子计算: {today} ===\n")

    result = compute_macro_factors(today)
    meta = result.pop("_meta", {})

    print(f"标普500因子:  {result['sp500']:+.4f}  (范围 ±2.0)")
    print(f"纳斯达克因子: {result['nasdaq']:+.4f}  (范围 ±1.5)")
    print(f"汇率因子:     {result['fx']:+.4f}  (范围 ±1.0)")
    print(f"VIX因子:      {result['vix']:+.4f}  (范围 ±1.5)")
    print(f"\n--- 元数据 ---")
    print(f"缓存命中: {meta.get('cache_hit')}")
    print(f"数据源数: {meta.get('sources')}")
    print(f"警告:     {meta.get('warnings')}")
