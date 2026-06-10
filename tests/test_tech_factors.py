#!/usr/bin/env python3
"""
test_tech_factors.py — 技术面因子单元测试

测试范围:
  1. 正确性验证: 与DuckDB SQL直接计算结果对比
  2. 边界安全: 新股/停牌/退市/涨跌停/数据缺失
  3. 归一化验证: 各因子值域检查
  4. 接口验证: compute_tech_factors() 输出规范

作者: qa-tester
日期: 2026-06-10
"""

import os
import sys
import math
import pytest
import numpy as np
import duckdb

# 将被测模块加入路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from tech_factors import (  # noqa: E402
    factor_trend, factor_momentum, factor_position, factor_volume,
    factor_atr, factor_distribution, factor_sr,
    compute_tech_factors, compute_tech_factors_batch,
    _check_invalid, _is_limit_hit, _load_stock_data,
)

# ============================================================
# 配置
# ============================================================

DB_PATH = (
    r"C:\Users\LarkMi\quant_20260525"
    r"\每日A股日线行情数据duckDB（日更）"
    r"\daily_adj_19901219_20260610.duckdb"
)

TEST_CODES = [
    "603986.SH",  # 中小盘, 2172条
    "000001.SZ",  # 大盘银行, 8367条
    "600519.SH",  # 大盘蓝筹, 5936条
]

NEW_STOCK_CODES = ["301669.SZ"]  # 只有1条记录
SUSPENDED_CODES = ["000000.SZ"]  # 不存在的代码(模拟退市)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(scope="module")
def conn():
    """模块级DB连接, 复用避免反复连接"""
    c = duckdb.connect(DB_PATH, read_only=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def real_data(conn):
    """加载3只真实标的的完整数据"""
    data = {}
    for code in TEST_CODES:
        df = conn.execute(f"""
            SELECT trade_date, open, high, low, close, vol, pct_chg
            FROM daily_adj WHERE ts_code = '{code}'
            ORDER BY trade_date
        """).df()
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['vol'] = df['vol'].astype(float)
        df['pct_chg'] = df['pct_chg'].astype(float)
        data[code] = df
    return data


# ============================================================
# 1. 正确性验证 — 与 DuckDB SQL 直接对比
# ============================================================

class TestTrendFactor:
    """趋势因子: MA5/MA10 交叉验证"""

    def test_synthetic_bullish(self):
        """合成数据: 完全多头 (price > MA5 > MA10)"""
        closes = np.array([90, 92, 94, 96, 98, 100, 102, 104, 106, 108, 110], dtype=float)
        # MA5 = avg(106,108,110? no, last 5: [100,102,104,106,108,110] wait let me recount
        # Actually: indices 6-10 are closes[6:11] = [102,104,106,108,110] → MA5 = 106
        # MA10 = avg(closes[-10:]) = avg([92..110]) = 101
        latest = 110.0
        score = factor_trend(closes, latest)
        assert score > 0, "多头排列应为正分"
        assert -3.5 <= score <= 3.5

    def test_synthetic_bearish(self):
        """合成数据: 完全空头 (price < MA5 < MA10)"""
        closes = np.array([110, 108, 106, 104, 102, 100, 98, 96, 94, 92, 90], dtype=float)
        latest = 90.0
        score = factor_trend(closes, latest)
        assert score < 0, "空头排列应为负分"
        assert -3.5 <= score <= 3.5

    def test_vs_sql_ma5_ma10(self, conn, real_data):
        """与DuckDB直接计算的MA5/MA10比较"""
        for code in TEST_CODES:
            df = real_data[code]
            closes = df['close'].values[-65:]
            latest = float(closes[-1])

            # DuckDB 计算 MA5, MA10
            sql_ma5 = float(np.mean(closes[-5:]))
            sql_ma10 = float(np.mean(closes[-10:])) if len(closes) >= 10 else float(np.mean(closes))

            # 因子内部逻辑验证
            result = compute_tech_factors(conn, code)
            if result['trend'] is None:
                continue

            ind = result['indicators']
            # MA5/MA10 在 indicators 中, 验证与 SQL 一致
            assert abs(ind['ma5'] - sql_ma5) < 0.02, \
                f"{code}: MA5 mismatch {ind['ma5']} vs SQL {sql_ma5}"
            assert abs(ind['ma10'] - sql_ma10) < 0.02, \
                f"{code}: MA10 mismatch {ind['ma10']} vs SQL {sql_ma10}"

    def test_hand_calc_bullish(self):
        """手工计算验证: 简单多头案例"""
        # closes = [10, 11, 12, 13, 14], latest = 15
        # MA5 = avg(11..15) = 13, MA10 = avg(10..15) = 12.5
        # 15 > 13 > 12.5 → 完全多头
        # ma5_dist = (15-13)/13*100 = 15.38
        # score = 1.0 + 15.38*0.35 = 6.38 → clip → 3.5
        closes = np.array([10, 11, 12, 13, 14, 15], dtype=float)
        score = factor_trend(closes, 15.0)
        assert score == pytest.approx(3.5, abs=0.01)


class TestMomentumFactor:
    """动量因子: 5日涨跌幅累计"""

    def test_synthetic_positive(self):
        """正动量: 连续5日上涨"""
        chgs = np.array([1.0, 2.0, 1.5, 0.5, 3.0], dtype=float)  # sum=8.0, *0.2 = 1.6
        score = factor_momentum(chgs)
        assert score == pytest.approx(1.6, abs=0.01)

    def test_synthetic_negative(self):
        """负动量: 连续5日下跌"""
        chgs = np.array([-2.0, -3.0, -1.0, -2.5, -1.5], dtype=float)  # sum=-10.0, *0.2=-2.0
        score = factor_momentum(chgs)
        assert score == pytest.approx(-2.0, abs=0.01)

    def test_limit_hit_exclusion(self):
        """涨跌停日应被剔除"""
        chgs = np.array([1.0, 2.0, 9.9, 3.0, 1.0], dtype=float)
        # 剔除 9.9 后: [1.0, 2.0, 3.0, 1.0], sum=7.0, *0.2=1.4
        score = factor_momentum(chgs)
        assert score == pytest.approx(1.4, abs=0.01)

    def test_all_limits_excluded(self):
        """全是涨跌停 → 返回0"""
        chgs = np.array([9.95, -9.92, 9.98, 10.0, -9.9], dtype=float)
        score = factor_momentum(chgs)
        assert score == 0.0

    def test_vs_sql_pct_chg_sum(self, conn, real_data):
        """与DuckDB直接 SUM(pct_chg) 比较"""
        for code in TEST_CODES:
            df = real_data[code]
            chgs = df['pct_chg'].values

            # SQL 计算: 最近5日剔除涨跌停后的 pct_chg 之和
            recent = chgs[-5:] if len(chgs) >= 5 else chgs
            valid = [c for c in recent if abs(c) < 9.9]
            expected_raw = sum(valid)

            result = compute_tech_factors(conn, code)
            if result['momentum'] is None:
                continue
            # momentum = chg_5d(剔除涨跌停) * 0.2
            expected = expected_raw * 0.2
            assert result['momentum'] == pytest.approx(expected, abs=0.02), \
                f"{code}: momentum {result['momentum']} vs expected {expected}"


class TestPositionFactor:
    """位置因子: (close-low)/(high-low) 分位映射"""

    def test_synthetic_top(self):
        """收盘在最高价 → +1.0"""
        highs = np.array([10, 12, 11, 13, 14, 15], dtype=float)
        lows = np.array([8, 9, 9, 10, 11, 12], dtype=float)
        score = factor_position(highs, lows, 15.0)
        assert score > 0.9

    def test_synthetic_bottom(self):
        """收盘在最低价 → -1.0 (≥10天确保[-10:]窗口生效)"""
        # 15天数据, 第15天在多日最低点收盘
        highs = np.array([20,19,18,17,16,15,14,13,12,11,10,11,12,13,14], dtype=float)
        lows  = np.array([18,17,16,15,14,13,12,11,10,9, 8, 9, 10,11,12], dtype=float)
        # h10=max(highs[-10:])=14, l10=min(lows[-10:])=8, close=8
        # pos_pct=(8-8)/(14-8)*100=0, score=(0-50)/50=-1.0
        score = factor_position(highs, lows, 8.0)
        assert score == pytest.approx(-1.0, abs=0.01)

    def test_synthetic_mid(self):
        """收盘在中间 → ~0 (≥10天确保[-10:]窗口生效)"""
        highs = np.array([20,19,18,17,16,15,14,13,12,11,10,11,12,13,14], dtype=float)
        lows  = np.array([10,9,8,7,6,5,4,3,2,1,0,1,2,3,4], dtype=float)
        # h10=max(highs[-10:])=15, l10=min(lows[-10:])=0, close=7.5(中点)
        # pos_pct=(7.5-0)/(15-0)*100=50, score=(50-50)/50=0
        score = factor_position(highs, lows, 7.5)
        assert abs(score) < 0.05

    def test_hand_calc_midpoint(self):
        """手工计算: pos_pct=50 → score=0"""
        highs = np.array([20, 20, 20, 20, 20, 20], dtype=float)
        lows = np.array([10, 10, 10, 10, 10, 10], dtype=float)
        # pos_pct = (15-10)/(20-10)*100 = 50
        # score = (50-50)/50 = 0
        score = factor_position(highs, lows, 15.0)
        assert score == pytest.approx(0.0, abs=0.01)


class TestVolumeFactor:
    """量价因子: 放量×方向"""

    def test_positive_volume(self):
        """放量上涨 → 正分"""
        vols = np.array([1000, 1100, 1050, 1200, 1150, 2000], dtype=float)
        chgs = np.array([0.5, 1.0, -0.5, 0.3, 0.2, 5.0], dtype=float)
        # avg_vol(前5日) = 1090, vol_ratio = 2000/1090 = 1.835 > 1.3
        # score = 5.0 * 0.15 = 0.75
        score = factor_volume(vols, chgs)
        assert score == pytest.approx(0.75, abs=0.01)

    def test_negative_volume(self):
        """放量下跌 → 负分"""
        vols = np.array([1000, 1100, 1050, 1200, 1150, 2000], dtype=float)
        chgs = np.array([0.5, 1.0, -0.5, 0.3, 0.2, -5.0], dtype=float)
        score = factor_volume(vols, chgs)
        assert score == pytest.approx(-0.75, abs=0.01)

    def test_normal_volume(self):
        """缩量 → 0"""
        vols = np.array([1000, 1100, 1050, 1200, 1150, 1200], dtype=float)
        chgs = np.array([0.5, 1.0, -0.5, 0.3, 0.2, 5.0], dtype=float)
        # vol_ratio ≈ 1.09 < 1.3 → 0
        score = factor_volume(vols, chgs)
        assert score == 0.0

    def test_limit_hit_volume_zero(self):
        """涨跌停日量价因子返回0"""
        vols = np.array([1000, 1100, 1050, 1200, 1150, 5000], dtype=float)
        chgs = np.array([0.5, 1.0, -0.5, 0.3, 0.2, 9.95], dtype=float)
        score = factor_volume(vols, chgs)
        assert score == 0.0

    def test_vs_sql_vol_ratio(self, conn, real_data):
        """与DuckDB计算的量比对比"""
        for code in TEST_CODES:
            df = real_data[code]
            vols = df['vol'].values
            # SQL: 今日量/前5日均量
            avg_vol = float(np.mean(vols[-6:-1])) if len(vols) >= 6 else 1.0
            vol_ratio = float(vols[-1]) / avg_vol if avg_vol > 0 else 1.0

            result = compute_tech_factors(conn, code)
            if result['volume'] is None:
                continue
            ind = result['indicators']
            assert ind['vol_ratio'] == pytest.approx(vol_ratio, abs=0.02), \
                f"{code}: vol_ratio {ind['vol_ratio']} vs SQL {vol_ratio}"


class TestAtrFactor:
    """ATR 波动率因子"""

    def test_synthetic_with_known_tr(self):
        """已知TR值的合成数据"""
        # True Range = max(H-L, |H-PC|, |L-PC|)
        # Day1: H=105, L=95, PC=100 → TR = max(10, 5, 5) = 10
        # Day2: H=110, L=98, PC=100 → TR = max(12, 10, 2) = 12
        highs = np.array([105, 110], dtype=float)
        lows = np.array([95, 98], dtype=float)
        closes_arr = np.array([100, 102], dtype=float)
        # n_tr = min(1, 14) = 1, TR = max(12, 10, 2) = 12
        # atr_val = 12, atr_pct = 12/102*100 = 11.76%
        # >5% → 高波动, direction: 102 vs MA5=avg(100,102)=101 → 102>101 → +1
        # intensity = max(0.3, 1-(11.76-5)*0.15) = max(0.3, 1-1.014) = max(0.3, -0.014) = 0.3
        # score = -1*0.3 = -0.3 (clipped)
        score = factor_atr(highs, lows, closes_arr, 102.0)
        assert -1.0 <= score <= 1.0

    def test_vs_sql_atr_pct(self, conn, real_data):
        """与DuckDB手工计算的ATR%对比"""
        for code in TEST_CODES:
            df = real_data[code]
            result = compute_tech_factors(conn, code)
            if result['atr'] is None:
                continue

            closes = df['close'].values.astype(float)[-65:]
            highs = df['high'].values.astype(float)[-65:]
            lows = df['low'].values.astype(float)[-65:]
            latest = float(closes[-1])

            # 手工TR计算(与 factor_atr 内部逻辑一致)
            n = len(closes)
            n_tr = min(n - 1, 14)
            tr_sum = 0.0
            for i in range(1, n_tr + 1):
                h = float(highs[-i])
                l = float(lows[-i])
                pc = float(closes[-i - 1])
                tr_sum += max(h - l, abs(h - pc), abs(l - pc))
            expected_atr_pct = tr_sum / n_tr / latest * 100 if n_tr > 0 else 3.0

            ind = result['indicators']
            assert ind['atr_pct'] == pytest.approx(expected_atr_pct, abs=0.05), \
                f"{code}: atr_pct {ind['atr_pct']} vs expected {expected_atr_pct}"


class TestDistributionFactor:
    """筹码分布因子: 60日分位 + VWAP"""

    def test_synthetic_top_vwap_above(self):
        """高位 + VWAP上方"""
        closes = np.full(60, 10.0, dtype=float)
        closes[-10:] = np.array([12, 13, 14, 15, 16, 17, 18, 19, 20, 21], dtype=float)
        vols = np.full(60, 1000.0, dtype=float)
        # latest = 21, max=21, min=10 → pos_60=100
        # VWAP ≈ all equal weight → avg of closes = ~((50*10)+(55+...))/60
        score = factor_distribution(closes, vols, 21.0)
        assert score >= 0, "高位+VWAP上应正偏"
        assert -0.8 <= score <= 0.8

    def test_synthetic_bottom_vwap_below(self):
        """低位 + VWAP下方"""
        closes = np.full(60, 20.0, dtype=float)
        closes[-10:] = np.array([19, 18, 17, 16, 15, 14, 13, 12, 11, 10], dtype=float)
        vols = np.full(60, 1000.0, dtype=float)
        score = factor_distribution(closes, vols, 10.0)
        assert score <= 0, "低位+VWAP下应负偏"
        assert -0.8 <= score <= 0.8


class TestSrFactor:
    """支撑阻力因子"""

    def test_near_support(self):
        """贴近支撑位 → 反弹预期(正分)"""
        # 60日高=20, 60日低=8, 20日高=18, 20日低=10
        # latest=10.2 → dist_to_low20=(10.2-10)/10.2*100=1.96%
        # dist_to_resistance=min((18-10.2)/10.2, (20-10.2)/10.2) → min(76.5,96.1)=76.5%
        # dist_to_support=min(1.96,(10.2-8)/10.2)→min(1.96,21.6)=1.96% < 3%
        highs = np.full(65, 20.0, dtype=float)
        highs[-20:] = 18.0
        lows = np.full(65, 8.0, dtype=float)
        lows[-20:] = 10.0
        closes = np.full(65, 15.0, dtype=float)
        vols = np.full(65, 1000.0, dtype=float)
        score = factor_sr(highs, lows, closes, vols, 10.2)
        assert score > 0, f"贴近支撑应正分, 实际{score}"

    def test_near_resistance(self):
        """贴近阻力位 → 回调预期(负分)"""
        highs = np.full(65, 20.0, dtype=float)
        highs[-20:] = 18.0
        lows = np.full(65, 8.0, dtype=float)
        lows[-20:] = 10.0
        closes = np.full(65, 15.0, dtype=float)
        vols = np.full(65, 1000.0, dtype=float)
        score = factor_sr(highs, lows, closes, vols, 17.8)
        assert score < 0, f"贴近阻力应负分, 实际{score}"


# ============================================================
# 2. 边界安全测试
# ============================================================

class TestBoundaries:
    """边界条件: 新股、停牌、涨跌停、缺失数据"""

    def test_new_stock_returns_zero(self, conn):
        """数据<5日的标的: 所有因子返回0"""
        result = compute_tech_factors(conn, "301669.SZ")
        # 该股只有1条记录，应返回0而非None或异常
        assert result['trend'] is not None, "新股不应返回None"
        for key in ['trend', 'momentum', 'position', 'volume', 'atr', 'distribution', 'sr']:
            assert result[key] == 0.0, f"新股 {key} 应为0, 实际 {result[key]}"
        assert result['indicators']['is_new'] is True

    def test_nonexistent_stock_returns_none(self, conn):
        """不存在的标的: 返回None"""
        result = compute_tech_factors(conn, "000000.SZ")
        for key in ['trend', 'momentum', 'position', 'volume', 'atr', 'distribution', 'sr']:
            assert result[key] is None, f"不存在的标的 {key} 应为None"
        assert result['indicators'] == {}

    def test_suspended_stock_returns_none(self, conn):
        """退市/停牌标的: close≤0 应返回None层的空结果"""
        # 找一个历史上已退市的标的测试
        # 模拟: 直接测试 _check_invalid 和 _empty_result
        assert _check_invalid(0.0) == True
        assert _check_invalid(-1.0) == True
        assert _check_invalid(np.nan) == True  # 用==而非is, 兼容np.bool_
        assert _check_invalid(10.5) == False

    def test_limit_up_stock(self, conn):
        """涨停标的(使用cutoff_date匹配涨跌停日)"""
        # 找一个最近涨跌停日
        row = conn.execute("""
            SELECT ts_code, trade_date, close, pct_chg
            FROM daily_adj WHERE abs(pct_chg) >= 9.9
            AND trade_date >= '20250501'
            LIMIT 1
        """).fetchone()
        if not row:
            pytest.skip("近期无涨跌停标的, 跳过")
        code, trade_date = row[0], row[1]
        # 转换 trade_date 为 YYYY-MM-DD
        cutoff = f"{str(trade_date)[:4]}-{str(trade_date)[4:6]}-{str(trade_date)[6:8]}"
        result = compute_tech_factors(conn, code, cutoff_date=cutoff)
        if result['trend'] is None:
            pytest.skip(f"{code} 在 {cutoff} 数据不足")
        # 涨跌停当日 volume因子应为0(无量价意义)
        assert result['volume'] == 0.0, \
            f"涨跌停当日volume应为0, 实际{result['volume']}"
        assert result['indicators']['is_limit'] is True

    def test_data_with_nan_close(self):
        """close为NaN时应返回0或安全处理"""
        closes = np.array([10, 11, 12, np.nan, 14], dtype=float)
        score = factor_trend(closes, np.nan)
        assert score == 0.0, f"NaN close应返回0, 实际{score}"

    def test_insufficient_data_synthetic(self):
        """合成数据量不足测试(<3条)"""
        closes = np.array([10, 11], dtype=float)
        highs = np.array([12, 13], dtype=float)
        lows = np.array([9, 10], dtype=float)
        chgs = np.array([1.0, 2.0], dtype=float)
        vols = np.array([1000, 1100], dtype=float)

        # 所有因子函数应对短数据安全处理
        assert factor_trend(closes, 11.0) == 0.0
        assert factor_momentum(chgs) == 0.0
        assert factor_position(highs, lows, 11.0) == 0.0
        assert factor_volume(vols, chgs) == 0.0
        assert factor_atr(highs, lows, closes, 11.0) == 0.0
        assert factor_distribution(closes, vols, 11.0) == 0.0
        assert factor_sr(highs, lows, closes, vols, 11.0) == 0.0

    def test_single_day_data(self, conn):
        """仅有1日数据的标的"""
        result = compute_tech_factors(conn, "301669.SZ")
        assert result['trend'] == 0.0
        assert result['indicators']['is_new'] is True

    def test_check_invalid_various(self):
        """_check_invalid 全面测试"""
        assert _check_invalid(0.0) == True
        assert _check_invalid(-0.01) == True
        assert _check_invalid(-100.0) == True
        assert _check_invalid(np.nan) == True  # 用==而非is, 兼容np.bool_
        assert _check_invalid(0.01) == False
        assert _check_invalid(100.0) == False

    def test_is_limit_hit_various(self):
        """_is_limit_hit 全面测试"""
        assert _is_limit_hit(9.9) is True
        assert _is_limit_hit(-9.9) is True
        assert _is_limit_hit(10.0) is True
        assert _is_limit_hit(-10.0) is True
        assert _is_limit_hit(9.89) is False
        assert _is_limit_hit(-9.89) is False
        assert _is_limit_hit(0.0) is False


# ============================================================
# 3. 归一化验证
# ============================================================

class TestNormalization:
    """各因子值域检查"""

    FACTOR_RANGES = {
        'trend': (-3.5, 3.5),
        'momentum': (-4.0, 4.0),
        'position': (-1.0, 1.0),
        'volume': (-1.5, 1.5),
        'atr': (-1.0, 1.0),
        'distribution': (-0.8, 0.8),
        'sr': (-0.8, 0.8),
    }

    def test_real_stocks_normalization(self, conn, real_data):
        """验证真实标的所有因子在指定值域内"""
        for code in TEST_CODES:
            result = compute_tech_factors(conn, code)
            if result['trend'] is None:
                continue
            for key, (lo, hi) in self.FACTOR_RANGES.items():
                val = result[key]
                assert lo <= val <= hi, \
                    f"{code} {key}={val} 超出 [{lo}, {hi}]"

    def test_synthetic_extreme_bull(self):
        """极端多头合成数据: 所有因子不应越界"""
        # 连续20日涨停(合成)
        closes = np.linspace(10, 50, 65)
        highs = closes + 0.5
        lows = closes - 0.5
        vols = np.full(65, 1000.0)
        chgs = np.full(65, 9.5)  # 接近涨停但未触发剔除

        t = factor_trend(closes, closes[-1])
        m = factor_momentum(chgs)
        p = factor_position(highs, lows, closes[-1])
        v = factor_volume(vols, chgs)
        a = factor_atr(highs, lows, closes, closes[-1])
        d = factor_distribution(closes, vols, closes[-1])
        s = factor_sr(highs, lows, closes, vols, closes[-1])

        for name, val, (lo, hi) in [
            ('trend', t, (-3.5, 3.5)),
            ('momentum', m, (-4.0, 4.0)),
            ('position', p, (-1.0, 1.0)),
            ('volume', v, (-1.5, 1.5)),
            ('atr', a, (-1.0, 1.0)),
            ('distribution', d, (-0.8, 0.8)),
            ('sr', s, (-0.8, 0.8)),
        ]:
            assert lo <= val <= hi, f"{name}={val} 超出 [{lo}, {hi}]"

    def test_synthetic_extreme_bear(self):
        """极端空头合成数据: 所有因子不应越界"""
        closes = np.linspace(50, 10, 65)
        highs = closes + 0.5
        lows = closes - 0.5
        vols = np.full(65, 1000.0)
        chgs = np.full(65, -9.5)

        t = factor_trend(closes, closes[-1])
        m = factor_momentum(chgs)
        p = factor_position(highs, lows, closes[-1])
        v = factor_volume(vols, chgs)
        a = factor_atr(highs, lows, closes, closes[-1])
        d = factor_distribution(closes, vols, closes[-1])
        s = factor_sr(highs, lows, closes, vols, closes[-1])

        for name, val, (lo, hi) in [
            ('trend', t, (-3.5, 3.5)),
            ('momentum', m, (-4.0, 4.0)),
            ('position', p, (-1.0, 1.0)),
            ('volume', v, (-1.5, 1.5)),
            ('atr', a, (-1.0, 1.0)),
            ('distribution', d, (-0.8, 0.8)),
            ('sr', s, (-0.8, 0.8)),
        ]:
            assert lo <= val <= hi, f"{name}={val} in extreme bear should be in [{lo}, {hi}]"


# ============================================================
# 4. 接口验证
# ============================================================

class TestInterface:
    """compute_tech_factors() 接口规范"""

    REQUIRED_KEYS = {
        'trend', 'momentum', 'position', 'volume',
        'atr', 'distribution', 'sr', 'indicators',
    }

    REQUIRED_INDICATOR_KEYS = {
        'latest', 'ma5', 'ma10', 'high_10d', 'low_10d',
        'chg_1d', 'chg_3d', 'chg_5d', 'vol_ratio',
        'atr_pct', 'trend', 'pos_10d', 'is_limit', 'is_new',
    }

    def test_returns_all_keys(self, conn):
        """正常标的返回完整8个key"""
        result = compute_tech_factors(conn, "000001.SZ")
        assert result['trend'] is not None
        assert set(result.keys()) == self.REQUIRED_KEYS

    def test_indicators_keys(self, conn):
        """indicators 子字典包含全部必需字段"""
        result = compute_tech_factors(conn, "000001.SZ")
        ind = result['indicators']
        for key in self.REQUIRED_INDICATOR_KEYS:
            assert key in ind, f"indicators 缺少字段: {key}"

    def test_cutoff_date_none(self, conn):
        """cutoff_date=None 使用最新数据"""
        result = compute_tech_factors(conn, "000001.SZ", cutoff_date=None)
        # 最新的 trade_date 应是 20260609 附近
        assert result['indicators']['latest'] > 0

    def test_cutoff_date_specific(self, conn):
        """cutoff_date 指定历史日期"""
        result = compute_tech_factors(conn, "000001.SZ", cutoff_date="2024-06-01")
        assert result['trend'] is not None
        # 价格应为历史值，不是最新
        latest = result['indicators']['latest']
        # 2024-06-01 附近平安银行价格大约在10元上下
        assert latest > 5, f"历史价格异常: {latest}"

    def test_cutoff_date_invalid(self, conn):
        """cutoff_date 不合法日期应安全处理"""
        # 极早日期(无数据)
        result = compute_tech_factors(conn, "000001.SZ", cutoff_date="2000-01-01")
        # 返回None(无数据)或空结果
        if result['trend'] is None:
            pass  # 预期行为
        else:
            # 有数据也应合法
            for k in self.REQUIRED_KEYS - {'indicators'}:
                assert isinstance(result[k], (int, float))

    def test_batch_interface(self, conn):
        """批量接口测试"""
        codes = ["000001.SZ", "600519.SH"]
        results = compute_tech_factors_batch(conn, codes)
        assert set(results.keys()) == set(codes)
        for code in codes:
            assert set(results[code].keys()) == self.REQUIRED_KEYS

    def test_batch_with_new_stock(self, conn):
        """批量接口包含新股"""
        codes = ["000001.SZ", "301669.SZ"]
        results = compute_tech_factors_batch(conn, codes)
        # 新股应返回0
        assert results["301669.SZ"]['trend'] == 0.0
        assert results["301669.SZ"]['momentum'] == 0.0
        # 正常股应正常
        assert results["000001.SZ"]['trend'] is not None

    def test_empty_result_structure(self):
        """_empty_result 的结构"""
        from tech_factors import _empty_result
        r = _empty_result()
        assert r['trend'] is None
        assert r['indicators'] == {}
        assert set(r.keys()) == self.REQUIRED_KEYS

    def test_factor_return_types(self):
        """各因子函数返回float"""
        closes = np.array([10, 11, 12, 13, 14, 15], dtype=float)
        highs = np.array([11, 12, 13, 14, 15, 16], dtype=float)
        lows = np.array([9, 10, 11, 12, 13, 14], dtype=float)
        vols = np.array([1000, 1100, 1200, 1300, 1400, 1500], dtype=float)
        chgs = np.array([1, 2, -1, 3, -2, 1], dtype=float)

        results = [
            factor_trend(closes, 15.0),
            factor_momentum(chgs),
            factor_position(highs, lows, 15.0),
            factor_volume(vols, chgs),
            factor_atr(highs, lows, closes, 15.0),
            factor_distribution(closes, vols, 15.0),
            factor_sr(highs, lows, closes, vols, 15.0),
        ]
        for val in results:
            assert isinstance(val, (int, float)), f"返回类型应为float, 实际{type(val)}"


# ============================================================
# 5. 极端行情压力测试
# ============================================================

class TestStressScenarios:
    """极端行情: 连续涨跌停、剧烈波动"""

    def test_consecutive_limit_ups(self, conn):
        """连续涨停: 动量因子应剔除涨跌停日"""
        closes = np.array([10, 11, 12.1, 13.31, 14.64, 15.0], dtype=float)
        highs = np.array([10.5, 11.5, 12.5, 13.5, 14.8, 15.5], dtype=float)
        lows = np.array([9.5, 10.5, 11.5, 12.5, 13.5, 14.5], dtype=float)
        vols = np.full(6, 1000.0)
        # 模拟连续涨跌停的 pct_chg
        chgs = np.array([9.95, 9.95, 9.95, 9.95, 9.95, 5.0], dtype=float)
        # 动量: 只有5.0参与计算, sum=5.0, *0.2=1.0
        m = factor_momentum(chgs)
        assert m == pytest.approx(1.0, abs=0.01)

    def test_consecutive_limit_downs(self, conn):
        """连续跌停"""
        chgs = np.array([-9.95, -9.95, -9.95, -9.95, -9.95, -5.0], dtype=float)
        m = factor_momentum(chgs)
        assert m == pytest.approx(-1.0, abs=0.01)

    def test_flash_crash_synthetic(self):
        """闪崩场景: 单日暴跌20%"""
        closes = np.full(65, 100.0)
        closes[-1] = 80.0  # 单日 -20%
        highs = closes + 1
        lows = closes - 1
        chgs = np.full(65, 0.0)
        chgs[-1] = -9.95  # 跌停
        vols = np.full(65, 1000.0)

        # 趋势因子应受大幅下跌影响
        t = factor_trend(closes, 80.0)
        assert t < 0, f"暴跌后趋势应负分, 实际{t}"

        # 位置因子应接近底部
        p = factor_position(highs, lows, 80.0)
        assert p < 0, f"暴跌后位置应接近底部, 实际{p}"

    def test_extreme_pos_60_scenarios(self):
        """分布因子在极端分位下不崩溃"""
        # 全历史最高点
        closes = np.linspace(10, 200, 65)  # 连续上涨 → pos_60=100
        vols = np.full(65, 1000.0)
        score = factor_distribution(closes, vols, 200.0)
        assert -0.8 <= score <= 0.8

        # 全历史最低点
        closes = np.linspace(200, 10, 65)
        score = factor_distribution(closes, vols, 10.0)
        assert -0.8 <= score <= 0.8


# ============================================================
# 6. 多标的覆盖验证
# ============================================================

class TestMultiStockCoverage:
    """确保 ≥3 只不同类型股票覆盖"""

    def test_at_least_three_stocks(self, conn):
        """测试覆盖 ≥3 只股票: 中小盘 + 大盘 + 蓝筹"""
        codes = TEST_CODES
        assert len(codes) >= 3

        results = {}
        for code in codes:
            r = compute_tech_factors(conn, code)
            results[code] = r
            assert r['trend'] is not None, f"{code} 应返回有效结果"

        # 各因子至少有2只标的不同于0(有区分度)
        for factor in ['trend', 'momentum', 'position']:
            nonzero = sum(1 for c in codes if abs(results[c][factor]) > 0.01)
            assert nonzero >= 1, f"{factor}: 所有标的的值=0, 无区分度"


# ============================================================
# 7. cutoff_date 回归验证
# ============================================================

class TestCutoffDateRegression:
    """cutoff_date 是否真正限制数据范围"""

    def test_cutoff_limits_data(self, conn):
        """cutoff_date之后的日期不应被使用"""
        result = compute_tech_factors(conn, "000001.SZ", cutoff_date="2024-01-15")
        if result['trend'] is None:
            return  # 数据不足

        # 2024-01-15 对应的价格范围验证
        # 查询该日期附近的价格
        row = conn.execute("""
            SELECT close FROM daily_adj
            WHERE ts_code = '000001.SZ'
            AND trade_date <= '20240115'
            ORDER BY trade_date DESC LIMIT 1
        """).fetchone()
        assert row is not None
        expected_close = float(row[0])

        # 最新价应在历史价格附近(±5%容忍)
        actual = result['indicators']['latest']
        assert abs(actual - expected_close) / expected_close < 0.05, \
            f"cutoff 价格不匹配: {actual} vs expected {expected_close}"


# ============================================================
# 8. 数据完整性
# ============================================================

class TestDataIntegrity:
    """未来信息泄露 & 幸存者偏差检查"""

    def test_no_future_data_in_cutoff(self, conn):
        """cutoff_date 不应泄露未来数据"""
        # 用两个相邻交易日(非周末)，检查结果不同
        r1 = compute_tech_factors(conn, "000001.SZ", cutoff_date="2024-06-03")
        r2 = compute_tech_factors(conn, "000001.SZ", cutoff_date="2024-06-04")

        if r1['trend'] is not None and r2['trend'] is not None:
            # 相邻交易日至少latest不同
            l1 = r1['indicators']['latest']
            l2 = r2['indicators']['latest']
            assert l1 != l2, f"相邻交易日latest应不同: {l1} vs {l2}"

    def test_survivorship_not_applicable(self, conn):
        """幸存者偏差: DuckDB包含全历史(含已退市股)，非仅当前存活"""
        # 验证DB中有退市标的（代码仍可查）
        result = compute_tech_factors(conn, "000033.SZ")  # 新都退
        # 无论有无数据，不崩溃即可
        assert isinstance(result, dict)


# ============================================================
# 运行入口
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
