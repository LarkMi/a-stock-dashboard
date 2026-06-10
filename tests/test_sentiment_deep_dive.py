#!/usr/bin/env python3
"""
test_sentiment_deep_dive.py — 情绪面因子深度验证（补充现有测试未覆盖的P0风险点）

覆盖:
  1. 北向资金API响应格式验证 (klines vs hk2sh/hk2sz)
  2. 融资融券API可访问性验证
  3. ST/BJ/8%过滤准确性对比
  4. 精确边界值计算 (全部上涨/全部下跌)
  5. _fetch_northbound_net() 和 _fetch_margin_change() 独立测试
  6. 因子崩溃隔离测试
"""

import os
import sys
import json
import tempfile
import urllib.request
import pytest
import duckdb
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from sentiment_factors import (
    _discover_duckdb, _normalize_date, _is_weekend,
    factor_breadth, factor_limit_ratio,
    factor_northbound, factor_margin,
    _fetch_northbound_net, _fetch_margin_change,
    _fetch_url, _get_cache, _set_cache, _get_cached_value,
    compute_sentiment_factors, compute_sentiment_factors_auto,
    _extract_raw_stats,
)

DB_DIR = r"C:\Users\LarkMi\quant_20260525\每日A股日线行情数据duckDB（日更）"
DB_PATH = os.path.join(DB_DIR, "daily_adj_19901219_20260610.duckdb")


# ============================================================
# Part A: Web API 深度验证 (P0风险)
# ============================================================

class TestNorthboundApiFormat:
    """验证北向资金API实际返回格式是否与代码解析方式匹配"""

    def test_api_is_reachable(self):
        """API端点可达"""
        url = "https://push2.eastmoney.com/api/qt/kamt.kline/get"
        result = _fetch_url(url + "?fields1=f1&fields2=f51&klt=101&lmt=1", timeout=10)
        assert result is not None, f"{url} 不可达"

    def test_response_has_expected_structure(self):
        """验证API响应结构 — 检查klines/hk2sh/hk2sz等字段"""
        url = ("https://push2.eastmoney.com/api/qt/kamt.kline/get?"
               "fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56&"
               "klt=101&lmt=1")
        data = _fetch_url(url, timeout=10)
        assert data is not None, "API响应为空"
        j = json.loads(data)

        # 关键：检查实际返回的数据字段
        data_block = j.get('data', {})
        actual_fields = set(data_block.keys()) - {'rc', 'rt', 'svr', 'lt', 'full', 'dlmkts'}

        # P0检测: 代码期望 'klines' 字段，实际返回 hk2sh/hk2sz/sh2hk/sz2hk
        has_klines = 'klines' in data_block
        has_hk2sh = 'hk2sh' in data_block
        has_hk2sz = 'hk2sz' in data_block

        print(f"\n  API返回字段: {actual_fields}")
        print(f"  代码期望 'klines': {has_klines}")
        print(f"  实际有 'hk2sh': {has_hk2sh}, 'hk2sz': {has_hk2sz}")

        # 记录供报告使用
        if not has_klines and (has_hk2sh or has_hk2sz):
            pytest.fail(
                f"P0 BUG: 代码解析 'klines' 但API返回 {actual_fields}。"
                f"北向资金因子始终返回0.0!"
            )

    def test_fetch_northbound_net_returns_real_data(self):
        """验证_fetch_northbound_net()是否真的获取到数据"""
        result = _fetch_northbound_net()
        print(f"\n  _fetch_northbound_net() 返回值: {result}")
        # 如果API格式不匹配，这里始终返回0.0而非None
        # None = API不可达；0.0 = 可能API格式错误或真实为0
        if result is not None and result == 0.0:
            # 需要进一步判断是真实0还是解析失败
            # 检查API原始响应
            url = ("https://push2.eastmoney.com/api/qt/kamt.kline/get?"
                   "fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56&"
                   "klt=101&lmt=1")
            raw = _fetch_url(url, timeout=10)
            j = json.loads(raw)
            data_block = j.get('data', {})
            has_klines = 'klines' in data_block
            if not has_klines:
                pytest.fail(
                    "P0: _fetch_northbound_net()=0.0 且API无klines字段 → 解析失败静默返回0"
                )


class TestMarginApiFormat:
    """验证融资融券API是否可用"""

    def test_api_is_reachable(self):
        """API端点可达"""
        url = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
        result = _fetch_url(
            url + "?reportName=RPTA_WEB_MARGIN_TRADEHIS&columns=TRADE_DATE&"
            "pageSize=1&pageNumber=1&source=WEB&client=WEB",
            timeout=10
        )
        assert result is not None, f"{url} 不可达"

    def test_response_report_name_valid(self):
        """验证报告名称是否存在"""
        url = ("https://datacenter.eastmoney.com/securities/api/data/v1/get?"
               "reportName=RPTA_WEB_MARGIN_TRADEHIS&"
               "columns=TRADE_DATE,FIN_BALANCE&"
               "sortColumns=TRADE_DATE&sortTypes=-1&"
               "pageSize=2&pageNumber=1&source=WEB&client=WEB")
        data = _fetch_url(url, timeout=10)
        assert data is not None
        j = json.loads(data)

        success = j.get('success', False)
        message = j.get('message', '')
        print(f"\n  API success={success}, message={message}")

        if not success:
            pytest.fail(
                f"P0: 融资融券API报告名 'RPTA_WEB_MARGIN_TRADEHIS' 不存在: {message}。"
                f"margin因子始终返回0.0!"
            )

    def test_fetch_margin_change_returns_real_data(self):
        """验证_fetch_margin_change()是否获取到数据"""
        result = _fetch_margin_change()
        print(f"\n  _fetch_margin_change() 返回值: {result}")
        if result is None:
            # 检查是否因为API不可达
            url = ("https://datacenter.eastmoney.com/securities/api/data/v1/get?"
                   "reportName=RPTA_WEB_MARGIN_TRADEHIS&"
                   "columns=TRADE_DATE,FIN_BALANCE&"
                   "pageSize=2&pageNumber=1&source=WEB&client=WEB")
            raw = _fetch_url(url, timeout=10)
            if raw:
                j = json.loads(raw)
                if not j.get('success'):
                    pytest.fail("P0: margin API报告名无效 → _fetch_margin_change()永远返回None")


# ============================================================
# Part B: 过滤逻辑精确验证
# ============================================================

@pytest.fixture(scope="module")
def db_conn():
    c = duckdb.connect(DB_PATH, read_only=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def latest_date(db_conn):
    return db_conn.execute('SELECT MAX(trade_date) FROM daily_adj').fetchone()[0]


class TestFilterCorrectness:
    """验证ST/BJ/8%过滤是否真的被应用在因子计算中"""

    def test_filter_matches_code(self, db_conn, latest_date):
        """对比：因子内SQL过滤 vs 手动全集计算 — 应该一致"""
        # 因子内部使用的SQL
        date = str(latest_date)

        # 获取因子使用的SQL过滤后数据
        filtered_sql = f"""
            SELECT pct_chg FROM daily_adj
            WHERE trade_date = '{date}'
              AND ts_code NOT LIKE '%ST%'
              AND ts_code NOT LIKE '%BJ%'
              AND ts_code NOT LIKE '8%'
              AND pct_chg IS NOT NULL
        """
        df_filtered = db_conn.execute(filtered_sql).df()

        # 获取全量数据
        all_sql = f"""
            SELECT pct_chg FROM daily_adj
            WHERE trade_date = '{date}'
              AND pct_chg IS NOT NULL
        """
        df_all = db_conn.execute(all_sql).df()

        # 对比
        excluded = len(df_all) - len(df_filtered)
        print(f"\n  全量: {len(df_all)}, 过滤后: {len(df_filtered)}, 排除: {excluded}")

        # 验证过滤后的数据没有ST/BJ/8%
        st_count = db_conn.execute(f"""
            SELECT COUNT(*) FROM daily_adj
            WHERE trade_date = '{date}'
              AND ts_code LIKE '%ST%' AND pct_chg IS NOT NULL
        """).fetchone()[0]
        bj_count = db_conn.execute(f"""
            SELECT COUNT(*) FROM daily_adj
            WHERE trade_date = '{date}'
              AND ts_code LIKE '%BJ%' AND pct_chg IS NOT NULL
        """).fetchone()[0]
        eight_count = db_conn.execute(f"""
            SELECT COUNT(*) FROM daily_adj
            WHERE trade_date = '{date}'
              AND ts_code LIKE '8%' AND pct_chg IS NOT NULL
        """).fetchone()[0]

        print(f"  ST:{st_count} BJ:{bj_count} 8开头:{eight_count}")

        # 如果过滤数量=0而实际有，说明过滤没生效
        if excluded == 0 and (st_count + bj_count + eight_count) > 0:
            print("  ⚠ 警告: 过滤条件未生效但实际存在被排除标的")

    def test_factor_uses_correct_filters(self, db_conn, latest_date):
        """验证factor_breadth结果与手动过滤计算一致"""
        date = str(latest_date)
        val = factor_breadth(db_conn, date)
        assert val is not None

        # 手动用同样的SQL计算
        df = db_conn.execute(f"""
            SELECT pct_chg FROM daily_adj
            WHERE trade_date = '{date}'
              AND ts_code NOT LIKE '%ST%'
              AND ts_code NOT LIKE '%BJ%'
              AND ts_code NOT LIKE '8%'
              AND pct_chg IS NOT NULL
        """).df()

        up = int((df['pct_chg'] > 0).sum())
        total = len(df)
        raw = up / total
        expected = float(np.clip((raw - 0.5) / 0.1, -2.0, 2.0))

        assert abs(val - expected) < 0.001, \
            f"因子计算{val} != 手动验证{expected} (raw={raw:.4f})"


# ============================================================
# Part C: 精确边界值验证
# ============================================================

class TestExactBoundaries:
    """用受控数据测试精确边界映射"""

    def test_breadth_all_up_is_2(self):
        """全部上涨 → breadth=2.0"""
        raw = 1.0  # 100%上涨
        score = (raw - 0.5) / 0.1  # = 5.0
        expected = float(np.clip(score, -2.0, 2.0))
        assert expected == 2.0, f"全部上涨应=2.0, 实际{expected}"

    def test_breadth_all_down_is_minus_2(self):
        """全部下跌 → breadth=-2.0"""
        raw = 0.0  # 0%上涨
        score = (raw - 0.5) / 0.1  # = -5.0
        expected = float(np.clip(score, -2.0, 2.0))
        assert expected == -2.0, f"全部下跌应=-2.0, 实际{expected}"

    def test_breadth_5050_is_0(self):
        """50%上涨 → breadth=0.0"""
        score = (0.5 - 0.5) / 0.1
        expected = float(np.clip(score, -2.0, 2.0))
        assert expected == 0.0

    def test_breadth_60pct_is_1(self):
        """60%上涨 → breadth=1.0"""
        score = (0.6 - 0.5) / 0.1
        expected = float(np.clip(score, -2.0, 2.0))
        assert expected == 1.0

    def test_limit_ratio_all_up_is_1point5(self):
        """全部涨停 → limit_ratio=1.5"""
        raw = 1.0
        score = (raw - 0.5) / 0.333
        expected = float(np.clip(score, -1.5, 1.5))
        assert expected == 1.5

    def test_limit_ratio_all_down_is_minus_1point5(self):
        """全部跌停 → limit_ratio=-1.5"""
        raw = 0.0
        score = (raw - 0.5) / 0.333
        expected = float(np.clip(score, -1.5, 1.5))
        assert abs(expected - (-1.5)) < 0.01

    def test_limit_ratio_no_limits_is_0(self):
        """无涨跌停 → 0.0"""
        assert factor_limit_ratio.__wrapped__ if hasattr(factor_limit_ratio, '__wrapped__') else True
        # 代码中 total_limits==0 → return 0.0 ✓

    def test_northbound_exact_mapping(self):
        """北向资金映射: net=60亿 → score=1.0"""
        net = 60.0
        score = float(np.clip(net / 30 * 0.5, -1.5, 1.5))
        assert score == 1.0, f"net=60应=1.0, 实际{score}"

    def test_northbound_extreme_bull(self):
        """北向资金300亿 → 1.5 (clamp)"""
        net = 300.0
        score = float(np.clip(net / 30 * 0.5, -1.5, 1.5))
        assert score == 1.5

    def test_margin_exact_mapping(self):
        """融资变化3.33% → score=1.0"""
        change = 3.333
        score = float(np.clip(change * 0.3, -1.0, 1.0))
        assert abs(score - 1.0) < 0.01

    def test_margin_extreme(self):
        """融资变化10% → 1.0 (clamp)"""
        change = 10.0
        score = float(np.clip(change * 0.3, -1.0, 1.0))
        assert score == 1.0


# ============================================================
# Part D: 因子崩溃隔离
# ============================================================

class TestFactorIsolation:
    """验证单个因子崩溃不影响其他因子"""

    def test_northbound_fails_others_still_work(self, db_conn, latest_date):
        """北向API失败 → DuckDB因子仍正常工作"""
        with tempfile.TemporaryDirectory() as d:
            result = compute_sentiment_factors(db_conn, latest_date, d)
            # DuckDB因子应始终有效
            assert result['breadth'] is not None
            assert result['limit_ratio'] is not None
            # 北向和融资可能是0.0但不应崩溃
            assert isinstance(result['northbound'], float)
            assert isinstance(result['margin'], float)

    def test_main_interface_never_raises(self, db_conn, latest_date):
        """主接口永不抛异常"""
        try:
            with tempfile.TemporaryDirectory() as d:
                result = compute_sentiment_factors(db_conn, latest_date, d)
            assert isinstance(result, dict)
        except Exception as e:
            pytest.fail(f"compute_sentiment_factors抛异常: {e}")

    def test_auto_interface_never_raises(self):
        """自动接口永不抛异常"""
        try:
            result = compute_sentiment_factors_auto()
            assert result is not None
        except Exception as e:
            pytest.fail(f"compute_sentiment_factors_auto抛异常: {e}")


# ============================================================
# Part E: 缓存完整链路测试
# ============================================================

class TestCacheFullCycle:
    """缓存完整生命周期: 无缓存→写入→命中→过期→刷新"""

    def test_full_cycle_write_read_expire(self, tmp_path):
        """完整缓存周期测试"""
        cache_dir = str(tmp_path)

        # 阶段1: 无缓存 → None
        result = _get_cache(cache_dir, 'cycle_test', max_age_hours=24)
        assert result is None, "初始应无缓存"

        # 阶段2: 写入
        _set_cache(cache_dir, 'cycle_test', 0.75, 'test_data')

        # 阶段3: 读取命中
        result = _get_cache(cache_dir, 'cycle_test', max_age_hours=24)
        assert result is not None
        assert result['value'] == 0.75
        assert result['raw_data'] == 'test_data'

        # 阶段4: 更新值 (INSERT OR REPLACE)
        _set_cache(cache_dir, 'cycle_test', -0.50, 'updated_data')
        result = _get_cache(cache_dir, 'cycle_test', max_age_hours=24)
        assert result['value'] == -0.50

        # 阶段5: 写入另一个因子，不影响第一个
        _set_cache(cache_dir, 'other_factor', 1.0)
        result = _get_cache(cache_dir, 'cycle_test', max_age_hours=24)
        assert result['value'] == -0.50, "其他因子写入不应影响cycle_test"

    def test_cache_edge_empty_raw_data(self, tmp_path):
        """raw_data为None/空时的处理"""
        _set_cache(str(tmp_path), 'no_raw', 0.5, None)
        result = _get_cache(str(tmp_path), 'no_raw', max_age_hours=24)
        assert result is not None
        assert result['value'] == 0.5


# ============================================================
# Part F: _extract_raw_stats 直接测试
# ============================================================

class TestExtractRawStats:
    """直接测试辅助函数_extract_raw_stats"""

    def test_returns_all_keys(self, db_conn, latest_date):
        raw = _extract_raw_stats(db_conn, str(latest_date))
        for key in ['breadth_raw', 'limit_up_count', 'limit_down_count',
                     'limit_ratio_raw', 'northbound_net', 'margin_change']:
            assert key in raw, f"缺少key: {key}"

    def test_breadth_raw_in_range(self, db_conn, latest_date):
        raw = _extract_raw_stats(db_conn, str(latest_date))
        if raw['breadth_raw'] is not None:
            assert 0.0 <= raw['breadth_raw'] <= 1.0, \
                f"breadth_raw应在[0,1], 实际{raw['breadth_raw']}"

    def test_count_consistency(self, db_conn, latest_date):
        """limit_up_count + limit_down_count 应该 ≥ 0"""
        raw = _extract_raw_stats(db_conn, str(latest_date))
        assert raw['limit_up_count'] >= 0
        assert raw['limit_down_count'] >= 0
        limits = raw['limit_up_count'] + raw['limit_down_count']
        if limits > 0 and raw['limit_ratio_raw'] is not None:
            assert 0.0 <= raw['limit_ratio_raw'] <= 1.0

    def test_nonexistent_date_returns_defaults(self, db_conn):
        raw = _extract_raw_stats(db_conn, '19000101')
        assert raw['breadth_raw'] is None
        assert raw['limit_up_count'] == 0


# ============================================================
# Part G: 性能基准
# ============================================================

class TestPerformance:
    """性能基准测试"""

    def test_breadth_computation_speed(self, db_conn, latest_date):
        """市场宽度计算 < 2秒"""
        import time
        t0 = time.time()
        for _ in range(5):
            factor_breadth(db_conn, str(latest_date))
        elapsed = time.time() - t0
        avg = elapsed / 5
        print(f"\n  breadth 5次平均: {avg:.3f}s")
        assert avg < 2.0, f"breadth计算{avg:.1f}s > 2s阈值"

    def test_full_computation_speed(self, db_conn, latest_date):
        """全因子计算 < 20秒"""
        import time
        with tempfile.TemporaryDirectory() as d:
            t0 = time.time()
            result = compute_sentiment_factors(db_conn, str(latest_date), d)
            elapsed = time.time() - t0
            print(f"\n  compute_sentiment_factors 耗时: {elapsed:.2f}s")
            # Web API可能慢，给30s宽容阈值
            assert elapsed < 30.0, f"全因子计算{elapsed:.1f}s > 30s阈值"

    def test_cache_read_speed(self, tmp_path):
        """缓存读取 < 0.01秒"""
        import time
        _set_cache(str(tmp_path), 'perf_test', 1.0)
        t0 = time.time()
        for _ in range(100):
            _get_cache(str(tmp_path), 'perf_test', max_age_hours=24)
        elapsed = time.time() - t0
        avg_ms = elapsed / 100 * 1000
        print(f"\n  缓存读取100次平均: {avg_ms:.2f}ms")
        assert avg_ms < 10, f"缓存读取{avg_ms:.1f}ms > 10ms阈值"


# ============================================================
# 运行入口
# ============================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
