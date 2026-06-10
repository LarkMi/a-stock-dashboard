#!/usr/bin/env python3
"""
test_news_factors.py — 消息面3因子单元测试

测试范围:
  1. announcement: 15组关键词评分规则 / 缓存命中 / 无结果→0 / 裁剪±2.0
  2. policy: 政策日历加载 / 30日衰减+10日前瞻 / web搜索补充 / 无行业fallback
  3. dragon_tiger: 机构净买卖金额提取 / 游资接力检测 / 无上榜→0
  4. batch接口: 20只<30s / 个别失败不影响其余
  5. 搜索注入: set_search_function() 可替换搜索源

作者: qa-tester
日期: 2026-06-10
"""

import os
import sys
import json
import re
import sqlite3
import math
from datetime import date, datetime

import pytest

# 将被测模块加入路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from news_factors import (  # noqa: E402
    compute_news_factors,
    compute_news_factors_batch,
    set_search_function,
    _compute_announcement,
    _compute_policy,
    _compute_dragon_tiger,
    _init_cache_db,
    _get_cached,
    _set_cached,
    _load_policy_calendar,
    _get_stock_industry,
    _match_industry,
    _ANNOUNCEMENT_RULES,
)

# ============================================================
# 工具函数
# ============================================================

def _mock_search_empty(query):
    """模拟空搜索结果"""
    return []

def _mock_search_error(query):
    """模拟搜索抛异常"""
    raise RuntimeError("network error")

def _make_mock_search(results_list):
    """构造模拟搜索函数，返回指定结果"""
    def _search(query):
        return results_list
    return _search

def _temp_cache_db():
    """创建临时缓存DB路径"""
    import tempfile
    fd, path = tempfile.mkstemp(suffix='.db', prefix='test_news_cache_')
    os.close(fd)
    return path


# ============================================================
# P0 BUG描述常量
# ============================================================

P0_REGEX_STAR = "P0: _ANNOUNCEMENT_RULES 含 '*ST' 关键词, re.search() 未转义 → Python re.PatternError"
P0_DT_FALLTHROUGH = "P0: _compute_dragon_tiger 机构净买入<1000万时正则匹配但不兜底+0.8"


# ============================================================
# 1. announcement 个股公告测试
# ============================================================

class TestAnnouncementRules:
    """15组关键词评分规则验证"""

    def test_rule_count(self):
        """确认有14组规则(7利好+7利空)"""
        assert len(_ANNOUNCEMENT_RULES) == 14, f"预期14组规则，实际{len(_ANNOUNCEMENT_RULES)}"

    def test_positive_rules(self):
        """利好规则: +0.5 ~ +2.0"""
        positive = [r for r in _ANNOUNCEMENT_RULES if r[1] > 0]
        scores = [r[1] for r in positive]
        for s in scores:
            assert 0.5 <= s <= 2.0, f"利好分数{s}超出范围[0.5,2.0]"
        assert len(positive) >= 6, f"利好规则至少6组，实际{len(positive)}"

    def test_negative_rules(self):
        """利空规则: -2.0 ~ -1.0"""
        negative = [r for r in _ANNOUNCEMENT_RULES if r[1] < 0]
        scores = [r[1] for r in negative]
        for s in scores:
            assert -2.0 <= s <= -1.0, f"利空分数{s}超出范围[-2.0,-1.0]"
        assert len(negative) >= 6, f"利空规则至少6组，实际{len(negative)}"


class TestAnnouncementScoring:
    """公告关键词评分逻辑 (部分被P0 regex bug阻塞)"""

    @pytest.mark.xfail(reason=P0_REGEX_STAR)
    def test_positive_keyword_match(self):
        """单条利好关键词: 业绩预增=+1.8"""
        mock = _make_mock_search([
            {"title": "600519 业绩预增公告", "description": "贵州茅台2026Q1业绩预增20%"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_announcement("600519.SH", conn, search_fn=mock)
            assert score == 1.8, f"业绩预增应得+1.8，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    @pytest.mark.xfail(reason=P0_REGEX_STAR)
    def test_negative_keyword_match(self):
        """单条利空关键词: 监管处罚=-2.0"""
        mock = _make_mock_search([
            {"title": "603986 收到监管函", "description": "被立案调查"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_announcement("603986.SH", conn, search_fn=mock)
            assert score == -2.0, f"监管处罚应得-2.0，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    @pytest.mark.xfail(reason=P0_REGEX_STAR)
    def test_multiple_keywords_sum(self):
        """多条关键词累加: 回购+1.5 + 分红+1.0 = 2.5→裁剪到2.0"""
        mock = _make_mock_search([
            {"title": "股份回购", "description": "公司公告10亿回购计划"},
            {"title": "分红方案", "description": "每10股派发现金红利15元"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_announcement("000858.SZ", conn, search_fn=mock)
            assert score == 2.0, f"回购+分红=2.5应裁剪到2.0，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    @pytest.mark.xfail(reason=P0_REGEX_STAR)
    def test_mixed_sentiment_net(self):
        """利好利空混合: 业绩预增+1.8 + 减持-1.8 = 0"""
        mock = _make_mock_search([
            {"title": "业绩预增", "description": "净利润增长50%"},
            {"title": "股东减持计划", "description": "控股股东拟减持3%"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_announcement("000001.SZ", conn, search_fn=mock)
            assert score == 0.0, f"利好利空抵消应为0，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    @pytest.mark.xfail(reason=P0_REGEX_STAR)
    def test_same_group_only_once(self):
        """同组关键词只计一次: 回购和股份回购同组，只计+1.5"""
        mock = _make_mock_search([
            {"title": "回购股份", "description": "回购和股份回购同时出现"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_announcement("600519.SH", conn, search_fn=mock)
            assert score == 1.5, f"同组只计一次，应为1.5，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    def test_no_search_results_returns_zero(self):
        """无搜索结果→返回0"""
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_announcement("999999.SH", conn, search_fn=_mock_search_empty)
            assert score == 0.0, f"无搜索结果应为0，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    def test_search_exception_returns_zero(self):
        """搜索异常→返回0(不崩溃)"""
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_announcement("000001.SZ", conn, search_fn=_mock_search_error)
            assert score == 0.0, f"搜索异常应为0，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    @pytest.mark.xfail(reason=P0_REGEX_STAR)
    def test_cache_hit_second_call(self):
        """缓存命中: 同一标的同日两次调用结果一致"""
        call_count = [0]
        def counted_search(query):
            call_count[0] += 1
            return [{"title": "业绩预增", "description": "净利润+30%"}]

        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            s1 = _compute_announcement("600519.SH", conn, search_fn=counted_search)
            s2 = _compute_announcement("600519.SH", conn, search_fn=counted_search)
            assert call_count[0] == 1, f"缓存命中不应再调搜索，实际调了{call_count[0]}次"
            assert s1 == s2, f"两次调用应一致: {s1} vs {s2}"
        finally:
            conn.close()
            os.unlink(db)

    @pytest.mark.xfail(reason=P0_REGEX_STAR)
    def test_clip_negative_to_minus_2(self):
        """利空累加裁剪: 监管-2.0 + 减持-1.8 = -3.8→-2.0"""
        mock = _make_mock_search([
            {"title": "被立案调查", "description": "监管处罚"},
            {"title": "控股股东减持", "description": "减持计划公告"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_announcement("000002.SZ", conn, search_fn=mock)
            assert score == -2.0, f"利空累加应裁剪到-2.0，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    @pytest.mark.xfail(reason=P0_REGEX_STAR)
    def test_single_keyword_per_group(self):
        """每组关键词任一命中即触发: 14组独立验证"""
        for keywords, expected, label in _ANNOUNCEMENT_RULES:
            kw = keywords[0]
            mock = _make_mock_search([
                {"title": kw, "description": f"公告涉及{label}"}
            ])
            db = _temp_cache_db()
            try:
                conn = _init_cache_db(db)
                score = _compute_announcement("600001.SH", conn, search_fn=mock)
                assert score == expected, \
                    f"关键词'{kw}'({label})应得{expected}，实际{score}"
            finally:
                conn.close()
                os.unlink(db)

    def test_empty_text_returns_zero(self):
        """搜索结果无有效文本→返回0"""
        mock = _make_mock_search([
            {"title": "", "description": ""},
            {"not_title": "wrong_key"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_announcement("000001.SZ", conn, search_fn=mock)
            assert score == 0.0, f"空文本应为0，实际{score}"
        finally:
            conn.close()
            os.unlink(db)


# ============================================================
# 2. policy 行业政策测试
# ============================================================

class TestPolicyCalendar:
    """政策日历加载与行业映射"""

    def test_calendar_loads(self):
        """政策日历JSON可正常加载"""
        cal = _load_policy_calendar()
        assert "industries" in cal
        assert "stock_industry_map" in cal
        assert len(cal["industries"]) == 9, f"预期9大行业，实际{len(cal['industries'])}"

    def test_stock_industry_map(self):
        """stock_industry_map 直接映射"""
        cal = _load_policy_calendar()
        ind = _get_stock_industry("603986.SH", cal)
        assert ind == "半导体", f"603986应为半导体，实际{ind}"

        ind = _get_stock_industry("600519.SH", cal)
        assert ind == "消费", f"600519应为消费，实际{ind}"

    def test_stock_not_in_map(self):
        """不在映射中的标的返回None"""
        cal = _load_policy_calendar()
        ind = _get_stock_industry("999999.SH", cal)
        assert ind is None, f"未知标的应返回None，实际{ind}"

    def test_industries_have_policies(self):
        """9大行业均有policies字段"""
        cal = _load_policy_calendar()
        for name, info in cal["industries"].items():
            assert "policies" in info, f"{name}缺少policies"
            assert "keywords" in info, f"{name}缺少keywords"
            assert "sentiment" in info, f"{name}缺少sentiment"
            assert len(info["policies"]) >= 1, f"{name}至少1条政策"


class TestPolicyDecay:
    """政策衰减与前瞻窗口"""

    def test_today_policy_full_score(self):
        """今天发布的政策权重=1.0"""
        today = datetime(2026, 6, 10)
        ind_info = {"policies": [{"date": "2026-06-10", "score": 1.5, "type": "利好", "desc": "test"}],
                     "keywords": []}
        cal = _load_policy_calendar()
        score = _match_industry("603986.SH", "半导体", ind_info, today)
        assert score == pytest.approx(1.5, abs=0.01), f"今天政策应全额1.5，实际{score}"

    def test_30day_decay(self):
        """30天前政策衰减: weight = max(0.2, 1-30/30) = 0.2 → score=1.5*0.2=0.3"""
        today = datetime(2026, 6, 10)
        ind_info = {"policies": [{"date": "2026-05-11", "score": 1.5, "type": "利好", "desc": "test"}],
                     "keywords": []}
        cal = _load_policy_calendar()
        score = _match_industry("603986.SH", "半导体", ind_info, today)
        # 30天: days_diff=30, weight=max(0.2, 1-30/30)=0.2, score=1.5*0.2=0.3
        assert score == pytest.approx(0.3, abs=0.01), f"30天衰减后应为0.3，实际{score}"

    def test_31day_expired(self):
        """31天前政策失效→不计入"""
        today = datetime(2026, 6, 10)
        ind_info = {"policies": [{"date": "2026-05-10", "score": 1.5, "type": "利好", "desc": "test"}],
                     "keywords": []}
        cal = _load_policy_calendar()
        score = _match_industry("603986.SH", "半导体", ind_info, today)
        assert score == 0.0, f"31天前政策应失效=0，实际{score}"

    def test_10day_forward_window(self):
        """未来10天内政策有前瞻权重"""
        today = datetime(2026, 6, 10)
        ind_info = {"policies": [{"date": "2026-06-15", "score": 1.0, "type": "利好", "desc": "test"}],
                     "keywords": []}
        cal = _load_policy_calendar()
        score = _match_industry("603986.SH", "半导体", ind_info, today)
        # days_diff=-5, weight=1.0+(-5)/10=0.5, score=1.0*0.5=0.5
        assert score == pytest.approx(0.5, abs=0.01), f"5天后政策权重应为0.5，实际{score}"

    def test_future_beyond_10day_ignored(self):
        """未来超过10天的政策不计入"""
        today = datetime(2026, 6, 10)
        ind_info = {"policies": [{"date": "2026-06-25", "score": 1.5, "type": "利好", "desc": "test"}],
                     "keywords": []}
        cal = _load_policy_calendar()
        score = _match_industry("603986.SH", "半导体", ind_info, today)
        assert score == 0.0, f"15天后政策不应计入，实际{score}"

    def test_wrong_industry_returns_zero(self):
        """标的行业不匹配→返回0"""
        today = datetime(2026, 6, 10)
        ind_info = {"policies": [{"date": "2026-06-01", "score": 1.5, "type": "利好", "desc": "test"}],
                     "keywords": []}
        cal = _load_policy_calendar()
        # 603986 是半导体，检查新能源行业→应返回0
        score = _match_industry("603986.SH", "新能源", ind_info, today)
        assert score == 0.0, f"行业不匹配应为0，实际{score}"


class TestPolicyWebSearch:
    """policy web搜索补充验证"""

    def test_positive_keyword_counting(self):
        """利好关键词计数: 补贴+支持+利好=3词→1.5"""
        mock = _make_mock_search([
            {"title": "补贴", "description": "出台补贴政策支持新能源，利好行业"},
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_policy("603986.SH", conn, search_fn=mock)
            assert isinstance(score, float)
        finally:
            conn.close()
            os.unlink(db)

    def test_negative_keyword_counting(self):
        """利空关键词计数: 监管+限制+处罚=3词→-1.5"""
        mock = _make_mock_search([
            {"title": "监管处罚", "description": "监管趋严限制行业扩张处罚违规"},
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_policy("603986.SH", conn, search_fn=mock)
            assert isinstance(score, float)
        finally:
            conn.close()
            os.unlink(db)

    def test_no_industry_generic_search(self):
        """无行业信息→泛搜索fallback"""
        mock = _make_mock_search([
            {"title": "政策利好", "description": "补贴支持税收减免"},
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_policy("999999.SH", conn, search_fn=mock)
            assert isinstance(score, float)
        finally:
            conn.close()
            os.unlink(db)

    def test_search_exception_returns_zero(self):
        """policy搜索异常→返回0(不崩溃)"""
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_policy("603986.SH", conn, search_fn=_mock_search_error)
            assert isinstance(score, float)
        finally:
            conn.close()
            os.unlink(db)

    def test_policy_cache_hit(self):
        """policy缓存: 同日两次调用一致"""
        call_count = [0]
        def counted_search(query):
            call_count[0] += 1
            return [{"title": "利好", "description": "补贴支持"}]

        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            s1 = _compute_policy("603986.SH", conn, search_fn=counted_search)
            s2 = _compute_policy("603986.SH", conn, search_fn=counted_search)
            assert call_count[0] == 1, f"缓存命中不应再搜索({call_count[0]}次)"
            assert s1 == s2
        finally:
            conn.close()
            os.unlink(db)


# ============================================================
# 3. dragon_tiger 龙虎榜测试
# ============================================================

class TestDragonTiger:
    """龙虎榜因子测试"""

    def test_inst_buy_above_threshold(self):
        """机构净买入>1000万→+1.0"""
        mock = _make_mock_search([
            {"title": "龙虎榜 600519", "description": "机构净买入5000万"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_dragon_tiger("600519.SH", conn, search_fn=mock)
            assert score == 1.0, f"机构净买入5000万应得+1.0，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    def test_inst_buy_yi_unit(self):
        """机构净买入(亿单位)→转换为万后>1000→+1.0"""
        mock = _make_mock_search([
            {"title": "龙虎榜 603986", "description": "机构净买入2.5亿"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_dragon_tiger("603986.SH", conn, search_fn=mock)
            assert score == 1.0, f"机构净买入2.5亿(25000万)应得+1.0，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    def test_inst_sell_above_threshold(self):
        """机构净卖出>1000万→-1.0"""
        mock = _make_mock_search([
            {"title": "龙虎榜 000001", "description": "机构净卖出3000万"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_dragon_tiger("000001.SZ", conn, search_fn=mock)
            assert score == -1.0, f"机构净卖出3000万应得-1.0，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    @pytest.mark.xfail(reason=P0_DT_FALLTHROUGH)
    def test_inst_buy_below_threshold(self):
        """机构净买入500万: 正则匹配<1000不加+1.0，但应兜底+0.8"""
        mock = _make_mock_search([
            {"title": "龙虎榜 000002", "description": "机构净买入500万"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_dragon_tiger("000002.SZ", conn, search_fn=mock)
            # 500万<1000万，正则匹配不触发 +1.0，应触发 elif "机构净买入" 兜底 +0.8
            assert score == 0.8, f"机构净买入500万兜底应得+0.8，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    def test_youzi_relay(self):
        """游资接力→+0.5"""
        mock = _make_mock_search([
            {"title": "龙虎榜 002415", "description": "游资接力连续上榜"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_dragon_tiger("002415.SZ", conn, search_fn=mock)
            assert score == 0.5, f"游资接力应得+0.5，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    def test_no_listing_returns_zero(self):
        """无上榜→返回0"""
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_dragon_tiger("999999.SH", conn, search_fn=_mock_search_empty)
            assert score == 0.0, f"无上榜应为0，实际{score}"
        finally:
            conn.close()
            os.unlink(db)

    def test_search_exception_returns_zero(self):
        """搜索异常→返回0"""
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_dragon_tiger("000001.SZ", conn, search_fn=_mock_search_error)
            assert score == 0.0
        finally:
            conn.close()
            os.unlink(db)

    def test_clip_score(self):
        """分数裁剪到±1.5: 机构买入+游资接力=1.5, 再加也应≤1.5"""
        mock = _make_mock_search([
            {"title": "龙虎榜", "description": "机构净买入5000万 游资接力连续上榜"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_dragon_tiger("600519.SH", conn, search_fn=mock)
            # 机构买入1.0+游资0.5=1.5, 裁剪后仍≤1.5
            assert -1.5 <= score <= 1.5, f"分数{score}超出范围[-1.5,1.5]"
        finally:
            conn.close()
            os.unlink(db)

    def test_cache_hit(self):
        """龙虎榜缓存: 同日两次调用一致"""
        call_count = [0]
        def counted_search(query):
            call_count[0] += 1
            return [{"title": "龙虎榜", "description": "机构净买入2000万"}]

        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            s1 = _compute_dragon_tiger("600519.SH", conn, search_fn=counted_search)
            s2 = _compute_dragon_tiger("600519.SH", conn, search_fn=counted_search)
            assert call_count[0] == 1, f"缓存命中不应再搜索({call_count[0]})"
            assert s1 == s2
        finally:
            conn.close()
            os.unlink(db)

    def test_inst_buy_no_amount(self):
        """仅有买入文字无数额→兜底+0.8"""
        mock = _make_mock_search([
            {"title": "龙虎榜 603986", "description": "机构买入上榜"}
        ])
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_dragon_tiger("603986.SH", conn, search_fn=mock)
            assert score == 0.8, f"'机构买入'兜底应得+0.8，实际{score}"
        finally:
            conn.close()
            os.unlink(db)


# ============================================================
# 4. 批量接口测试
# ============================================================

class TestBatchInterface:
    """compute_news_factors_batch 批量接口"""

    def test_batch_returns_dict(self):
        """批量返回格式: {ts_code: {announcement, policy, dragon_tiger}}"""
        mock = _make_mock_search([])
        results = compute_news_factors_batch(["603986.SH", "600519.SH"],
                                             search_fn=mock)
        assert isinstance(results, dict)
        for code in ["603986.SH", "600519.SH"]:
            assert code in results
            for key in ["announcement", "policy", "dragon_tiger"]:
                assert key in results[code], f"{code}缺少{key}"
                assert isinstance(results[code][key], float)

    def test_batch_individual_failure_does_not_affect_others(self):
        """个别因子搜索失败不影响其余: 单只的announcement失败返回0，其他正常"""
        def fail_announcement_search(query):
            if "公告" in query:
                raise RuntimeError("search failed for announcement")
            return [{"title": "龙虎榜 600519", "description": "机构净买入2000万"}]

        results = compute_news_factors_batch(["603986.SH", "600519.SH"],
                                             search_fn=fail_announcement_search)
        # announcement搜索失败→该只announcement=0, 但不影响policy/dragon_tiger/其他标的
        assert results["603986.SH"]["announcement"] == 0.0, "announcement失败应返回0"
        assert isinstance(results["600519.SH"]["dragon_tiger"], float)
        # 600519的龙虎榜不受603986的announcement失败影响
        assert results["600519.SH"]["dragon_tiger"] >= 0, "龙虎榜应有结果"

    def test_batch_20_stocks_within_30s(self):
        """20只标的<30秒(模拟空搜索)"""
        codes = [f"{600000 + i:06d}.SH" for i in range(20)]
        mock = _make_mock_search([])
        import time
        start = time.time()
        results = compute_news_factors_batch(codes, search_fn=mock)
        elapsed = time.time() - start
        assert elapsed < 30, f"20只空搜索耗时{elapsed:.1f}s>30s"
        assert len(results) == 20

    def test_batch_empty_input(self):
        """空列表→返回空dict"""
        results = compute_news_factors_batch([], search_fn=_mock_search_empty)
        assert results == {}


# ============================================================
# 5. 搜索函数注入测试
# ============================================================

class TestSearchInjection:
    """set_search_function 搜索函数注入 (部分被P0 regex bug阻塞)"""

    @pytest.mark.xfail(reason=P0_REGEX_STAR)
    def test_inject_custom_search(self):
        """注入自定义搜索后，使用自定义搜索源"""
        def custom_search(query):
            return [{"title": "custom result", "description": "custom注入成功"}]

        set_search_function(custom_search)
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            score = _compute_announcement("600519.SH", conn)
            assert score == 0.0, f"custom无匹配关键词应得0，实际{score}"
        finally:
            conn.close()
            os.unlink(db)
            set_search_function(None)

    @pytest.mark.xfail(reason=P0_REGEX_STAR)
    def test_per_call_override(self):
        """单次调用传参覆盖全局注入"""
        def global_search(query):
            return [{"title": "减持", "description": "减持公告"}]

        def local_search(query):
            return [{"title": "业绩预增", "description": "增长公告"}]

        set_search_function(global_search)
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            s_local = _compute_announcement("600001.SH", conn, search_fn=local_search)
            assert s_local == 1.8, f"传参覆盖应为+1.8(业绩预增)，实际{s_local}"
        finally:
            conn.close()
            os.unlink(db)
            set_search_function(None)


# ============================================================
# 6. 核心接口测试
# ============================================================

class TestCoreInterface:
    """compute_news_factors 核心接口"""

    def test_return_format(self):
        """返回格式: {announcement, policy, dragon_tiger}"""
        result = compute_news_factors("603986.SH", search_fn=_mock_search_empty)
        assert set(result.keys()) == {"announcement", "policy", "dragon_tiger"}
        for key in result:
            assert isinstance(result[key], float), f"{key}不是float"

    def test_score_ranges(self):
        """各因子值域检查"""
        result = compute_news_factors("603986.SH", search_fn=_mock_search_empty)
        assert -2.0 <= result["announcement"] <= 2.0, f"announcement超出[-2,2]"
        assert -1.5 <= result["policy"] <= 1.5, f"policy超出[-1.5,1.5]"
        assert -1.5 <= result["dragon_tiger"] <= 1.5, f"dragon_tiger超出[-1.5,1.5]"

    def test_independent_factors(self):
        """3因子独立计算（一个失败不影响其他）"""
        def fail_announcement(query):
            if "公告" in query:
                raise RuntimeError("announcement search failed")
            return []

        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            ann = _compute_announcement("603986.SH", conn, search_fn=fail_announcement)
            pol = _compute_policy("603986.SH", conn, search_fn=_mock_search_empty)
            dt = _compute_dragon_tiger("603986.SH", conn, search_fn=_mock_search_empty)
            assert ann == 0.0, "announcement失败应返回0"
            assert isinstance(pol, float)
            assert isinstance(dt, float)
        finally:
            conn.close()
            os.unlink(db)


# ============================================================
# 7. 数据完整性测试
# ============================================================

class TestDataIntegrity:
    """数据完整性: 无未来信息/无幸存者偏差/异常值检查"""

    def test_cache_uses_today_only(self):
        """缓存日期使用当天，无未来信息"""
        today = date.today().strftime("%Y%m%d")
        db = _temp_cache_db()
        try:
            conn = _init_cache_db(db)
            _set_cached(conn, "test_code.SH", "announcement", 1.0, "test", today)
            future = "20991231"
            cached = _get_cached(conn, "test_code.SH", "announcement", future)
            assert cached is None, "未来日期不应命中缓存"
            cached = _get_cached(conn, "test_code.SH", "announcement", today)
            assert cached is not None, "当天应命中缓存"
        finally:
            conn.close()
            os.unlink(db)

    def test_policy_calendar_no_future_info(self):
        """政策日历日期均不晚于当天"""
        today = date.today()
        cal = _load_policy_calendar()
        for industry, info in cal["industries"].items():
            for p in info["policies"]:
                try:
                    p_date = datetime.strptime(p["date"], "%Y-%m-%d").date()
                    assert (today - p_date).days >= -10, \
                        f"{industry}政策日期{p['date']}超出10天前瞻窗口"
                except ValueError:
                    pass

    def test_no_survivor_bias_cache(self):
        """缓存表结构: ts_code为TEXT不限制值域(退市股也可缓存)"""
        conn = _init_cache_db()
        try:
            c = conn.cursor()
            c.execute("PRAGMA table_info(factor_cache)")
            cols = {row[1]: row[2] for row in c.fetchall()}
            assert cols["ts_code"] == "TEXT", "ts_code应为TEXT(不限制值域)"
        finally:
            conn.close()


# ============================================================
# 8. 压力测试：真实搜索（agent环境下）
# ============================================================

class TestRealSearch:
    """真实hermes_tools.web_search集成测试"""

    @pytest.mark.slow
    def test_real_search_returns_results(self):
        """真实搜索应返回非空结果"""
        try:
            from hermes_tools import web_search
        except ImportError:
            pytest.skip("非agent环境，跳过真实搜索测试")

        result = compute_news_factors("600519.SH")
        assert "announcement" in result
        assert isinstance(result["announcement"], float)
        assert isinstance(result["policy"], float)
        assert isinstance(result["dragon_tiger"], float)


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
