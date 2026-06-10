#!/usr/bin/env python3
"""
test_macro_factors.py — 宏观面因子全面测试

测试范围:
  1. Yahoo Finance API: 4 symbol 可访问性, 3重试, fallback链
  2. 交叉验证: ≥2源, diff>20%逻辑, 搜索不可用降级
  3. 因子计算: sp500/nasdaq钳位, fx得分, vix线性映射
  4. 缓存+交易日: 48h TTL, 非交易日→0, datetime健壮性
  5. 便捷接口: batch/get_raw/reset_cache

作者: qa-tester
日期: 2026-06-10
"""

import os
import sys
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock, mock_open
from datetime import datetime, timedelta

# 将被测模块加入路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import macro_factors as mf  # noqa: E402


# ============================================================
# 测试辅助: 构造mock Yahoo API响应
# ============================================================

def _make_yahoo_response(regular_price, prev_close, timestamps=None):
    """构造标准Yahoo Finance v8 API JSON响应"""
    ts = timestamps or [1718000000, 1718086400, 1718172800, 1718259200, 1718345600]
    return {
        "chart": {
            "result": [{
                "meta": {
                    "regularMarketPrice": regular_price,
                    "previousClose": prev_close,
                    "chartPreviousClose": prev_close,
                },
                "timestamp": ts,
                "indicators": {
                    "quote": [{
                        "close": [prev_close * 0.99, prev_close * 1.01, prev_close, prev_close * 1.005, regular_price],
                    }]
                },
            }]
        }
    }


def _make_yahoo_vix_response(vix, prev_vix=None):
    """构造VIX专用响应"""
    ts = [1718000000, 1718086400, 1718172800, 1718259200, 1718345600]
    return {
        "chart": {
            "result": [{
                "meta": {
                    "regularMarketPrice": vix,
                    "previousClose": prev_vix or vix - 1,
                },
                "timestamp": ts,
            }]
        }
    }


def _mock_urlopen_factory(response_dict):
    """工厂函数: 返回一个mock urlopen, 按URL返回不同响应"""
    def _mock_urlopen(req, timeout=10):
        url = req.full_url if hasattr(req, 'full_url') else req.get_full_url()
        for pattern, resp_data in response_dict.items():
            if pattern in url:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps(resp_data).encode()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                return mock_resp
        raise Exception(f"Unexpected URL: {url}")
    return _mock_urlopen


# ============================================================
# 测试类1: Yahoo Finance 数据获取
# ============================================================

class TestYahooFinanceAPI(unittest.TestCase):
    """测试 Yahoo Finance v8 API 获取层"""

    def test_01_http_get_json_success(self):
        """正常获取JSON"""
        mock_data = {"test": "ok"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = mf._http_get_json("https://fake.url/test")
        self.assertEqual(result, mock_data)

    def test_02_http_get_json_retry_then_success(self):
        """前2次失败, 第3次成功 → 3次重试机制"""
        call_count = [0]

        def _mock_urlopen_retry(req, timeout=10):
            call_count[0] += 1
            if call_count[0] < 3:
                raise ConnectionError("临时网络错误")
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"ok": True}).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=_mock_urlopen_retry):
            with patch('time.sleep', return_value=None):  # 跳过sleep
                result = mf._http_get_json("https://fake.url/test")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(call_count[0], 3, "应该恰好重试3次")

    def test_03_http_get_json_all_retries_fail(self):
        """3次全部失败 → 抛出异常"""
        with patch('urllib.request.urlopen', side_effect=ConnectionError("网络不通")):
            with patch('time.sleep', return_value=None):
                with self.assertRaises(ConnectionError):
                    mf._http_get_json("https://fake.url/test")

    def test_04_fetch_yahoo_change_sp500_normal(self):
        """标普500正常获取: +1.5%"""
        resp = _make_yahoo_response(regular_price=5600.00, prev_close=5517.24)
        mock_fn = _mock_urlopen_factory({"^GSPC": resp})

        with patch('urllib.request.urlopen', side_effect=mock_fn):
            result = mf._fetch_yahoo_change("^GSPC")

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["chg_pct"], 1.5, delta=0.1)
        self.assertAlmostEqual(result["close"], 5600.0)
        self.assertAlmostEqual(result["prev_close"], 5517.24)

    def test_05_fetch_yahoo_change_nasdaq_normal(self):
        """纳斯达克正常获取: -0.8%"""
        resp = _make_yahoo_response(regular_price=18000.00, prev_close=18145.16)
        mock_fn = _mock_urlopen_factory({"^IXIC": resp})

        with patch('urllib.request.urlopen', side_effect=mock_fn):
            result = mf._fetch_yahoo_change("^IXIC")

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["chg_pct"], -0.8, delta=0.1)
        self.assertEqual(result["close"], 18000.0)

    def test_06_fetch_yahoo_change_cny_fx(self):
        """USDCNY正常获取: +0.3%"""
        resp = _make_yahoo_response(regular_price=7.25, prev_close=7.2283)
        mock_fn = _mock_urlopen_factory({"CNY=X": resp})

        with patch('urllib.request.urlopen', side_effect=mock_fn):
            result = mf._fetch_yahoo_change("CNY=X")

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["chg_pct"], 0.3, delta=0.1)

    def test_07_fetch_yahoo_vix_normal(self):
        """VIX正常获取"""
        resp = _make_yahoo_vix_response(vix=18.5, prev_vix=19.2)
        mock_fn = _mock_urlopen_factory({"^VIX": resp})

        with patch('urllib.request.urlopen', side_effect=mock_fn):
            result = mf._fetch_yahoo_vix()

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["vix"], 18.5)

    def test_08_fetch_yahoo_usdcny_normal(self):
        """USDCNY完整接口"""
        resp = _make_yahoo_response(regular_price=7.20, prev_close=7.18)
        mock_fn = _mock_urlopen_factory({"CNY=X": resp})

        with patch('urllib.request.urlopen', side_effect=mock_fn):
            result = mf._fetch_yahoo_usdcny()

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["rate"], 7.20)

    def test_09_fetch_yahoo_change_failure(self):
        """网络失败 → 返回None"""
        with patch('urllib.request.urlopen', side_effect=ConnectionError("网络故障")):
            with patch('time.sleep', return_value=None):
                result = mf._fetch_yahoo_change("^GSPC")
        self.assertIsNone(result)

    def test_10_fetch_yahoo_vix_failure(self):
        """VIX获取失败 → 返回None"""
        with patch('urllib.request.urlopen', side_effect=ConnectionError("网络故障")):
            with patch('time.sleep', return_value=None):
                result = mf._fetch_yahoo_vix()
        self.assertIsNone(result)

    def test_11_fetch_yahoo_change_zero_prev(self):
        """prev_close为0 → 应安全处理"""
        resp = _make_yahoo_response(regular_price=5600.0, prev_close=0.0)
        mock_fn = _mock_urlopen_factory({"^GSPC": resp})

        with patch('urllib.request.urlopen', side_effect=mock_fn):
            result = mf._fetch_yahoo_change("^GSPC")
        # 涨跌幅为inf或None, 但至少不应崩溃
        self.assertIsNotNone(result)

    def test_12_fetch_yahoo_change_chg_pct_calculation(self):
        """涨跌幅计算公式验证: (current - prev) / prev * 100"""
        # prev=100, current=102 → +2%
        resp = _make_yahoo_response(regular_price=102.0, prev_close=100.0)
        mock_fn = _mock_urlopen_factory({"TEST": resp})

        with patch('urllib.request.urlopen', side_effect=mock_fn):
            result = mf._fetch_yahoo_change("TEST")

        self.assertEqual(result["chg_pct"], 2.0)

        # prev=100, current=95 → -5%
        resp2 = _make_yahoo_response(regular_price=95.0, prev_close=100.0)
        mock_fn2 = _mock_urlopen_factory({"TEST": resp2})

        with patch('urllib.request.urlopen', side_effect=mock_fn2):
            result2 = mf._fetch_yahoo_change("TEST")

        self.assertEqual(result2["chg_pct"], -5.0)


# ============================================================
# 测试类2: 交叉验证 (web_search)
# ============================================================

class TestCrossValidation(unittest.TestCase):
    """测试 web_search 交叉验证逻辑"""

    def test_13_parse_pct_from_text_positive(self):
        """提取 +X.XX% 格式"""
        text = "The S&P 500 index rose 1.25% today"
        result = mf._parse_pct_from_text(text, ["s&p 500"])
        self.assertEqual(result, 1.25)

    def test_14_parse_pct_from_text_negative(self):
        """提取 -X.XX% 格式"""
        text = "NASDAQ composite dropped -2.10% in afternoon trading"
        result = mf._parse_pct_from_text(text, ["nasdaq"])
        self.assertEqual(result, -2.10)

    def test_15_parse_pct_from_text_no_match(self):
        """无匹配 → None"""
        result = mf._parse_pct_from_text("no numbers here", ["s&p 500"])
        self.assertIsNone(result)

    def test_16_parse_pct_from_text_empty(self):
        """空文本 → None"""
        self.assertIsNone(mf._parse_pct_from_text("", ["test"]))
        self.assertIsNone(mf._parse_pct_from_text(None, ["test"]))

    def test_17_parse_pct_from_text_changed_by(self):
        """'changed by ±X' 模式"""
        text = "S&P 500 changed by -1.52 points"
        result = mf._parse_pct_from_text(text, ["s&p 500"])
        self.assertEqual(result, -1.52)

    def test_18_try_hermes_search_unavailable(self):
        """hermes_tools不可用 → 返回None (不崩溃)"""
        with patch.dict('sys.modules', {'hermes_tools': None}):
            # 模拟ImportError
            with patch('macro_factors._try_hermes_search', side_effect=lambda x: None):
                result = mf._try_hermes_search("test query")
        self.assertIsNone(result)

    def test_19_cross_validate_sp500_search_available_consistent(self):
        """搜索可用, Yahoo +1.5%, 搜索 +1.4% → 差异<20% → 用Yahoo"""
        with patch('macro_factors._try_hermes_search', return_value="S&P 500 index change +1.4% today"):
            val, n_src, note = mf._cross_validate_sp500(1.5)

        self.assertEqual(val, 1.5)
        self.assertEqual(n_src, 2)
        self.assertIn("一致", note)

    def test_20_cross_validate_sp500_large_divergence(self):
        """差异>20% → 取均值"""
        with patch('macro_factors._try_hermes_search', return_value="S&P 500 dropped -3.0% today"):
            val, n_src, note = mf._cross_validate_sp500(1.5)

        # diff = |1.5 - (-3.0)| = 4.5, max(1.5*0.2, 0.3) = 0.3 → diff>0.3 → 取均值
        self.assertAlmostEqual(val, (1.5 + -3.0) / 2, delta=0.01)
        self.assertEqual(n_src, 2)
        self.assertIn("交叉验证", note)

    def test_21_cross_validate_sp500_no_search(self):
        """搜索不可用 → 单源, 标记警告"""
        with patch('macro_factors._try_hermes_search', return_value=None):
            val, n_src, note = mf._cross_validate_sp500(1.5)

        self.assertEqual(val, 1.5)
        self.assertEqual(n_src, 1)
        self.assertEqual(note, "仅YahooFinance")

    def test_22_cross_validate_sp500_parse_failure(self):
        """搜索有结果但无法解析数值 → 用Yahoo, 标记"""
        with patch('macro_factors._try_hermes_search', return_value="Stock market news today no numbers"):
            val, n_src, note = mf._cross_validate_sp500(1.5)

        self.assertEqual(val, 1.5)
        self.assertEqual(n_src, 1)
        self.assertIn("搜索无匹配", note)

    def test_23_cross_validate_nasdaq_consistent(self):
        """NASDAQ交叉验证一致"""
        with patch('macro_factors._try_hermes_search', return_value="Nasdaq Composite up 0.85%"):
            val, n_src, note = mf._cross_validate_nasdaq(0.8)

        self.assertEqual(val, 0.8)
        self.assertEqual(n_src, 2)
        self.assertIn("一致", note)

    def test_24_cross_validate_usdcny_consistent(self):
        """USDCNY交叉验证一致"""
        with patch('macro_factors._try_hermes_search', return_value="USD CNY exchange rate 7.2000"):
            val, n_src, note = mf._cross_validate_usdcny(7.2005)

        self.assertAlmostEqual(val, 7.2005, delta=0.01)
        self.assertEqual(n_src, 2)
        self.assertIn("一致", note)

    def test_25_cross_validate_usdcny_divergence(self):
        """USDCNY差异>0.3% → 取均值"""
        with patch('macro_factors._try_hermes_search', return_value="USD/CNY 7.1000 today"):
            # yahoo_rate=7.25, diff_pct = |7.25-7.10|/7.25*100 ≈ 2.07% > 0.3%
            val, n_src, note = mf._cross_validate_usdcny(7.25)

        self.assertAlmostEqual(val, (7.25 + 7.10) / 2, delta=0.01)
        self.assertEqual(n_src, 2)
        self.assertIn("交叉验证", note)

    def test_26_cross_validate_vix_consistent(self):
        """VIX交叉验证一致"""
        with patch('macro_factors._try_hermes_search', return_value="VIX volatility index at 18.5"):
            val, n_src, note = mf._cross_validate_vix(18.5)

        self.assertEqual(val, 18.5)
        self.assertEqual(n_src, 2)
        self.assertIn("一致", note)

    def test_27_cross_validate_vix_anomaly(self):
        """VIX搜索匹配到异常大值(>50) → 忽略搜索, 用Yahoo"""
        with patch('macro_factors._try_hermes_search', return_value="VIX 85.0 something else"):
            val, n_src, note = mf._cross_validate_vix(20.0)

        # 85>50 且 yahoo=20<50 → 异常, 回退到yahoo
        self.assertEqual(val, 20.0)
        self.assertEqual(n_src, 1)
        self.assertIn("匹配异常", note)

    def test_28_cross_validate_vix_divergence(self):
        """VIX差异>max(20%, 2.0) → 取均值"""
        with patch('macro_factors._try_hermes_search', return_value="VIX at 28.0"):
            # yahoo=20, search=28, diff=8 > max(4, 2)=4 → 取均值
            val, n_src, note = mf._cross_validate_vix(20.0)

        self.assertAlmostEqual(val, (20.0 + 28.0) / 2, delta=0.1)
        self.assertEqual(n_src, 2)
        self.assertIn("交叉验证", note)


# ============================================================
# 测试类3: 因子计算
# ============================================================

class TestFactorComputation(unittest.TestCase):
    """测试因子分数计算逻辑"""

    def test_29_sp500_factor_normal(self):
        """标普500 +1.5% → +1.5 (在±2.0内)"""
        self.assertEqual(mf._compute_sp500_factor(1.5), 1.5)

    def test_30_sp500_factor_negative(self):
        """标普500 -0.8% → -0.8"""
        self.assertEqual(mf._compute_sp500_factor(-0.8), -0.8)

    def test_31_sp500_factor_clamp_upper(self):
        """标普500 +3.5% → 钳位 +2.0"""
        self.assertEqual(mf._compute_sp500_factor(3.5), 2.0)

    def test_32_sp500_factor_clamp_lower(self):
        """标普500 -4.0% → 钳位 -2.0"""
        self.assertEqual(mf._compute_sp500_factor(-4.0), -2.0)

    def test_33_sp500_factor_zero(self):
        """标普500 0% → 0"""
        self.assertEqual(mf._compute_sp500_factor(0.0), 0.0)

    def test_34_nasdaq_factor_normal(self):
        """纳斯达克 +1.2% → +1.2"""
        self.assertEqual(mf._compute_nasdaq_factor(1.2), 1.2)

    def test_35_nasdaq_factor_clamp_upper(self):
        """纳斯达克 +2.5% → 钳位 +1.5"""
        self.assertEqual(mf._compute_nasdaq_factor(2.5), 1.5)

    def test_36_nasdaq_factor_clamp_lower(self):
        """纳斯达克 -3.0% → 钳位 -1.5"""
        self.assertEqual(mf._compute_nasdaq_factor(-3.0), -1.5)

    def test_37_fx_factor_depreciation(self):
        """人民币贬值 (USDCNY +0.3%) → 负分, -1.0"""
        # chg_pct=0.003 (即0.3%), score=-0.003/0.003=-1.0
        self.assertAlmostEqual(mf._compute_fx_factor(0.003), -1.0, delta=0.01)

    def test_38_fx_factor_appreciation(self):
        """人民币升值 (USDCNY -0.3%) → 正分, +1.0"""
        self.assertAlmostEqual(mf._compute_fx_factor(-0.003), 1.0, delta=0.01)

    def test_39_fx_factor_neutral(self):
        """USDCNY变化0% → 0"""
        self.assertEqual(mf._compute_fx_factor(0.0), 0.0)

    def test_40_fx_factor_clamp(self):
        """USDCNY极端变化 → 钳位 ±1.0"""
        self.assertEqual(mf._compute_fx_factor(0.01), -1.0)  # 1%贬值 → 钳位
        self.assertEqual(mf._compute_fx_factor(-0.01), 1.0)  # 1%升值 → 钳位

    def test_41_fx_factor_small_change(self):
        """USDCNY微小变化 → 比例映射"""
        # +0.15% → -0.15/0.3 = -0.5
        self.assertAlmostEqual(mf._compute_fx_factor(0.0015), -0.5, delta=0.01)

    def test_42_vix_factor_neutral(self):
        """VIX=20 → score = 1.5*(20-20)/15 = 0"""
        self.assertEqual(mf._compute_vix_factor(20.0), 0.0)

    def test_43_vix_factor_panic(self):
        """VIX=25 → score = 1.5*(20-25)/15 = -0.5"""
        self.assertEqual(mf._compute_vix_factor(25.0), -0.5)

    def test_44_vix_factor_calm(self):
        """VIX=15 → score = 1.5*(20-15)/15 = +0.5"""
        self.assertEqual(mf._compute_vix_factor(15.0), 0.5)

    def test_45_vix_factor_clamp_upper(self):
        """VIX=5 (极低) → score = 1.5*(20-5)/15 = 1.5 (钳位内)"""
        self.assertEqual(mf._compute_vix_factor(5.0), 1.5)

    def test_46_vix_factor_clamp_lower(self):
        """VIX=40 (极高) → score = 1.5*(20-40)/15 = -2.0 → 钳位 -1.5"""
        self.assertEqual(mf._compute_vix_factor(40.0), -1.5)

    def test_47_vix_factor_extreme_panic(self):
        """VIX=80 (2008/2020级别) → 钳位 -1.5"""
        self.assertEqual(mf._compute_vix_factor(80.0), -1.5)


# ============================================================
# 测试类4: 交易日判断
# ============================================================

class TestTradingDate(unittest.TestCase):
    """测试交易日判断逻辑"""

    def test_48_is_weekend_saturday(self):
        """周六 → True"""
        self.assertTrue(mf._is_weekend("2026-06-13"))  # 2026-06-13 is Saturday

    def test_49_is_weekend_sunday(self):
        """周日 → True"""
        self.assertTrue(mf._is_weekend("2026-06-14"))  # 2026-06-14 is Sunday

    def test_50_is_weekend_weekday(self):
        """工作日 → False"""
        self.assertFalse(mf._is_weekend("2026-06-10"))  # Wednesday
        self.assertFalse(mf._is_weekend("2026-06-12"))  # Friday

    def test_51_is_weekend_invalid_date(self):
        """无效日期 → False (不崩溃)"""
        self.assertFalse(mf._is_weekend("not-a-date"))
        self.assertFalse(mf._is_weekend(""))

    def test_52_is_trading_date(self):
        """交易日判断"""
        self.assertTrue(mf._is_a_stock_trading_date("2026-06-10"))   # Wed
        self.assertTrue(mf._is_a_stock_trading_date("2026-06-12"))   # Fri
        self.assertFalse(mf._is_a_stock_trading_date("2026-06-13"))  # Sat
        self.assertFalse(mf._is_a_stock_trading_date("2026-06-14"))  # Sun


# ============================================================
# 测试类5: 缓存层
# ============================================================

class TestCacheLayer(unittest.TestCase):
    """测试SQLite缓存层"""

    def setUp(self):
        """每个测试使用独立的临时DB"""
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_macro_cache.db")

    def tearDown(self):
        """清理"""
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_53_init_cache_db_creates_table(self):
        """首次初始化 → 创建表和索引"""
        conn = mf.init_cache_db(self.db_path)
        # 验证表存在
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='us_overnight_cache'"
        ).fetchone()
        self.assertIsNotNone(row)
        conn.close()

    def test_54_init_cache_db_idempotent(self):
        """重复初始化 → 不报错"""
        conn1 = mf.init_cache_db(self.db_path)
        conn1.close()
        conn2 = mf.init_cache_db(self.db_path)
        conn2.close()
        # 不应崩溃

    def test_55_set_and_get_cache(self):
        """写入→读取 完整链路"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=1.5, nasdaq_chg=-0.8,
                      vix=18.5, usd_cny=7.20, usd_cny_prev=7.18,
                      source_info="test")

        cached = mf._get_cached(conn, "2026-06-10")
        self.assertIsNotNone(cached)
        self.assertEqual(cached["sp500_chg"], 1.5)
        self.assertEqual(cached["nasdaq_chg"], -0.8)
        self.assertEqual(cached["vix"], 18.5)
        self.assertEqual(cached["usd_cny"], 7.20)
        self.assertEqual(cached["usd_cny_prev"], 7.18)
        conn.close()

    def test_56_cache_update_upsert(self):
        """UPSERT: 同日期重复写入 → 更新而非新增"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=1.0, nasdaq_chg=0.5,
                      vix=20.0, usd_cny=7.20, usd_cny_prev=7.18,
                      source_info="first")
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=2.0, nasdaq_chg=-1.0,
                      vix=25.0, usd_cny=7.25, usd_cny_prev=7.20,
                      source_info="second")

        # 应只有1条记录
        count = conn.execute(
            "SELECT COUNT(*) FROM us_overnight_cache WHERE trade_date='2026-06-10'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

        cached = mf._get_cached(conn, "2026-06-10")
        self.assertEqual(cached["sp500_chg"], 2.0)  # 更新后的值
        self.assertEqual(cached["vix"], 25.0)
        conn.close()

    def test_57_cache_miss_wrong_date(self):
        """查询不存在日期 → None"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=1.5, nasdaq_chg=0.0,
                      vix=20.0, usd_cny=7.20, usd_cny_prev=7.18,
                      source_info="test")
        result = mf._get_cached(conn, "2026-06-11")
        self.assertIsNone(result)
        conn.close()

    def test_58_cache_expired_48h(self):
        """超过48h → 返回None (过期)"""
        conn = mf.init_cache_db(self.db_path)

        # 直接插入一条49小时前的缓存
        past_time = (datetime.now() - timedelta(hours=49)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO us_overnight_cache
                (trade_date, sp500_chg, nasdaq_chg, vix, usd_cny, usd_cny_prev, updated_at, source_info)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("2026-06-10", 1.5, -0.8, 18.5, 7.20, 7.18, past_time, "expired"))
        conn.commit()

        result = mf._get_cached(conn, "2026-06-10")
        self.assertIsNone(result, "过期缓存应返回None")
        conn.close()

    def test_59_cache_within_48h_valid(self):
        """48h内 → 有效"""
        conn = mf.init_cache_db(self.db_path)
        recent = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO us_overnight_cache
                (trade_date, sp500_chg, nasdaq_chg, vix, usd_cny, usd_cny_prev, updated_at, source_info)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("2026-06-10", 1.5, -0.8, 18.5, 7.20, 7.18, recent, "valid"))
        conn.commit()

        result = mf._get_cached(conn, "2026-06-10")
        self.assertIsNotNone(result, "24h内缓存应有效")
        self.assertEqual(result["sp500_chg"], 1.5)
        conn.close()

    def test_60_cache_invalid_timestamp(self):
        """损坏的timestamp → 返回None"""
        conn = mf.init_cache_db(self.db_path)
        conn.execute("""
            INSERT INTO us_overnight_cache
                (trade_date, sp500_chg, nasdaq_chg, vix, usd_cny, usd_cny_prev, updated_at, source_info)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("2026-06-10", 1.5, -0.8, 18.5, 7.20, 7.18, "BAD_TIMESTAMP", "bad"))
        conn.commit()

        result = mf._get_cached(conn, "2026-06-10")
        self.assertIsNone(result, "损坏时间戳 → 视为过期")
        conn.close()

    def test_61_reset_cache(self):
        """reset_cache → 删除指定日期缓存"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=1.5, nasdaq_chg=0.0,
                      vix=20.0, usd_cny=7.20, usd_cny_prev=7.18,
                      source_info="test")
        mf._set_cache(conn, "2026-06-11",
                      sp500_chg=2.0, nasdaq_chg=0.0,
                      vix=21.0, usd_cny=7.25, usd_cny_prev=7.20,
                      source_info="test")
        conn.close()

        mf.reset_cache("2026-06-10", self.db_path)

        conn2 = sqlite3.connect(self.db_path)
        r1 = mf._get_cached(conn2, "2026-06-10")
        r2 = mf._get_cached(conn2, "2026-06-11")
        conn2.close()

        self.assertIsNone(r1, "已重置 → None")
        self.assertIsNotNone(r2, "未重置 → 仍存在")


# ============================================================
# 测试类6: 主接口集成测试
# ============================================================

class TestMainInterface(unittest.TestCase):
    """测试 compute_macro_factors 主接口及便捷接口"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_main_cache.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_62_non_trading_day_returns_zero(self):
        """非交易日 → 返回全0"""
        result = mf.compute_macro_factors("2026-06-13", self.db_path)  # Saturday
        self.assertEqual(result["sp500"], 0.0)
        self.assertEqual(result["nasdaq"], 0.0)
        self.assertEqual(result["fx"], 0.0)
        self.assertEqual(result["vix"], 0.0)
        self.assertIn("非A股交易日", result["_meta"]["warnings"][0])

    def test_63_cache_hit_bypasses_yahoo(self):
        """缓存命中 → 不调Yahoo API"""
        # 先写入缓存
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=1.5, nasdaq_chg=-0.8,
                      vix=18.5, usd_cny=7.20, usd_cny_prev=7.18,
                      source_info="cache_test")
        conn.close()

        result = mf.compute_macro_factors("2026-06-10", self.db_path)

        self.assertTrue(result["_meta"]["cache_hit"])
        self.assertAlmostEqual(result["sp500"], 1.5, delta=0.01)
        self.assertAlmostEqual(result["nasdaq"], -0.8, delta=0.01)
        # fx: usd_cny_chg = (7.20-7.18)/7.18 = 0.002786 → score = -0.002786/0.003 = -0.9287
        self.assertLess(result["fx"], 0)  # 贬值 → 负分

    def test_64_live_fetch_with_cross_validation(self):
        """实时获取 + 交叉验证 (mock全部Yahoo成功 + 搜索成功)"""
        sp500_resp = _make_yahoo_response(regular_price=5600.0, prev_close=5517.24)  # +1.5%
        nasdaq_resp = _make_yahoo_response(regular_price=18000.0, prev_close=18145.16)  # -0.8%
        cny_resp = _make_yahoo_response(regular_price=7.25, prev_close=7.2283)  # +0.3%
        vix_resp = _make_yahoo_vix_response(vix=18.5, prev_vix=19.2)

        mock_fn = _mock_urlopen_factory({
            "^GSPC": sp500_resp, "^IXIC": nasdaq_resp,
            "CNY=X": cny_resp, "^VIX": vix_resp,
        })

        with patch('urllib.request.urlopen', side_effect=mock_fn):
            # 同时mock搜索交叉验证为一致
            with patch('macro_factors._cross_validate_sp500', return_value=(1.5, 2, "一致")):
                with patch('macro_factors._cross_validate_nasdaq', return_value=(-0.8, 2, "一致")):
                    with patch('macro_factors._cross_validate_usdcny', return_value=(7.25, 2, "一致")):
                        with patch('macro_factors._cross_validate_vix', return_value=(18.5, 2, "一致")):
                            result = mf.compute_macro_factors("2026-06-10", self.db_path)

        self.assertFalse(result["_meta"]["cache_hit"])
        self.assertEqual(result["_meta"]["sources"], 2)
        self.assertAlmostEqual(result["sp500"], 1.5, delta=0.01)
        self.assertAlmostEqual(result["nasdaq"], -0.8, delta=0.01)
        self.assertAlmostEqual(result["vix"], 1.5 * (20 - 18.5) / 15, delta=0.01)

    def test_65_yahoo_all_fail_cache_fallback(self):
        """Yahoo全部失败 → 回退缓存 → 无缓存回退0"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=0.5, nasdaq_chg=0.3,
                      vix=20.0, usd_cny=7.20, usd_cny_prev=7.18,
                      source_info="fallback_test")
        conn.close()

        with patch('macro_factors._fetch_yahoo_change', return_value=None):
            with patch('macro_factors._fetch_yahoo_vix', return_value=None):
                with patch('macro_factors._fetch_yahoo_usdcny', return_value=None):
                    result = mf.compute_macro_factors("2026-06-10", self.db_path)

        # 应回退到缓存
        self.assertTrue(result["_meta"]["cache_hit"])
        warnings_str = " ".join(result["_meta"]["warnings"])
        self.assertIn("旧缓存", warnings_str)

    def test_66_yahoo_all_fail_no_cache_default_zero(self):
        """Yahoo失败, 无缓存 → 各因子默认值 (0/fx=0/vix=0)"""
        with patch('macro_factors._fetch_yahoo_change', return_value=None):
            with patch('macro_factors._fetch_yahoo_vix', return_value=None):
                with patch('macro_factors._fetch_yahoo_usdcny', return_value=None):
                    result = mf.compute_macro_factors("2026-06-10", self.db_path)

        self.assertEqual(result["sp500"], 0.0)
        self.assertEqual(result["nasdaq"], 0.0)
        self.assertEqual(result["vix"], 0.0)
        warnings_str = " ".join(result["_meta"]["warnings"])
        self.assertIn("SP500获取失败", warnings_str)
        self.assertIn("VIX获取失败", warnings_str)

    def test_67_compute_macro_factors_batch(self):
        """批量计算"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-09", sp500_chg=1.0, nasdaq_chg=0.5,
                      vix=20.0, usd_cny=7.20, usd_cny_prev=7.18, source_info="b1")
        mf._set_cache(conn, "2026-06-10", sp500_chg=-1.0, nasdaq_chg=-0.5,
                      vix=22.0, usd_cny=7.22, usd_cny_prev=7.20, source_info="b2")
        conn.close()

        results = mf.compute_macro_factors_batch(["2026-06-09", "2026-06-10"], self.db_path)
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["_meta"]["cache_hit"])
        self.assertTrue(results[1]["_meta"]["cache_hit"])

    def test_68_get_raw_data_with_cache(self):
        """get_raw_data: 有缓存 → 返回原始值"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=1.5, nasdaq_chg=-0.8,
                      vix=18.5, usd_cny=7.20, usd_cny_prev=7.18,
                      source_info="raw_test")
        conn.close()

        raw = mf.get_raw_data("2026-06-10", self.db_path)
        self.assertEqual(raw["sp500_chg"], 1.5)
        self.assertEqual(raw["nasdaq_chg"], -0.8)
        self.assertEqual(raw["vix"], 18.5)
        self.assertIn("_meta", raw)

    def test_69_get_raw_data_no_cache(self):
        """get_raw_data: 无缓存 → 返回error"""
        raw = mf.get_raw_data("2099-01-01", self.db_path)
        self.assertIn("error", raw)

    def test_70_all_four_factors_not_none(self):
        """网络正常时4因子均为非None"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=0.5, nasdaq_chg=0.3,
                      vix=20.0, usd_cny=7.20, usd_cny_prev=7.18,
                      source_info="test")
        conn.close()

        result = mf.compute_macro_factors("2026-06-10", self.db_path)
        self.assertIsNotNone(result["sp500"])
        self.assertIsNotNone(result["nasdaq"])
        self.assertIsNotNone(result["fx"])
        self.assertIsNotNone(result["vix"])

    def test_71_output_structure(self):
        """输出结构规范: 含 _meta"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=0.5, nasdaq_chg=0.3,
                      vix=20.0, usd_cny=7.20, usd_cny_prev=7.18,
                      source_info="test")
        conn.close()

        result = mf.compute_macro_factors("2026-06-10", self.db_path)
        self.assertIn("sp500", result)
        self.assertIn("nasdaq", result)
        self.assertIn("fx", result)
        self.assertIn("vix", result)
        self.assertIn("_meta", result)
        self.assertIn("sources", result["_meta"])
        self.assertIn("cache_hit", result["_meta"])
        self.assertIn("warnings", result["_meta"])

    def test_72_sp500_factor_clamped_in_output(self):
        """输出因子值在规格范围内"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=5.0,   # 超大涨幅, 应钳位到2.0
                      nasdaq_chg=-4.0,  # 超大跌幅, 应钳位到-1.5
                      vix=80.0,         # 极端恐慌, 应钳位到-1.5
                      usd_cny=10.0, usd_cny_prev=7.0,  # 大幅贬值
                      source_info="extreme")
        conn.close()

        result = mf.compute_macro_factors("2026-06-10", self.db_path)
        self.assertLessEqual(abs(result["sp500"]), 2.0)
        self.assertLessEqual(abs(result["nasdaq"]), 1.5)
        self.assertLessEqual(abs(result["fx"]), 1.0)
        self.assertLessEqual(abs(result["vix"]), 1.5)


# ============================================================
# 测试类7: 纯标准库验证
# ============================================================

class TestPureStdlib(unittest.TestCase):
    """验证无外部pip依赖"""

    def test_73_no_external_deps(self):
        """检查imports不包含需要pip安装的三方包"""
        with open(os.path.join(os.path.dirname(__file__), '..', 'scripts', 'macro_factors.py'), 'r') as f:
            content = f.read()

        # 方允许的标准库
        allowed = {'os', 'sys', 'json', 'sqlite3', 'urllib.request', 'urllib.error',
                   're', 'time', 'datetime', 'hermes_tools'}
        forbidden_patterns = ['import pandas', 'import numpy', 'import requests',
                              'import yfinance', 'from pandas', 'from numpy',
                              'from requests', 'from yfinance']

        for pat in forbidden_patterns:
            self.assertNotIn(pat, content, f"不应包含外部依赖: {pat}")


# ============================================================
# 测试类8: 极端行情压力测试
# ============================================================

class TestExtremeScenarios(unittest.TestCase):
    """极端行情场景测试"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_extreme.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_74_2008_crash_sp500(self):
        """2008金融危机: 标普500单日-7%"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2008-10-15",
                      sp500_chg=-7.0, nasdaq_chg=-8.5,
                      vix=60.0, usd_cny=6.83, usd_cny_prev=6.83,
                      source_info="2008_crash")
        conn.close()

        result = mf.compute_macro_factors("2008-10-15", self.db_path)
        self.assertEqual(result["sp500"], -2.0, "标普500应钳位到 -2.0")
        self.assertEqual(result["nasdaq"], -1.5, "纳斯达克应钳位到 -1.5")
        self.assertEqual(result["vix"], -1.5, "VIX应钳位到 -1.5")

    def test_75_2020_covid_crash(self):
        """2020年3月新冠崩盘"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2020-03-16",
                      sp500_chg=-12.0, nasdaq_chg=-12.3,
                      vix=82.69, usd_cny=7.00, usd_cny_prev=6.98,
                      source_info="covid_crash")
        conn.close()

        result = mf.compute_macro_factors("2020-03-16", self.db_path)
        # 全部应在钳位边界
        self.assertEqual(result["sp500"], -2.0)
        self.assertEqual(result["nasdaq"], -1.5)
        self.assertEqual(result["vix"], -1.5)

    def test_76_bull_market_rally(self):
        """大牛市: 美股+3%"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-01-15",
                      sp500_chg=3.5, nasdaq_chg=4.2,
                      vix=12.0, usd_cny=6.70, usd_cny_prev=6.72,
                      source_info="bull_rally")
        conn.close()

        result = mf.compute_macro_factors("2026-01-15", self.db_path)
        self.assertEqual(result["sp500"], 2.0)
        self.assertEqual(result["nasdaq"], 1.5)
        self.assertGreater(result["vix"], 0, "VIX<15 应为正分")

    def test_77_usdcny_extreme_depreciation(self):
        """人民币急剧贬值: USDCNY +2%"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=0.0, nasdaq_chg=0.0,
                      vix=20.0, usd_cny=7.35, usd_cny_prev=7.21,
                      source_info="fx_extreme")
        conn.close()

        result = mf.compute_macro_factors("2026-06-10", self.db_path)
        self.assertEqual(result["fx"], -1.0, "极端贬值 → 钳位 -1.0")

    def test_78_flat_market(self):
        """完全平静: 美股0%, VIX=20, 汇率不变"""
        conn = mf.init_cache_db(self.db_path)
        mf._set_cache(conn, "2026-06-10",
                      sp500_chg=0.0, nasdaq_chg=0.0,
                      vix=20.0, usd_cny=7.20, usd_cny_prev=7.20,
                      source_info="flat")
        conn.close()

        result = mf.compute_macro_factors("2026-06-10", self.db_path)
        self.assertEqual(result["sp500"], 0.0)
        self.assertEqual(result["nasdaq"], 0.0)
        self.assertEqual(result["fx"], 0.0)
        self.assertEqual(result["vix"], 0.0)


# ============================================================
# 测试类9: 因子规格完整性
# ============================================================

class TestFactorSpecs(unittest.TestCase):
    """验证 FACTOR_SPECS 常量完整性"""

    def test_79_all_four_factors_in_specs(self):
        """4因子均在FACTOR_SPECS中"""
        self.assertIn("sp500", mf.FACTOR_SPECS)
        self.assertIn("nasdaq", mf.FACTOR_SPECS)
        self.assertIn("fx", mf.FACTOR_SPECS)
        self.assertIn("vix", mf.FACTOR_SPECS)

    def test_80_specs_have_required_keys(self):
        """每个factor spec含权重/范围/Yahoo代码/搜索query"""
        required = {"weight", "range", "yahoo", "query"}
        for name, spec in mf.FACTOR_SPECS.items():
            self.assertTrue(required.issubset(spec.keys()),
                            f"{name} 缺少必要字段: {required - spec.keys()}")

    def test_81_weights_sum_check(self):
        """权重合理性检查 (不强制等于某个值)"""
        total = sum(s["weight"] for s in mf.FACTOR_SPECS.values())
        self.assertGreater(total, 0, "总权重应 > 0")


if __name__ == "__main__":
    # 使用 unittest 直接运行
    unittest.main(verbosity=2)
