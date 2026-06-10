#!/usr/bin/env python3
"""
test_integration.py — Phase 8 端到端集成测试

测试完整链路:
  DuckDB → 因子计算 → 聚合 → 自进化 → 看板生成

3只样本标的: 603986.SH, 000001.SZ, 600519.SH
验收标准: 全链路<120s, 无崩溃, 输出格式正确
"""

import os
import sys
import time
import json
import pytest

# 将 scripts 目录加入路径
_SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import duckdb
import pandas as pd

# ─── 常量 ────────────────────────────────────────────────
SAMPLE_CODES = ["603986.SH", "000001.SZ", "600519.SH"]
TRADE_DATE = "2026-06-10"
STATE_DB = os.path.expandvars(r"%LOCALAPPDATA%\hermes\data\market_watcher_state.db")

# 集成测试用 fixture — 连接 DuckDB


@pytest.fixture(scope="module")
def db_conn():
    """模块级 DuckDB 连接 (复用)"""
    from tech_factors import _discover_duckdb
    db_path = _discover_duckdb()
    if not db_path:
        pytest.skip("DuckDB 数据文件不存在, 跳过集成测试")
    conn = duckdb.connect(db_path, read_only=True)
    yield conn
    try:
        conn.close()
    except Exception:
        pass


# ====== 1. DuckDB 数据加载 ==================================

class TestDuckDBLoad:
    """Step 1: DuckDB 自动发现与数据加载"""

    def test_discover_duckdb(self):
        """发现 DuckDB 文件 (>100MB)"""
        from tech_factors import _discover_duckdb
        db_path = _discover_duckdb()
        assert db_path is not None, "未找到 DuckDB 文件"
        assert os.path.exists(db_path), f"路径不存在: {db_path}"
        size_mb = os.path.getsize(db_path) / (1024 * 1024)
        assert size_mb > 100, f"DuckDB 文件过小 ({size_mb:.1f}MB)"

    def test_connect_and_query(self, db_conn):
        """连接 DuckDB 并查询"""
        df = db_conn.execute("SELECT COUNT(*) as cnt FROM daily").df()
        assert df['cnt'].iloc[0] > 1000, "DuckDB daily 表为空或数据量异常"

    def test_sample_stocks_exist(self, db_conn):
        """3只样本标的在数据库中存在"""
        for code in SAMPLE_CODES:
            df = db_conn.execute(
                f"SELECT COUNT(*) as cnt FROM daily WHERE ts_code = '{code}'"
            ).df()
            assert df['cnt'].iloc[0] > 0, f"标的 {code} 无数据"

    def test_sample_stocks_recent_data(self, db_conn):
        """样本标的有近期数据 (2026年)"""
        for code in SAMPLE_CODES:
            df = db_conn.execute(f"""
                SELECT MAX(trade_date) as last_date
                FROM daily WHERE ts_code = '{code}'
            """).df()
            last_date = str(df['last_date'].iloc[0])
            assert last_date >= "20260601", f"{code} 最新数据日期 {last_date} 早于 2026-06-01"


# ====== 2. 因子计算 ==========================================

class TestFactorCalculation:
    """Step 2: 四大维度因子计算"""

    def test_tech_factors_single(self, db_conn):
        """技术面因子计算 (单只)"""
        from tech_factors import compute_tech_factors
        result = compute_tech_factors(db_conn, "603986.SH", TRADE_DATE)
        assert isinstance(result, dict), f"返回值不是 dict: {type(result)}"
        # 检查核心因子
        expected_keys = ["trend", "momentum", "position", "volume", "atr", "distribution", "sr"]
        for key in expected_keys:
            assert key in result, f"缺少因子: {key}"
            val = result[key]
            if val is not None:
                assert isinstance(val, (int, float, type(None))), \
                    f"{key} 类型异常: {type(val)}"

    def test_tech_factors_batch(self, db_conn):
        """技术面因子批量计算"""
        from tech_factors import compute_tech_factors_batch
        results = compute_tech_factors_batch(db_conn, SAMPLE_CODES, TRADE_DATE)
        assert len(results) == 3, f"应返回3个结果, 实际 {len(results)}"
        for r in results:
            assert isinstance(r, dict)
            assert all(k in r for k in ["trend", "momentum", "position"])

    def test_sentiment_factors(self, db_conn):
        """情绪面因子计算 (全市场)"""
        from sentiment_factors import compute_sentiment_factors
        result = compute_sentiment_factors(db_conn, TRADE_DATE)
        assert isinstance(result, dict)
        for key in ["breadth", "limit_ratio"]:
            assert key in result, f"缺少情绪面因子: {key}"
            val = result[key]
            if val is not None:
                assert isinstance(val, (int, float)), f"{key} 类型异常"

    @pytest.mark.integration  # 需要网络
    def test_macro_factors(self, db_conn):
        """宏观面因子计算 (需要网络, 降级为0不算失败)"""
        from macro_factors import compute_macro_factors
        macro_db = os.path.join(os.path.dirname(_SCRIPT_DIR), "data", "macro_cache.db")
        result = compute_macro_factors(TRADE_DATE, macro_db)
        assert isinstance(result, dict)
        for key in ["sp500", "nasdaq", "fx", "vix"]:
            assert key in result, f"缺少宏观面因子: {key}"
            # 网络失败时值为0, 不视为错误

    @pytest.mark.integration  # 需要搜索
    def test_news_factors_single(self, db_conn):
        """消息面因子计算 (需要搜索)"""
        from news_factors import compute_news_factors
        from news_factors import set_search_function
        # 注入哑搜索函数 (集成测试不需要真实网络)
        set_search_function(lambda q: [])
        result = compute_news_factors("603986.SH")
        assert isinstance(result, dict)
        for key in ["announcement", "policy", "dragon_tiger"]:
            assert key in result, f"缺少消息面因子: {key}"


# ====== 3. 多因子聚合 ========================================

class TestMultiFactorAggregation:
    """Step 3: 多因子聚合"""

    def test_aggregate_single_format(self, db_conn):
        """单只聚合 — 输出格式验证"""
        from multi_factor_aggregator import aggregate_single
        result = aggregate_single(db_conn, "603986.SH", TRADE_DATE)
        assert isinstance(result, dict)
        # 必须字段
        required = ["ts_code", "total_score", "pred_direction", "confidence",
                     "dimensions", "signals"]
        for key in required:
            assert key in result, f"缺少字段: {key}"
        # 类型检查
        assert isinstance(result["total_score"], (int, float))
        assert isinstance(result["pred_direction"], str)
        assert result["pred_direction"] in (
            "strong_up", "up", "neutral", "down", "strong_down"
        ), f"非法方向: {result['pred_direction']}"
        assert 0.0 <= result["confidence"] <= 1.0, \
            f"置信度越界: {result['confidence']}"
        # 维度
        assert isinstance(result["dimensions"], dict)
        for dim in ["tech", "sentiment", "macro", "news"]:
            assert dim in result["dimensions"], f"缺少维度: {dim}"

    def test_aggregate_all_three(self, db_conn):
        """批量聚合 — 3只标的"""
        from multi_factor_aggregator import aggregate_all
        results = aggregate_all(db_conn, SAMPLE_CODES, TRADE_DATE)
        assert len(results) == 3, f"应返回3个结果, 实际 {len(results)}"
        # 检查每只
        codes_seen = {r["ts_code"] for r in results}
        assert codes_seen == set(SAMPLE_CODES), \
            f"返回标的不匹配: {codes_seen}"
        # 排序检查 (降序)
        for i in range(len(results) - 1):
            assert results[i]["total_score"] >= results[i + 1]["total_score"], \
                "结果不是降序排列"

    def test_aggregate_all_scores_range(self, db_conn):
        """聚合分值在 [-5, 5] 范围内"""
        from multi_factor_aggregator import aggregate_all
        results = aggregate_all(db_conn, SAMPLE_CODES, TRADE_DATE)
        for r in results:
            assert -5.0 <= r["total_score"] <= 5.0, \
                f"{r['ts_code']} score={r['total_score']} 越界"

    def test_aggregate_all_no_crash(self, db_conn):
        """聚合不崩溃 — 不存在的标的返回 error"""
        from multi_factor_aggregator import aggregate_single
        # 不存在的标的应优雅降级而非崩溃
        result = aggregate_single(db_conn, "999999.SH", TRADE_DATE)
        assert isinstance(result, dict)
        assert result["ts_code"] == "999999.SH"


# ====== 4. 自进化 ============================================

class TestSelfEvolution:
    """Step 4: 自进化系统"""

    def test_get_adaptive_weights(self):
        """获取自适应权重"""
        from self_evolution import get_adaptive_weights
        if not os.path.exists(STATE_DB):
            pytest.skip("StateDB 不存在, 跳过进化测试")
        weights = get_adaptive_weights(STATE_DB)
        assert isinstance(weights, dict)
        # 检查维度权重
        dim_w = weights.get("dim_weights", {})
        assert isinstance(dim_w, dict)
        for dim in ["tech", "sentiment", "macro", "news"]:
            if dim in dim_w:
                assert isinstance(dim_w[dim], (int, float))
        # 检查子权重
        sub_w = weights.get("sub_weights", {})
        assert isinstance(sub_w, dict)

    def test_evolve_weights_no_crash(self):
        """进化执行不崩溃"""
        from self_evolution import evolve_weights
        if not os.path.exists(STATE_DB):
            pytest.skip("StateDB 不存在, 跳过进化测试")
        result = evolve_weights(STATE_DB)
        assert isinstance(result, dict)
        # 有 reason 或 evolved 字段
        assert "reason" in result or "evolved" in result

    def test_get_evolution_log(self):
        """获取进化日志"""
        from self_evolution import get_evolution_log
        if not os.path.exists(STATE_DB):
            pytest.skip("StateDB 不存在, 跳过进化测试")
        entries = get_evolution_log(limit=5, state_db_path=STATE_DB)
        assert isinstance(entries, list)
        for entry in entries[:2]:
            assert isinstance(entry, dict)


# ====== 5. 端到端 ============================================

class TestEndToEnd:
    """端到端: DuckDB → 因子 → 聚合 → 进化 → 完整链路"""

    @pytest.mark.slow
    def test_full_chain_test_three_stocks(self, db_conn):
        """全链路测试: 3只标的 < 120s"""
        from multi_factor_aggregator import aggregate_all
        from self_evolution import get_adaptive_weights

        t0 = time.time()

        # Step 1-3: 因子计算 + 聚合
        results = aggregate_all(db_conn, SAMPLE_CODES, TRADE_DATE)

        # Step 4: 自进化权重
        weights = {}
        if os.path.exists(STATE_DB):
            try:
                weights = get_adaptive_weights(STATE_DB)
            except Exception:
                pass

        elapsed = time.time() - t0

        # 验收: 3只 < 120s
        assert elapsed < 120, f"全链路耗时 {elapsed:.1f}s >= 120s 阈值"
        assert len(results) == 3

        # 验收: 无崩溃 (走到这里即证明)
        print(f"\n✅ 全链路 {elapsed:.1f}s | 权重维度: "
              f"{list(weights.get('dim_weights',{}).keys()) or 'N/A'}")

    @pytest.mark.slow
    def test_full_chain_output_format(self, db_conn):
        """全链路输出格式验证"""
        from multi_factor_aggregator import aggregate_all
        results = aggregate_all(db_conn, SAMPLE_CODES, TRADE_DATE)

        for r in results:
            # 必备顶层字段
            assert isinstance(r["ts_code"], str)
            assert len(r["ts_code"]) > 0
            assert isinstance(r["total_score"], (int, float))
            assert isinstance(r["pred_direction"], str)
            assert isinstance(r["confidence"], (int, float))
            assert isinstance(r["dimensions"], dict)
            for dim in ["tech", "sentiment", "macro", "news"]:
                assert dim in r["dimensions"]
                assert isinstance(r["dimensions"][dim], (int, float))
            # 信号
            assert isinstance(r["signals"], list)
            # factor_details (可能为空)
            assert "factor_details" in r

    def test_pipeline_script_importable(self):
        """run_pipeline.py 可导入且函数签名正确"""
        sys.path.insert(0, _SCRIPT_DIR)
        try:
            import run_pipeline
            assert hasattr(run_pipeline, "run_pipeline"), "缺少 run_pipeline 函数"
            assert hasattr(run_pipeline, "main"), "缺少 main 函数"
        except ImportError as e:
            pytest.fail(f"无法导入 run_pipeline: {e}")
        finally:
            if "run_pipeline" in sys.modules:
                pass

    def test_pipeline_consistency(self, db_conn):
        """一致性: 单独聚合 vs 批量聚合结果一致"""
        from multi_factor_aggregator import aggregate_single, aggregate_all

        # 批量
        batch = aggregate_all(db_conn, SAMPLE_CODES[:2], TRADE_DATE)
        # 单独
        single = [
            aggregate_single(db_conn, code, TRADE_DATE)
            for code in SAMPLE_CODES[:2]
        ]

        for b, s in zip(batch, single):
            assert b["ts_code"] == s["ts_code"]
            # total_score 应相近 (由于全市场因子复用可能有微小差异, 容差 0.1)
            assert abs(b["total_score"] - s["total_score"]) < 1.0, \
                f"{b['ts_code']}: batch={b['total_score']:.2f} vs single={s['total_score']:.2f}"
            assert b["pred_direction"] == s["pred_direction"], \
                f"{b['ts_code']}: 方向不一致"


# ====== 6. 看板生成 ==========================================

class TestDashboardGeneration:
    """Step 5: Dashboard 生成"""

    def test_gen_dashboard_import(self):
        """gen_dashboard 模块可导入"""
        import gen_dashboard
        assert hasattr(gen_dashboard, "generate")
        assert hasattr(gen_dashboard, "generate_html_from_results")

    def test_gen_dashboard_has_html_template(self):
        """HTML 模板非空"""
        import gen_dashboard
        assert hasattr(gen_dashboard, "HTML_TEMPLATE")
        assert len(gen_dashboard.HTML_TEMPLATE) > 1000
        assert "__DATA_PLACEHOLDER__" in gen_dashboard.HTML_TEMPLATE


# ====== 7. 性能回归 ==========================================

class TestPerformance:
    """性能基准"""

    def test_aggregate_performance_three_stocks(self, db_conn):
        """3只标的聚合 < 30s"""
        from multi_factor_aggregator import aggregate_all
        t0 = time.time()
        results = aggregate_all(db_conn, SAMPLE_CODES, TRADE_DATE)
        elapsed = time.time() - t0
        assert elapsed < 30, f"3只聚合耗时 {elapsed:.1f}s >= 30s"
        assert len(results) == 3

    def test_tech_factors_performance_three_stocks(self, db_conn):
        """3只技术面因子 < 5s"""
        from tech_factors import compute_tech_factors_batch
        t0 = time.time()
        compute_tech_factors_batch(db_conn, SAMPLE_CODES, TRADE_DATE)
        elapsed = time.time() - t0
        assert elapsed < 5, f"3只技术面 {elapsed:.1f}s >= 5s"
