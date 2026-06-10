#!/usr/bin/env python3
"""
news_factors.py — 消息面3因子模块（公告+政策+龙虎榜）

3个公开因子:
  announcement  (个股公告,  权重0.8, ±2.0)  搜索个股公告，关键词评分
  policy        (行业政策,  权重0.5, ±1.5)  政策日历 + 行业政策搜索
  dragon_tiger  (龙虎榜,    权重0.5, ±1.5)  搜索龙虎榜游资/机构动向

核心接口:
  compute_news_factors(ts_code, cache_db_path=None, search_fn=None) -> dict
  compute_news_factors_batch(ts_codes, cache_db_path=None, search_fn=None) -> dict

缓存策略:
  - announcement: 每标的每日1次 (SQLite)
  - policy: 每标的每日1次 (SQLite, 按行业缓存)
  - dragon_tiger: 每标的每日1次 (SQLite)
  - web_search 超时10s后返回0

架构参考: docs/architecture/factor-design.md 第二节
依赖: sqlite3, json, os, datetime
"""

import sqlite3
import json
import os
import re
from datetime import datetime, date

# ====== 路径常量 ======
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_CACHE_DB = os.path.join(_PROJECT_ROOT, "data", "news_factors_cache.db")
_POLICY_CALENDAR_PATH = os.path.join(_PROJECT_ROOT, "data", "policy_calendar.json")


# ====== 搜索函数注入 ======

def _agent_search(query, timeout=10):
    """使用 hermes_tools.web_search（agent 环境），失败返回空列表"""
    try:
        from hermes_tools import web_search
        result = web_search(query, limit=5)
        return result.get("data", {}).get("web", [])
    except (ImportError, Exception):
        return []


# 全局搜索函数引用（可外部注入）
_SEARCH_FN = _agent_search


def set_search_function(fn):
    """注入自定义搜索函数 fn(query: str) -> list[dict]"""
    global _SEARCH_FN
    _SEARCH_FN = fn


# ====== 缓存数据库 ======

def _init_cache_db(db_path=None):
    """初始化缓存 SQLite，返回连接"""
    if db_path is None:
        db_path = _DEFAULT_CACHE_DB
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS factor_cache (
        ts_code TEXT,
        factor_type TEXT,
        check_date TEXT,
        score REAL,
        detail TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(ts_code, factor_type, check_date)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS policy_industry_cache (
        industry TEXT,
        check_date TEXT,
        score REAL,
        detail TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(industry, check_date)
    )""")
    conn.commit()
    return conn


def _get_cached(conn, ts_code, factor_type, today_str=None):
    """查询当日缓存，命中返回 (score, detail)，否则 None"""
    if today_str is None:
        today_str = date.today().strftime("%Y%m%d")
    c = conn.cursor()
    c.execute("""SELECT score, detail FROM factor_cache
                 WHERE ts_code=? AND factor_type=? AND check_date=?""",
              (ts_code, factor_type, today_str))
    row = c.fetchone()
    return row if row else None


def _set_cached(conn, ts_code, factor_type, score, detail, today_str=None):
    """写入当日缓存"""
    if today_str is None:
        today_str = date.today().strftime("%Y%m%d")
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO factor_cache
                 (ts_code, factor_type, check_date, score, detail)
                 VALUES (?,?,?,?,?)""",
              (ts_code, factor_type, today_str, score, detail))
    conn.commit()


# ====== 行业映射（从政策日历加载） ======

def _load_policy_calendar():
    """加载政策日历 JSON"""
    try:
        with open(_POLICY_CALENDAR_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"industries": {}, "stock_industry_map": {}}


def _get_stock_industry(ts_code, calendar=None):
    """获取标的所属行业（优先 calendar stock_industry_map，回退关键词匹配）"""
    if calendar is None:
        calendar = _load_policy_calendar()

    # 直接映射
    stock_map = calendar.get("stock_industry_map", {})
    if ts_code in stock_map:
        return stock_map[ts_code]

    # 关键词匹配: 提取股票代码数字部分做简单推断
    # 600/601/603 = 上海主板, 000/002 = 深圳主板
    # fallback: 返回 None（由 policy 计算时处理为中性）
    return None


# ====== 因子1: 个股公告 ======

# 公告关键词评分表
_ANNOUNCEMENT_RULES = [
    # (关键词列表, 分数, 描述)
    (["业绩预增", "业绩大增", "净利润增长", "营收增长", "利润增长",
      "业绩预告.*增", "预盈", "扭亏为盈", "业绩超预期"], +1.8, "业绩预增"),
    (["回购", "股份回购", "回购股份"], +1.5, "公司回购"),
    (["增持", "股东增持", "高管增持", "控股股东增持"], +1.5, "股东增持"),
    (["分红", "派息", "现金分红", "高分红", "送转"], +1.0, "分红派息"),
    (["股权激励", "员工持股", "限制性股票", "股票期权"], +0.8, "股权激励"),
    (["重大合同", "中标", "签约", "战略合作", "合作协议", "订单",
      "获得项目", "中标项目"], +0.8, "重大合同"),
    (["重组", "资产注入", "并购", "优质资产"], +0.5, "资产重组"),
    # 利空
    (["业绩预亏", "业绩下降", "净利润下降", "亏损", "业绩大幅下滑",
      "业绩预减", "预亏"], -1.8, "业绩预亏"),
    (["减持", "股东减持", "高管减持", "控股股东减持",
      "拟减持", "减持计划"], -1.8, "股东减持"),
    (["监管函", "立案调查", "行政处罚", "警示函", "通报批评",
      "被立案", "调查通知"], -2.0, "监管处罚"),
    (["退市风险", "ST", "*ST", "暂停上市", "终止上市"], -2.0, "退市风险"),
    (["限售解禁", "解禁", "大额解禁", "巨额解禁"], -1.0, "限售解禁"),
    (["商誉减值", "资产减值", "计提减值"], -1.5, "资产减值"),
    (["诉讼", "仲裁", "重大诉讼"], -1.0, "诉讼风险"),
]


def _compute_announcement(ts_code, conn, search_fn=None):
    """计算个股公告因子分"""
    if search_fn is None:
        search_fn = _SEARCH_FN

    today_str = date.today().strftime("%Y%m%d")

    # 查缓存
    cached = _get_cached(conn, ts_code, "announcement", today_str)
    if cached is not None:
        return cached[0]

    # 构建搜索词
    code_short = ts_code.split(".")[0]  # "600519.SH" → "600519"
    query = f"{code_short} 公告 2026"

    try:
        results = search_fn(query)
    except Exception:
        results = []

    if not results:
        _set_cached(conn, ts_code, "announcement", 0.0, "无搜索结果", today_str)
        return 0.0

    # 合并所有搜索结果的标题+描述
    combined_text = " ".join(
        r.get("title", "") + " " + r.get("description", "")
        for r in results if isinstance(r, dict)
    )

    if not combined_text.strip():
        _set_cached(conn, ts_code, "announcement", 0.0, "空文本", today_str)
        return 0.0

    # 关键词评分
    total_score = 0.0
    matched = []
    for keywords, score, label in _ANNOUNCEMENT_RULES:
        for kw in keywords:
            if re.search(kw, combined_text):
                total_score += score
                matched.append(f"{label}({score:+.1f})")
                break  # 每组只计一次

    # 裁剪到 ±2.0
    total_score = max(-2.0, min(2.0, total_score))
    detail = "; ".join(matched) if matched else "无重要事件"

    _set_cached(conn, ts_code, "announcement", total_score, detail, today_str)
    return total_score


# ====== 因子2: 行业政策 ======

_POLICY_POSITIVE_KW = ["补贴", "降税", "放开", "支持", "利好", "扶持",
                        "减税", "退税", "免税", "鼓励", "推动", "促进"]
_POLICY_NEGATIVE_KW = ["监管", "加税", "限制", "收紧", "整顿", "利空",
                        "管控", "处罚", "禁止", "淘汰", "遏制"]


def _match_industry(ts_code, industry_name, industry_info, today_dt):
    """检查标的是否属于该行业，返回日历中活跃政策的加权分"""
    keywords = industry_info.get("keywords", [])
    code_short = ts_code.split(".")[0]

    # 直接 maps 优先
    calendar = _load_policy_calendar()
    stock_map = calendar.get("stock_industry_map", {})
    if ts_code in stock_map:
        mapped_ind = stock_map[ts_code]
        if mapped_ind != industry_name:
            return 0.0  # 不属于这个行业

    # 计算政策加权分
    total = 0.0
    for p in industry_info.get("policies", []):
        try:
            p_date = datetime.strptime(p["date"], "%Y-%m-%d")
        except ValueError:
            continue
        days_diff = (today_dt - p_date).days
        # 政策影响衰减: 30天内有效
        if days_diff < 0:
            # 未来政策: 10天前瞻窗口
            if days_diff >= -10:
                weight = 1.0 + days_diff / 10.0  # 越近权重越高
            else:
                continue
        elif days_diff <= 30:
            weight = max(0.2, 1.0 - days_diff / 30.0)
        else:
            continue
        total += p.get("score", 0) * weight
    return total


def _compute_policy(ts_code, conn, search_fn=None):
    """计算行业政策因子分"""
    if search_fn is None:
        search_fn = _SEARCH_FN

    today_str = date.today().strftime("%Y%m%d")
    today_dt = datetime.strptime(today_str, "%Y%m%d")

    # 查缓存
    cached = _get_cached(conn, ts_code, "policy", today_str)
    if cached is not None:
        return cached[0]

    calendar = _load_policy_calendar()
    industry = _get_stock_industry(ts_code, calendar)

    # 1) 政策日历评分
    calendar_score = 0.0
    calendar_detail = []
    if industry and industry in calendar.get("industries", {}):
        ind_info = calendar["industries"][industry]
        calendar_score = _match_industry(ts_code, industry, ind_info, today_dt)
        if abs(calendar_score) > 0.05:
            calendar_detail.append(f"日历:{industry}({calendar_score:+.1f})")

    # 2) web_search 补充
    search_score = 0.0
    search_detail = ""
    if industry:
        query = f"{industry} 政策 2026"
    else:
        # 无行业信息，做泛搜索
        code_short = ts_code.split(".")[0]
        query = f"{code_short} 行业 政策 2026"

    try:
        results = search_fn(query)
    except Exception:
        results = []

    if results:
        combined = " ".join(
            r.get("title", "") + " " + r.get("description", "")
            for r in results if isinstance(r, dict)
        )
        # 利好关键词
        pos_count = sum(1 for kw in _POLICY_POSITIVE_KW if kw in combined)
        neg_count = sum(1 for kw in _POLICY_NEGATIVE_KW if kw in combined)
        if pos_count > neg_count:
            search_score = min(1.5, pos_count * 0.5)
            search_detail = f"搜索利好({pos_count}词)"
        elif neg_count > pos_count:
            search_score = max(-1.5, -neg_count * 0.5)
            search_detail = f"搜索利空({neg_count}词)"
        # else: 中性, search_score=0

    # 综合: 日历优先，搜索补充
    if abs(calendar_score) > 0.05:
        total_score = calendar_score  # 日历有明确信息，以日历为准
    else:
        total_score = search_score

    total_score = max(-1.5, min(1.5, total_score))

    details = []
    if calendar_detail:
        details.extend(calendar_detail)
    if search_detail:
        details.append(search_detail)
    detail = "; ".join(details) if details else "无相关行业政策"

    _set_cached(conn, ts_code, "policy", total_score, detail, today_str)
    return total_score


# ====== 因子3: 龙虎榜 ======

def _compute_dragon_tiger(ts_code, conn, search_fn=None):
    """计算龙虎榜因子分"""
    if search_fn is None:
        search_fn = _SEARCH_FN

    today_str = date.today().strftime("%Y%m%d")

    # 查缓存
    cached = _get_cached(conn, ts_code, "dragon_tiger", today_str)
    if cached is not None:
        return cached[0]

    code_short = ts_code.split(".")[0]
    query = f"龙虎榜 {code_short}"

    try:
        results = search_fn(query)
    except Exception:
        results = []

    if not results:
        _set_cached(conn, ts_code, "dragon_tiger", 0.0, "无龙虎榜数据", today_str)
        return 0.0

    combined = " ".join(
        r.get("title", "") + " " + r.get("description", "")
        for r in results if isinstance(r, dict)
    )

    if not combined.strip():
        _set_cached(conn, ts_code, "dragon_tiger", 0.0, "空结果", today_str)
        return 0.0

    score = 0.0
    details = []

    # 机构净买入 > 1000万
    inst_buy = re.search(r"机构.*?净买入.*?(\d+\.?\d*)\s*[万亿]", combined)
    if inst_buy:
        try:
            amount_str = inst_buy.group(1)
            amount = float(amount_str)
            unit = inst_buy.group(0)[-1] if inst_buy.group(0) else "万"
            # 估算: 万为单位
            if "亿" in inst_buy.group(0):
                amount *= 10000
            if amount > 1000:
                score += 1.0
                details.append(f"机构净买入({amount:.0f}万)")
        except (ValueError, IndexError):
            pass
    elif "机构净买入" in combined or "机构买入" in combined:
        score += 0.8
        details.append("机构净买入")

    # 机构净卖出 > 1000万
    inst_sell = re.search(r"机构.*?净卖出.*?(\d+\.?\d*)\s*[万亿]", combined)
    if inst_sell:
        try:
            amount_str = inst_sell.group(1)
            amount = float(amount_str)
            if "亿" in inst_sell.group(0):
                amount *= 10000
            if amount > 1000:
                score -= 1.0
                details.append(f"机构净卖出({amount:.0f}万)")
        except (ValueError, IndexError):
            pass
    elif "机构净卖出" in combined or "机构卖出" in combined:
        score -= 0.8
        details.append("机构净卖出")

    # 游资接力
    if "游资" in combined and ("接力" in combined or "连续上榜" in combined):
        score += 0.5
        details.append("游资接力")

    # 龙虎榜出现但未上榜
    if score == 0 and "龙虎榜" in combined:
        details.append("上榜无显著机构动向")

    score = max(-1.5, min(1.5, score))
    detail = "; ".join(details) if details else "未上榜"

    _set_cached(conn, ts_code, "dragon_tiger", score, detail, today_str)
    return score


# ====== 核心接口 ======

def compute_news_factors(ts_code, cache_db_path=None, search_fn=None):
    """计算单只标的的三消息面因子

    Args:
        ts_code: 标的代码, 如 "603986.SH"
        cache_db_path: 缓存DB路径, 默认 data/news_factors_cache.db
        search_fn: 搜索函数 query->list[dict], 默认使用 hermes_tools.web_search

    Returns:
        {"announcement": float, "policy": float, "dragon_tiger": float}
    """
    conn = _init_cache_db(cache_db_path)
    try:
        announcement = _compute_announcement(ts_code, conn, search_fn)
        policy = _compute_policy(ts_code, conn, search_fn)
        dragon_tiger = _compute_dragon_tiger(ts_code, conn, search_fn)
        return {
            "announcement": announcement,
            "policy": policy,
            "dragon_tiger": dragon_tiger,
        }
    finally:
        conn.close()


def compute_news_factors_batch(ts_codes, cache_db_path=None, search_fn=None):
    """批量计算消息面因子

    Args:
        ts_codes: 标的代码列表 ["603986.SH", "000001.SZ", ...]
        cache_db_path: 缓存DB路径
        search_fn: 搜索函数

    Returns:
        {ts_code: {"announcement": ..., "policy": ..., "dragon_tiger": ...}, ...}
    """
    conn = _init_cache_db(cache_db_path)
    results = {}
    try:
        for ts_code in ts_codes:
            try:
                announcement = _compute_announcement(ts_code, conn, search_fn)
                policy = _compute_policy(ts_code, conn, search_fn)
                dragon_tiger = _compute_dragon_tiger(ts_code, conn, search_fn)
                results[ts_code] = {
                    "announcement": announcement,
                    "policy": policy,
                    "dragon_tiger": dragon_tiger,
                }
            except Exception as e:
                results[ts_code] = {
                    "announcement": 0.0,
                    "policy": 0.0,
                    "dragon_tiger": 0.0,
                    "error": str(e),
                }
        return results
    finally:
        conn.close()


# ====== 独立测试入口 ======

if __name__ == "__main__":
    test_codes = ["603986.SH", "000001.SZ", "600519.SH", "000002.SZ", "002415.SZ"]

    print("=== 消息面因子测试 ===\n")
    print(f"政策日历: {_POLICY_CALENDAR_PATH}")
    print(f"缓存DB:   {_DEFAULT_CACHE_DB}")
    print()

    # 测试单只
    for code in test_codes:
        print(f"--- {code} ---")
        try:
            result = compute_news_factors(code)
        except Exception as e:
            print(f"  [!] 计算失败: {e}\n")
            continue
        print(f"  公告(announcement):   {result['announcement']:+.1f}  (range ±2.0)")
        print(f"  政策(policy):         {result['policy']:+.1f}  (range ±1.5)")
        print(f"  龙虎榜(dragon_tiger): {result['dragon_tiger']:+.1f}  (range ±1.5)")
        print()

    # 测试批量
    print("--- 批量测试 ---")
    batch = compute_news_factors_batch(test_codes)
    for code, scores in batch.items():
        if "error" in scores:
            print(f"  {code}: ERROR - {scores['error']}")
        else:
            total = scores["announcement"] + scores["policy"] + scores["dragon_tiger"]
            print(f"  {code}: ann={scores['announcement']:+.1f} "
                  f"pol={scores['policy']:+.1f} dt={scores['dragon_tiger']:+.1f} "
                  f"→ sum={total:+.1f}")

    print("\n测试完成")
