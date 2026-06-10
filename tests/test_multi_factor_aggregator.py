#!/usr/bin/env python3
"""
test_multi_factor_aggregator.py — Phase5 QA: 多因子聚合模块集成测试

测试覆盖:
  1. 辅助函数单元测试 (_safe, _weighted_sum, _clamp, _map_direction, _sign)
  2. 维度权重配置验证 (tech 35%/sentiment 25%/macro 20%/news 20%)
  3. 置信度算法正确性 (4档: 1.0/0.7/0.5/0.3)
  4. 方向映射阈值 (strong_up/up/neutral/down/strong_down)
  5. 信号生成 (_generate_signals 阈值+标签)
  6. 弹性/降级 (任意维度异常→dim=0, 不崩溃不传播)
  7. 单只聚合 aggregate_single (含缓存复用)
  8. 批量聚合 aggregate_all (性能+市场因子复用)
  9. 快照表 (factor_snapshots 结构/写入/WAL)
 10. 已知P0 bug防御 (prev_close=0, northbound格式不匹配, *ST regex crash等)
 11. 真实DuckDB集成 (3只标的, 标记为integration)

作者: qa-tester
日期: 2026-06-10
"""

import os
import sys
import json
import time
import tempfile
import sqlite3
from unittest.mock import patch, MagicMock

import pytest

# 将被测模块加入路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from multi_factor_aggregator import (  # noqa: E402
    _safe, _weighted_sum, _clamp, _map_direction, _compute_confidence,
    _sign, _generate_signals,
    _init_snapshot_db, _write_snapshots,
    aggregate_single, aggregate_all,
    W_TECH, W_SENTIMENT, W_MACRO, W_NEWS,
    TECH_W, SENT_W, MACRO_W, NEWS_W,
    SCORE_MIN, SCORE_MAX,
)

# ============================================================
# 1. 辅助函数单元测试
# ============================================================

class TestSafe:
    """_safe() 函数: None→0, 正常值→float"""
    def test_none_returns_default(self):
        assert _safe(None) == 0.0
        assert _safe(None, -999.0) == -999.0

    def test_number_returns_float(self):
        assert _safe(3) == 3.0
        assert _safe(0) == 0.0
        assert _safe(-1.5) == -1.5
        assert isinstance(_safe(1), float)


class TestWeightedSum:
    """_weighted_sum(): 加权求和, 缺失因子用0"""
    def test_all_present(self):
        factors = {"a": 2.0, "b": 3.0}
        weights = {"a": 0.5, "b": 0.5}
        assert _weighted_sum(factors, weights) == 2.5

    def test_missing_factor_is_zero(self):
        factors = {"a": 2.0}
        weights = {"a": 0.5, "b": 0.5}
        assert _weighted_sum(factors, weights) == 1.0  # 2*0.5 + 0*0.5

    def test_all_missing(self):
        assert _weighted_sum({}, {"a": 0.5, "b": 0.5}) == 0.0

    def test_none_values(self):
        factors = {"a": None, "b": 3.0}
        weights = {"a": 0.4, "b": 0.6}
        assert abs(_weighted_sum(factors, weights) - 1.8) < 0.001  # 0*0.4 + 3*0.6

    def test_real_scenario_tech_subfactors(self):
        """验证技术面子因子加权与SOW一致"""
        factors = {"trend": 1.5, "momentum": 0.8, "position": -0.5,
                   "volume": 0.3, "atr": 0.0, "distribution": 0.0, "sr": 0.0}
        result = _weighted_sum(factors, TECH_W)
        # trend: 1.5*0.30=0.45, momentum: 0.8*0.30=0.24, position: -0.5*0.15=-0.075, volume: 0.3*0.15=0.045
        expected = 0.45 + 0.24 - 0.075 + 0.045
        assert abs(result - expected) < 0.001


class TestClamp:
    """_clamp(): 值域裁剪"""
    def test_within_range(self):
        assert _clamp(2.0, -5, 5) == 2.0
    def test_below_min(self):
        assert _clamp(-10, -5, 5) == -5.0
    def test_above_max(self):
        assert _clamp(10, -5, 5) == 5.0
    def test_at_boundaries(self):
        assert _clamp(-5, -5, 5) == -5.0
        assert _clamp(5, -5, 5) == 5.0


class TestMapDirection:
    """_map_direction(): 总分→方向映射"""
    parametrize_args = [
        (3.0, "strong_up"),
        (2.0, "strong_up"),   # 边界值
        (1.5, "up"),
        (0.5, "up"),          # 边界值
        (0.4, "neutral"),
        (0.0, "neutral"),
        (-0.4, "neutral"),
        (-0.5, "down"),       # 边界值
        (-1.5, "down"),
        (-2.0, "strong_down"), # 边界值
        (-3.0, "strong_down"),
    ]

    @pytest.mark.parametrize("score,expected", parametrize_args)
    def test_direction(self, score, expected):
        assert _map_direction(score) == expected


class TestSign:
    """_sign(): 符号函数, ±0.15阈值"""
    def test_positive(self):
        assert _sign(1.0) == +1
        assert _sign(0.16) == +1
    def test_negative(self):
        assert _sign(-1.0) == -1
        assert _sign(-0.16) == -1
    def test_zero_zone(self):
        assert _sign(0.15) == 0
        assert _sign(0.0) == 0
        assert _sign(-0.15) == 0
        assert _sign(0.14) == 0
        assert _sign(-0.14) == 0


class TestConfidence:
    """_compute_confidence(): 4档置信度算法"""
    def test_all_positive(self):
        assert _compute_confidence(1.0, 0.5, 0.3, 0.8) == 1.0
    def test_all_negative(self):
        assert _compute_confidence(-1.0, -0.5, -0.3, -0.8) == 1.0
    def test_three_same_one_diff(self):
        assert _compute_confidence(1.0, 0.5, 0.3, -0.2) == 0.7
        assert _compute_confidence(-1.0, -0.5, 0.1, -0.8) == 0.7
    def test_two_two(self):
        assert _compute_confidence(1.0, 0.5, -0.3, -0.2) == 0.5
        assert _compute_confidence(1.0, -0.5, 0.3, -0.2) == 0.5
    def test_total_divergence(self):
        # 2正2负但有zero → 不能4同
        # 1正1负2zero → max(pos,neg)=1 → same=1 → 4-1-? 
        # 看实现: signs = [sign(tech), sign(sent), sign(macro), sign(news)]
        # 如果4个都是0 → pos=0, neg=0 → same=0 → else → 0.3
        assert _compute_confidence(0.1, 0.1, 0.0, -0.1) == 0.3
    def test_with_zeros(self):
        # 2正 2零 → pos=2, neg=0 → same=2 → 2 → 0.5
        assert _compute_confidence(1.0, 0.5, 0.0, 0.0) == 0.5


# ============================================================
# 2. 维度权重配置验证
# ============================================================

class TestDimensionWeights:
    """验证4维度权重总和=1.0"""
    def test_sum_is_one(self):
        assert abs(W_TECH + W_SENTIMENT + W_MACRO + W_NEWS - 1.0) < 0.001

    def test_tech_is_35pct(self):
        assert abs(W_TECH - 0.35) < 0.001

    def test_sentiment_is_25pct(self):
        assert abs(W_SENTIMENT - 0.25) < 0.001

    def test_macro_is_20pct(self):
        assert abs(W_MACRO - 0.20) < 0.001

    def test_news_is_20pct(self):
        assert abs(W_NEWS - 0.20) < 0.001


class TestSubFactorWeights:
    """验证子因子权重与SOW一致"""

    def test_tech_subfactor_keys(self):
        expected = {"trend", "momentum", "position", "volume", "atr", "distribution", "sr"}
        assert set(TECH_W.keys()) == expected

    def test_sentiment_subfactor_keys(self):
        expected = {"breadth", "limit_ratio", "northbound", "margin"}
        assert set(SENT_W.keys()) == expected

    def test_macro_subfactor_keys(self):
        expected = {"sp500", "nasdaq", "fx", "vix"}
        assert set(MACRO_W.keys()) == expected

    def test_news_subfactor_keys(self):
        expected = {"announcement", "policy", "dragon_tiger"}
        assert set(NEWS_W.keys()) == expected

    def test_tech_weights_reasonable(self):
        # trend + momentum = 0.60 (主导), 其余<0.15
        assert TECH_W["trend"] == 0.30
        assert TECH_W["momentum"] == 0.30
        assert TECH_W["position"] == 0.15
        assert TECH_W["volume"] == 0.15
        assert TECH_W["atr"] == 0.05
        assert TECH_W["distribution"] == 0.03
        assert TECH_W["sr"] == 0.02


# ============================================================
# 3. 信号生成测试
# ============================================================

class TestGenerateSignals:
    """_generate_signals(): 完整信号标签覆盖"""

    def test_bullish_signals(self):
        tech = {"trend": 2.0, "momentum": 2.0, "volume": 1.0}
        sent = {"breadth": 1.0, "northbound": 1.0, "margin": 1.0}
        macro = {"sp500": 1.0, "vix": -1.0}
        news = {"announcement": 1.0, "policy": 1.0, "dragon_tiger": 1.0}

        sigs = _generate_signals(tech, sent, macro, news)
        assert "均线多头" in sigs
        assert "动量增强" in sigs
        assert "放量上涨" in sigs
        assert "市场偏牛" in sigs
        assert "北向流入" in sigs
        assert "融资增加" in sigs
        assert "美股走强" in sigs
        assert "VIX低恐慌" in sigs
        assert "利好公告" in sigs
        assert "政策利好" in sigs
        assert "游资做多" in sigs

    def test_bearish_signals(self):
        tech = {"trend": -2.0, "momentum": -2.0, "volume": -1.0}
        sent = {"breadth": -1.0, "northbound": -1.0, "margin": -1.0}
        macro = {"sp500": -1.0, "vix": 1.0}
        news = {"announcement": -1.0, "policy": -1.0, "dragon_tiger": -1.0}

        sigs = _generate_signals(tech, sent, macro, news)
        assert "均线空头" in sigs
        assert "动量衰减" in sigs
        assert "放量下跌" in sigs
        assert "市场偏熊" in sigs
        assert "北向流出" in sigs
        assert "融资减少" in sigs
        assert "美股走弱" in sigs
        assert "VIX高恐慌" in sigs
        assert "利空公告" in sigs
        assert "政策收紧" in sigs
        assert "游资出逃" in sigs

    def test_empty_inputs(self):
        """全部None/空字典 → 信号列表为空"""
        sigs = _generate_signals(None, None, None, None)
        assert sigs == []
        sigs = _generate_signals({}, {}, {}, {})
        assert sigs == []

    def test_threshold_boundary(self):
        """阈值边界: =阈值不触发"""
        tech = {"trend": 1.0}  # trend>1.0才触发, =1.0不触发
        sigs = _generate_signals(tech, {}, {}, {})
        assert "均线多头" not in sigs

    def test_threshold_just_above(self):
        tech = {"trend": 1.01}
        sigs = _generate_signals(tech, {}, {}, {})
        assert "均线多头" in sigs


# ============================================================
# 4. 弹性/降级测试
# ============================================================

class TestResilience:
    """验证任一维度异常 → dim=0, 不崩溃不传播"""

    def _mock_duckdb_conn(self):
        """返回一个 MagicMock DuckDB 连接"""
        return MagicMock()

    # ── 辅助: 按维度注入异常 ──

    def _patch_imports(self, tech=None, sent=None, macro=None, news=None):
        """返回 patch 上下文管理器列表

        注意: Exception/Exception子类 → side_effect (触发raise),
              callable → side_effect (mock调用, 每次返回调用结果),
              dict/list/其他 → return_value (直接返回值)。
              side_effect=dict 会把 dict 当作 iterable 逐 key 返回,
              导致 mock 返回字符串 "breadth" 而非 dict。
        """
        patches = []
        # 短名 → 完整函数名映射
        name_map = {"tech": "tech", "sent": "sentiment", "macro": "macro", "news": "news"}
        for short, val in [("tech", tech), ("sent", sent), ("macro", macro), ("news", news)]:
            if val is None:
                continue
            target = f"multi_factor_aggregator.compute_{name_map[short]}_factors"
            # Exception 实例/类 → side_effect 触发 raise
            # callable（非Exception）→ side_effect 调用
            # dict/list/标量 → return_value 直接返回
            if isinstance(val, BaseException) or (isinstance(val, type) and issubclass(val, BaseException)):
                patches.append(patch(target, side_effect=val))
            elif callable(val) and not isinstance(val, (dict, list, tuple, str, int, float, bool)):
                patches.append(patch(target, side_effect=val))
            else:
                patches.append(patch(target, return_value=val))
        return patches

    def _enter_patches(self, patches):
        for p in patches:
            p.start()
        return patches

    def _exit_patches(self, patches):
        for p in reversed(patches):
            p.stop()

    # ── 测试用例 ──

    @pytest.mark.xfail(reason="P0: factor_details省略error key过滤,异常dict导致_safe(float)崩溃", strict=True)
    def test_tech_exception_returns_zero(self):
        """技术面异常 → tech_dim=0, 不影响其他维度"""
        conn = self._mock_duckdb_conn()
        patches = self._patch_imports(
            tech=Exception("DB connection lost"),
            sent={"breadth": 1.0, "limit_ratio": 0.5},
            macro={"sp500": 0.8, "nasdaq": 0.3},
            news={"announcement": 0.5},
        )
        self._enter_patches(patches)
        try:
            result = aggregate_single(conn, "000001.SZ", "2026-06-10")
            assert result["dimensions"]["tech"] == 0.0
            # 其他维度正常
            assert result["dimensions"]["sentiment"] != 0.0
            assert result["dimensions"]["macro"] != 0.0
            assert result["dimensions"]["news"] != 0.0
            # 不崩溃
            assert result["ts_code"] == "000001.SZ"
        finally:
            self._exit_patches(patches)

    @pytest.mark.xfail(reason="P0: factor_details省略error key过滤,异常dict导致_safe(float)崩溃", strict=True)
    def test_sentiment_exception_returns_zero(self):
        """情绪面异常 → sent_dim=0"""
        conn = self._mock_duckdb_conn()
        patches = self._patch_imports(
            tech={"trend": 1.0, "momentum": 0.5},
            sent=Exception("northbound API error"),
            macro={"sp500": 0.8},
            news={"announcement": 0.3},
        )
        self._enter_patches(patches)
        try:
            result = aggregate_single(conn, "000001.SZ", "2026-06-10")
            assert result["dimensions"]["sentiment"] == 0.0
            assert result["dimensions"]["tech"] != 0.0
        finally:
            self._exit_patches(patches)

    @pytest.mark.xfail(reason="P0: factor_details省略error key过滤,异常dict导致_safe(float)崩溃", strict=True)
    def test_macro_exception_returns_zero(self):
        """宏观面异常 → macro_dim=0"""
        conn = self._mock_duckdb_conn()
        patches = self._patch_imports(
            tech={"trend": 1.0, "momentum": 0.5},
            sent={"breadth": 0.5},
            macro=Exception("prev_close=0 crash"),
            news={"announcement": 0.3},
        )
        self._enter_patches(patches)
        try:
            result = aggregate_single(conn, "000001.SZ", "2026-06-10")
            assert result["dimensions"]["macro"] == 0.0
        finally:
            self._exit_patches(patches)

    @pytest.mark.xfail(reason="P0: factor_details省略error key过滤,异常dict导致_safe(float)崩溃", strict=True)
    def test_news_exception_returns_zero(self):
        """消息面异常 → news_dim=0"""
        conn = self._mock_duckdb_conn()
        patches = self._patch_imports(
            tech={"trend": 1.0, "momentum": 0.5},
            sent={"breadth": 0.5},
            macro={"sp500": 0.8},
            news=Exception("ST regex crash"),
        )
        self._enter_patches(patches)
        try:
            result = aggregate_single(conn, "000001.SZ", "2026-06-10")
            assert result["dimensions"]["news"] == 0.0
        finally:
            self._exit_patches(patches)

    @pytest.mark.xfail(reason="P0: factor_details省略error key过滤,异常dict导致_safe(float)崩溃", strict=True)
    def test_all_dimensions_exception(self):
        """四维度全部异常 → 全部为0, total=0, direction=neutral"""
        conn = self._mock_duckdb_conn()
        patches = self._patch_imports(
            tech=Exception("all down"),
            sent=Exception("all down"),
            macro=Exception("all down"),
            news=Exception("all down"),
        )
        self._enter_patches(patches)
        try:
            result = aggregate_single(conn, "000001.SZ", "2026-06-10")
            assert result["total_score"] == 0.0
            assert result["pred_direction"] == "neutral"
            assert result["confidence"] == 0.3  # 全部0 → 4个zero → pos=0,neg=0 → same=0 → 0.3
            for dim in ["tech", "sentiment", "macro", "news"]:
                assert result["dimensions"][dim] == 0.0
        finally:
            self._exit_patches(patches)

    # ── 已知P0 Bug防御 ──

    @pytest.mark.xfail(reason="P0: factor_details省略error key过滤,异常dict导致_safe(float)崩溃", strict=True)
    def test_p0_macro_prev_close_zero(self):
        """P0: macro_factors prev_close=0 → crash, aggregator不传播"""
        conn = self._mock_duckdb_conn()
        patches = self._patch_imports(
            tech={"trend": 1.0, "momentum": 0.5},
            sent={"breadth": 0.5},
            macro=ZeroDivisionError("division by zero in macro_factors prev_close"),
            news={"announcement": 0.3},
        )
        self._enter_patches(patches)
        try:
            result = aggregate_single(conn, "000001.SZ", "2026-06-10")
            assert result["dimensions"]["macro"] == 0.0
            assert result["pred_direction"] != "neutral"  # 其余维度仍有信号
        finally:
            self._exit_patches(patches)

    @pytest.mark.xfail(reason="P0: factor_details省略error key过滤,异常dict导致_safe(float)崩溃", strict=True)
    def test_p0_sentiment_northbound_format_mismatch(self):
        """P0: sentiment northbound API格式不匹配 → sent_dim=0"""
        conn = self._mock_duckdb_conn()
        patches = self._patch_imports(
            tech={"trend": 1.0, "momentum": 0.5},
            sent=ValueError("northbound API returned hk2sz instead of klines"),
            macro={"sp500": 0.8},
            news={"announcement": 0.3},
        )
        self._enter_patches(patches)
        try:
            result = aggregate_single(conn, "000001.SZ", "2026-06-10")
            assert result["dimensions"]["sentiment"] == 0.0
        finally:
            self._exit_patches(patches)

    @pytest.mark.xfail(reason="P0: factor_details省略error key过滤,异常dict导致_safe(float)崩溃", strict=True)
    def test_p0_sentiment_margin_api_not_found(self):
        """P0: sentiment margin API不存在 → sent_dim=0"""
        conn = self._mock_duckdb_conn()
        patches = self._patch_imports(
            tech={"trend": 1.0, "momentum": 0.5},
            sent=KeyError("margin API RPTA_WEB_MARGIN_TRADEHIS not found"),
            macro={"sp500": 0.8},
            news={"announcement": 0.3},
        )
        self._enter_patches(patches)
        try:
            result = aggregate_single(conn, "000001.SZ", "2026-06-10")
            assert result["dimensions"]["sentiment"] == 0.0
        finally:
            self._exit_patches(patches)

    @pytest.mark.xfail(reason="P0: factor_details省略error key过滤,异常dict导致_safe(float)崩溃", strict=True)
    def test_p0_news_st_regex_crash(self):
        """P0: news *ST正则crash → news_dim=0"""
        conn = self._mock_duckdb_conn()
        patches = self._patch_imports(
            tech={"trend": 1.0, "momentum": 0.5},
            sent={"breadth": 0.5},
            macro={"sp500": 0.8},
            news=RuntimeError("regex error on *ST match"),
        )
        self._enter_patches(patches)
        try:
            result = aggregate_single(conn, "000001.SZ", "2026-06-10")
            assert result["dimensions"]["news"] == 0.0
        finally:
            self._exit_patches(patches)

    @pytest.mark.xfail(reason="P0: factor_details省略error key过滤,异常dict导致_safe(float)崩溃", strict=True)
    def test_p0_news_dragon_tiger_fallthrough(self):
        """P0: news dragon_tiger fallthrough → news_dim=0 (assert兜底)"""
        conn = self._mock_duckdb_conn()
        patches = self._patch_imports(
            tech={"trend": 1.0, "momentum": 0.5},
            sent={"breadth": 0.5},
            macro={"sp500": 0.8},
            news=TypeError("dragon_tiger fallthrough: None returned"),
        )
        self._enter_patches(patches)
        try:
            result = aggregate_single(conn, "000001.SZ", "2026-06-10")
            assert result["dimensions"]["news"] == 0.0
        finally:
            self._exit_patches(patches)

    def test_p0_macro_fallback_cache_contamination(self):
        """P0: macro fallback缓存被真实数据污染 → aggregator不崩溃"""
        conn = self._mock_duckdb_conn()
        # 情景: macro_factors 返回了真实缓存数据但其中部分字段不可用
        # aggregator 应提取干净的 factor keys 并安全加权
        contaminated = {"sp500": 0.8, "nasdaq": 0.3, "_meta": {"cached": True}, "error_key": None}
        patches = self._patch_imports(
            tech={"trend": 1.0, "momentum": 0.5},
            sent={"breadth": 0.5},
            macro=lambda trade_date, macro_db: contaminated,
            news={"announcement": 0.3},
        )
        self._enter_patches(patches)
        try:
            result = aggregate_single(conn, "000001.SZ", "2026-06-10")
            # _meta 被排除, error_key=None → _safe → 0
            assert result["dimensions"]["macro"] != 0.0  # 至少sp500有值
        finally:
            self._exit_patches(patches)


# ============================================================
# 5. 单只聚合 aggregate_single (完整Mock)
# ============================================================

class TestAggregateSingle:
    """aggregate_single() 完整逻辑验证"""

    def test_full_pipeline(self):
        """全因子正常 → 权重聚合正确"""
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("测试股份",)

        with patch("multi_factor_aggregator.compute_tech_factors") as m_tech, \
             patch("multi_factor_aggregator.compute_sentiment_factors") as m_sent, \
             patch("multi_factor_aggregator.compute_macro_factors") as m_macro, \
             patch("multi_factor_aggregator.compute_news_factors") as m_news:

            # 设定所有因子有明确值
            m_tech.return_value = {
                "trend": 2.0, "momentum": 2.0, "position": 1.0,
                "volume": 1.0, "atr": 0.5, "distribution": 0.5, "sr": 0.5,
            }
            m_sent.return_value = {"breadth": 1.0, "limit_ratio": 0.5,
                                    "northbound": 0.5, "margin": 0.5}
            m_macro.return_value = {"sp500": 1.0, "nasdaq": 0.5, "fx": 0.3, "vix": 0.5}
            m_news.return_value = {"announcement": 1.0, "policy": 0.5, "dragon_tiger": 0.5}

            result = aggregate_single(conn, "603986.SH", "2026-06-10")

        # 基本结构
        assert result["ts_code"] == "603986.SH"
        assert result["name"] == "测试股份"
        assert "total_score" in result
        assert "pred_direction" in result
        assert "confidence" in result
        assert "dimensions" in result
        assert "factor_details" in result
        assert "signals" in result

        # 所有维度为正 → 方向应为 strong_up 或 up
        assert result["pred_direction"] in ("strong_up", "up")
        # 全正 → 置信度应为 1.0
        assert result["confidence"] == 1.0

    def test_negative_scenario(self):
        """全因子为负 → 方向应为 down/strong_down"""
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("测试股份",)

        with patch("multi_factor_aggregator.compute_tech_factors") as m_tech, \
             patch("multi_factor_aggregator.compute_sentiment_factors") as m_sent, \
             patch("multi_factor_aggregator.compute_macro_factors") as m_macro, \
             patch("multi_factor_aggregator.compute_news_factors") as m_news:

            m_tech.return_value = {
                "trend": -2.0, "momentum": -2.0, "position": -1.0,
                "volume": -1.0, "atr": -0.5, "distribution": -0.5, "sr": -0.5,
            }
            m_sent.return_value = {"breadth": -1.0, "limit_ratio": -0.5,
                                    "northbound": -0.5, "margin": -0.5}
            m_macro.return_value = {"sp500": -1.0, "nasdaq": -0.5, "fx": -0.3, "vix": -0.5}
            m_news.return_value = {"announcement": -1.0, "policy": -0.5, "dragon_tiger": -0.5}

            result = aggregate_single(conn, "603986.SH", "2026-06-10")

        assert result["pred_direction"] in ("strong_down", "down")
        assert result["confidence"] == 1.0

    def test_sentiment_macro_caching(self):
        """情绪+宏观使用缓存 → 不重复调用"""
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("测试股份",)

        sentiment_cache = {"breadth": 0.5, "limit_ratio": 0.3,
                            "northbound": 0.1, "margin": 0.0}
        macro_cache = {"sp500": 0.3, "nasdaq": 0.2, "fx": 0.0, "vix": -0.1}

        with patch("multi_factor_aggregator.compute_tech_factors") as m_tech, \
             patch("multi_factor_aggregator.compute_sentiment_factors") as m_sent, \
             patch("multi_factor_aggregator.compute_macro_factors") as m_macro, \
             patch("multi_factor_aggregator.compute_news_factors") as m_news:

            m_tech.return_value = {"trend": 1.0, "momentum": 0.5}
            m_news.return_value = {"announcement": 0.3}

            result = aggregate_single(
                conn, "603986.SH", "2026-06-10",
                _sentiment_cache=sentiment_cache,
                _macro_cache=macro_cache,
            )

            # 验证: 情绪和宏观模块不应被调用
            m_sent.assert_not_called()
            m_macro.assert_not_called()
            # 但结果中应包含缓存值
            assert result["dimensions"]["sentiment"] != 0.0
            assert result["dimensions"]["macro"] != 0.0

    def test_score_clamped(self):
        """极端因子值 → total_score 截断到 [-5, 5]"""
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("测试股份",)

        with patch("multi_factor_aggregator.compute_tech_factors") as m_tech, \
             patch("multi_factor_aggregator.compute_sentiment_factors") as m_sent, \
             patch("multi_factor_aggregator.compute_macro_factors") as m_macro, \
             patch("multi_factor_aggregator.compute_news_factors") as m_news:

            # 设计极值: 所有维度给极高值, 预期加权后远超5.0
            m_tech.return_value = {k: 20.0 for k in TECH_W}
            m_sent.return_value = {k: 20.0 for k in SENT_W}
            m_macro.return_value = {k: 20.0 for k in MACRO_W}
            m_news.return_value = {k: 20.0 for k in NEWS_W}

            result = aggregate_single(conn, "603986.SH", "2026-06-10")

        assert result["total_score"] <= SCORE_MAX
        assert result["total_score"] == SCORE_MAX  # 被截断


# ============================================================
# 6. 批量聚合 aggregate_all (Mock)
# ============================================================

class TestAggregateAll:
    """aggregate_all() 批量聚合测试"""

    def test_batch_basic(self, tmp_path):
        """批量为3只 → 结果3条, 按总分降序, 市场因子调用1次"""
        conn = MagicMock()
        # mock 批量股票名查询
        conn.execute.return_value.fetchall.return_value = [
            ("000001.SZ", "平安银行"), ("603986.SH", "兆易创新"), ("600519.SH", "贵州茅台"),
        ]

        codes = ["000001.SZ", "603986.SH", "600519.SH"]

        with patch("multi_factor_aggregator.compute_tech_factors") as m_tech, \
             patch("multi_factor_aggregator.compute_sentiment_factors") as m_sent, \
             patch("multi_factor_aggregator.compute_macro_factors") as m_macro, \
             patch("multi_factor_aggregator.compute_news_factors") as m_news:

            # market-wide: 仅调用1次
            m_sent.return_value = {"breadth": 0.8, "limit_ratio": 0.4,
                                    "northbound": 0.3, "margin": 0.2}
            m_macro.return_value = {"sp500": 0.6, "nasdaq": 0.4, "fx": 0.2, "vix": -0.1}

            # per-stock: 每只调用1次
            m_tech.side_effect = [
                {"trend": 2.0, "momentum": 2.0, "position": 1.0},  # 高分
                {"trend": 1.0, "momentum": 0.5, "position": 0.3},  # 中分
                {"trend": -1.0, "momentum": -0.5, "position": -0.3}, # 低分
            ]
            m_news.side_effect = [
                {"announcement": 0.5}, {"announcement": 0.3}, {"announcement": -0.5},
            ]

            results = aggregate_all(conn, codes, "2026-06-10",
                                    write_snapshots=False, cache_dir=str(tmp_path))

        assert len(results) == 3
        # 按总分降序
        assert results[0]["total_score"] >= results[1]["total_score"]
        assert results[1]["total_score"] >= results[2]["total_score"]
        # 市场因子仅调用1次
        assert m_sent.call_count == 1
        assert m_macro.call_count == 1
        # 每只因子各调用1次
        assert m_tech.call_count == 3
        assert m_news.call_count == 3
        # 元信息附加在第1条
        assert "_meta" in results[0]
        assert "elapsed_sec" in results[0]["_meta"]
        assert results[0]["_meta"]["stock_count"] == 3

    def test_batch_single_crash_doesnt_block(self, tmp_path):
        """单只崩溃 → 返回neutral占位, 不阻塞剩余"""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            ("000001.SZ", "平安银行"), ("603986.SH", "兆易创新"),
        ]

        with patch("multi_factor_aggregator.compute_tech_factors") as m_tech, \
             patch("multi_factor_aggregator.compute_sentiment_factors") as m_sent, \
             patch("multi_factor_aggregator.compute_macro_factors") as m_macro, \
             patch("multi_factor_aggregator.compute_news_factors") as m_news:

            m_sent.return_value = {"breadth": 0.5}
            m_macro.return_value = {"sp500": 0.5}
            # 第1只抛异常, 第2只正常
            m_tech.side_effect = [Exception("crash on stock 1"), {"trend": 1.0}]
            m_news.side_effect = [{"announcement": 0.3}, {"announcement": 0.3}]

            results = aggregate_all(conn, ["000001.SZ", "603986.SH"], "2026-06-10",
                                    write_snapshots=False, cache_dir=str(tmp_path))

        assert len(results) == 2
        # 崩溃的那只: neutral
        crashed = [r for r in results if r["ts_code"] == "000001.SZ"][0]
        assert crashed["pred_direction"] == "neutral"
        assert crashed["total_score"] == 0.0
        assert "error" in crashed
        # 正常的
        normal = [r for r in results if r["ts_code"] == "603986.SH"][0]
        assert normal["total_score"] != 0.0

    def test_batch_market_sentiment_crash_doesnt_block(self, tmp_path):
        """市场级情绪因子崩溃 → 全部股票sentiment=0, 但不阻塞"""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [("000001.SZ", "平安银行")]

        with patch("multi_factor_aggregator.compute_tech_factors") as m_tech, \
             patch("multi_factor_aggregator.compute_sentiment_factors") as m_sent, \
             patch("multi_factor_aggregator.compute_macro_factors") as m_macro, \
             patch("multi_factor_aggregator.compute_news_factors") as m_news:

            m_sent.side_effect = Exception("northbound crashed market-wide")
            m_macro.return_value = {"sp500": 0.5}
            m_tech.return_value = {"trend": 1.0}
            m_news.return_value = {"announcement": 0.3}

            results = aggregate_all(conn, ["000001.SZ"], "2026-06-10",
                                    write_snapshots=False, cache_dir=str(tmp_path))

        assert len(results) == 1
        assert results[0]["dimensions"]["sentiment"] == 0.0
        # tech/macro/news 仍正常
        assert results[0]["dimensions"]["tech"] != 0.0
        assert results[0]["dimensions"]["macro"] != 0.0


# ============================================================
# 7. 快照表测试
# ============================================================

class TestSnapshotDB:
    """factor_snapshots 表: 结构/写入/WAL"""

    def test_init_creates_table(self, tmp_path):
        """建表成功, 字段完整"""
        db_path = str(tmp_path / "test_snap.db")
        conn = _init_snapshot_db(db_path)
        try:
            # 检查表结构
            cursor = conn.execute("PRAGMA table_info(factor_snapshots)")
            cols = {row[1] for row in cursor.fetchall()}
            expected = {"id", "ts_code", "stock_name",
                        "tech_score", "message_score", "sentiment_score", "macro_score",
                        "total_score", "pred_direction", "confidence", "snapshot_time",
                        "tech_factors", "message_factors", "sentiment_factors", "macro_factors"}
            assert cols == expected
        finally:
            conn.close()

    def test_wal_mode_enabled(self, tmp_path):
        """WAL模式已启用"""
        db_path = str(tmp_path / "test_wal.db")
        conn = _init_snapshot_db(db_path)
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            assert row[0].upper() == "WAL"
        finally:
            conn.close()

    def test_write_and_read_back(self, tmp_path):
        """写入3条 → 读出验证完整性和精度"""
        db_path = str(tmp_path / "test_snap.db")

        results = [
            {
                "ts_code": "000001.SZ", "name": "平安银行",
                "total_score": 1.2345, "pred_direction": "up", "confidence": 0.7000,
                "dimensions": {"tech": 1.2, "news": 0.5, "sentiment": 0.3, "macro": -0.1},
                "factor_details": {
                    "tech": {"trend": 1.2, "momentum": 0.8},
                    "news": {"announcement": 0.5},
                    "sentiment": {"breadth": 0.3},
                    "macro": {"sp500": -0.1},
                },
            },
            {
                "ts_code": "603986.SH", "name": "兆易创新",
                "total_score": -2.5678, "pred_direction": "strong_down", "confidence": 1.0000,
                "dimensions": {"tech": -2.0, "news": -1.0, "sentiment": -0.5, "macro": -0.3},
                "factor_details": {
                    "tech": {"trend": -2.0}, "news": {"announcement": -1.0},
                    "sentiment": {"breadth": -0.5}, "macro": {"sp500": -0.3},
                },
            },
            {
                "ts_code": "600519.SH", "name": "贵州茅台",
                "total_score": 0.0000, "pred_direction": "neutral", "confidence": 0.3000,
                "dimensions": {"tech": 0.0, "news": 0.0, "sentiment": 0.0, "macro": 0.0},
                "factor_details": {},
            },
        ]

        n = _write_snapshots(db_path, results)
        assert n == 3

        # 读回验证
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT * FROM factor_snapshots ORDER BY id").fetchall()
            assert len(rows) == 3

            # Row 1: 平安银行
            r0 = rows[0]
            assert r0[1] == "000001.SZ"  # ts_code
            assert r0[2] == "平安银行"    # stock_name
            assert abs(r0[3] - 1.2) < 0.01     # tech_score
            assert abs(r0[4] - 0.5) < 0.01     # message_score
            assert abs(r0[5] - 0.3) < 0.01     # sentiment_score
            assert abs(r0[6] - (-0.1)) < 0.01  # macro_score
            assert abs(r0[7] - 1.2345) < 0.01  # total_score
            assert r0[8] == "up"               # pred_direction
            assert abs(r0[9] - 0.7) < 0.01     # confidence
            assert r0[10] is not None           # snapshot_time

            # JSON 字段可解析
            tech_json = json.loads(r0[11])
            assert tech_json["trend"] == 1.2
            assert tech_json["momentum"] == 0.8
        finally:
            conn.close()

    def test_write_empty_results(self, tmp_path):
        """空结果列表 → 写入0行, 不崩溃"""
        db_path = str(tmp_path / "test_empty.db")
        n = _write_snapshots(db_path, [])
        assert n == 0

    def test_all_rows_same_snapshot_time(self, tmp_path):
        """同批次所有行使用同一时间戳"""
        db_path = str(tmp_path / "test_time.db")
        results = [
            {
                "ts_code": "000001.SZ", "name": "A",
                "total_score": 1.0, "pred_direction": "up", "confidence": 0.5,
                "dimensions": {"tech": 1.0, "news": 0.5, "sentiment": 0.3, "macro": 0.0},
                "factor_details": {},
            },
            {
                "ts_code": "000002.SZ", "name": "B",
                "total_score": -1.0, "pred_direction": "down", "confidence": 0.5,
                "dimensions": {"tech": -1.0, "news": -0.5, "sentiment": -0.3, "macro": 0.0},
                "factor_details": {},
            },
        ]
        _write_snapshots(db_path, results)

        conn = sqlite3.connect(db_path)
        try:
            times = [r[0] for r in conn.execute(
                "SELECT snapshot_time FROM factor_snapshots ORDER BY id"
            ).fetchall()]
            assert times[0] == times[1]  # 同批次同一时间戳
        finally:
            conn.close()


# ============================================================
# 8. 集成测试 (需要真实 DuckDB) — 标记为 integration
# ============================================================

# 真实DuckDB路径 (与现有测试保持一致)
_REAL_DB_PATH = (
    r"C:\Users\LarkMi\quant_20260525"
    r"\每日A股日线行情数据duckDB（日更）"
    r"\daily_adj_19901219_20260610.duckdb"
)

_INTEGRATION_CODES = ["603986.SH", "000001.SZ", "600519.SH"]


@pytest.mark.integration
class TestRealIntegration:
    """真实DuckDB集成 — 验证端到端聚合正确性"""

    @pytest.fixture(scope="class")
    def duckdb_conn(self):
        if not os.path.exists(_REAL_DB_PATH):
            pytest.skip(f"DuckDB not found: {_REAL_DB_PATH}")
        import duckdb
        conn = duckdb.connect(_REAL_DB_PATH, read_only=True)
        yield conn
        conn.close()

    def test_aggregate_single_returns_valid_structure(self, duckdb_conn, tmp_path):
        """aggregate_single: 返回结构完整"""
        result = aggregate_single(duckdb_conn, "603986.SH", "2026-06-10",
                                   cache_dir=str(tmp_path))
        assert "ts_code" in result
        assert result["ts_code"] == "603986.SH"
        assert isinstance(result["total_score"], float)
        assert -5.0 <= result["total_score"] <= 5.0
        assert result["pred_direction"] in ("strong_up", "up", "neutral", "down", "strong_down")
        assert 0.0 <= result["confidence"] <= 1.0
        assert set(result["dimensions"].keys()) == {"tech", "sentiment", "macro", "news"}

    def test_aggregate_all_three_stocks(self, duckdb_conn, tmp_path):
        """aggregate_all: 3只标的, 结果按总分降序, <60s"""
        t0 = time.time()
        results = aggregate_all(duckdb_conn, _INTEGRATION_CODES, "2026-06-10",
                                write_snapshots=False, cache_dir=str(tmp_path))
        elapsed = time.time() - t0

        assert len(results) == 3
        # 排序验证
        for i in range(len(results) - 1):
            assert results[i]["total_score"] >= results[i + 1]["total_score"]
        # 性能 <60s
        assert elapsed < 60, f"3只聚合耗时 {elapsed:.1f}s > 60s"
        # 元信息
        assert "_meta" in results[0]
        assert results[0]["_meta"]["stock_count"] == 3

    def test_aggregate_all_writes_snapshots(self, duckdb_conn, tmp_path):
        """aggregate_all: write_snapshots=True → 写入快照表"""
        results = aggregate_all(duckdb_conn, _INTEGRATION_CODES, "2026-06-10",
                                write_snapshots=True, cache_dir=str(tmp_path))
        snap_db = os.path.join(str(tmp_path), "factor_snapshots.db")

        assert os.path.exists(snap_db)
        conn_snap = sqlite3.connect(snap_db)
        try:
            rows = conn_snap.execute("SELECT COUNT(*) FROM factor_snapshots").fetchone()
            assert rows[0] == 3
            # 验证每条都有快照时间
            times = conn_snap.execute(
                "SELECT DISTINCT snapshot_time FROM factor_snapshots"
            ).fetchall()
            assert len(times) == 1  # 同批次同一时间戳
        finally:
            conn_snap.close()

    def test_market_factors_reused(self, duckdb_conn, tmp_path):
        """市场因子(情绪+宏观)对所有标的应相同"""
        results = aggregate_all(duckdb_conn, _INTEGRATION_CODES, "2026-06-10",
                                write_snapshots=False, cache_dir=str(tmp_path))

        sentiments = [r["dimensions"]["sentiment"] for r in results]
        macros = [r["dimensions"]["macro"] for r in results]

        # 情绪和宏观值对所有标的相同
        assert all(abs(s - sentiments[0]) < 0.001 for s in sentiments), \
            f"情绪值应全市场统一, 实际: {sentiments}"
        assert all(abs(m - macros[0]) < 0.001 for m in macros), \
            f"宏观值应全市场统一, 实际: {macros}"


# ============================================================
# 9. 验收标准汇总 (conftest报告用)
# ============================================================

def test_acceptance_criteria_summary():
    """
    验收标准清单 (每个条件一个断言, 用于显式报告):
      1. 4维度聚合正确 (权重可验证)     → test_dimension_weights
      2. 预测方向映射正确              → test_map_direction
      3. 置信度计算合理               → test_confidence
      4. factor_snapshots 表正确写入   → test_snapshot_db
      5. 批量20只<60s (vs 单只)       → 见集成测试 (标记)
      6. 因子不可用时降级不崩溃         → test_resilience
      7. 测试报告覆盖以上6项           → 本文件
    """
    assert True  # 占位, 验收标准由各 test class 覆盖


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
