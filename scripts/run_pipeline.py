#!/usr/bin/env python3
"""
run_pipeline.py — 端到端预测流水线 (Phase 8 集成)

Pipeline 顺序:
  DuckDB 加载 → 四大因子计算 → 多因子聚合 → 自进化权重 → Dashboard 生成 → 结果推送

用法:
  python scripts/run_pipeline.py --date 2026-06-10 --codes 603986.SH,000001.SZ,600519.SH
  python scripts/run_pipeline.py --all          # 全市场扫描 (耗时较长)
  python scripts/run_pipeline.py --date 2026-06-10 --all  # 指定日期全市场

约束:
  - 单步失败不中断整体流水线 (容错)
  - 每步记录耗时 + 结果摘要
  - 不修改已有模块核心逻辑 (仅集成调用)
  - Python: /c/Python314/python
"""

import argparse
import os
import sys
import time
import json
import traceback
from datetime import datetime

# 确保脚本目录在 path 中
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import duckdb
import pandas as pd

# ─── 常量 ────────────────────────────────────────────────
STATE_DB = os.path.expandvars(r"%LOCALAPPDATA%\hermes\data\market_watcher_state.db")

# ====== 步骤函数 ==========================================

def step_duckdb_load(args):
    """Step 1: DuckDB 数据加载 — 自动发现并连接"""
    t0 = time.time()
    from tech_factors import _discover_duckdb
    db_path = _discover_duckdb()
    if not db_path:
        raise FileNotFoundError("未找到 DuckDB 数据文件 (daily_adj_*.duckdb >100MB)")
    conn = duckdb.connect(db_path, read_only=True)
    elapsed = time.time() - t0
    print(f"[Step 1] DuckDB 加载完成 ({elapsed:.1f}s): {os.path.basename(db_path)}")
    return conn, db_path


def step_get_codes(conn, args):
    """获取要分析的标的列表"""
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        print(f"[初始化] 指定标的: {len(codes)} 只 ({', '.join(codes[:5])}{'...' if len(codes)>5 else ''})")
        return codes

    # --all: 从 DuckDB 获取所有主板标的 (排除 ST/BJ/688/300/8 开头)
    t0 = time.time()
    try:
        df = conn.execute("""
            SELECT DISTINCT ts_code FROM daily
            WHERE ts_code NOT LIKE '%ST%'
              AND ts_code NOT LIKE '8%'
              AND ts_code NOT LIKE '688%'
              AND ts_code NOT LIKE '300%'
              AND ts_code NOT LIKE 'BJ%'
        """).df()
        codes = sorted(df['ts_code'].tolist())
        elapsed = time.time() - t0
        print(f"[初始化] 全市场扫描: {len(codes)} 只标的 ({elapsed:.1f}s)")
        return codes
    except Exception as e:
        print(f"[警告] 获取全市场标列表失败: {e}, 使用默认3只")
        return ["603986.SH", "000001.SZ", "600519.SH"]


def step_factors(conn, codes, trade_date):
    """Step 2: 四大因子计算 — 调用各因子模块"""
    t0 = time.time()
    from sentiment_factors import compute_sentiment_factors
    from macro_factors import compute_macro_factors

    results = {
        "sentiment": {"status": "skipped", "data": {}, "error": None},
        "macro": {"status": "skipped", "data": {}, "error": None},
    }
    macro_db = os.path.join(os.path.dirname(SCRIPT_DIR), "data", "macro_cache.db")

    # 情绪面 (全市场统一)
    t_s = time.time()
    try:
        results["sentiment"]["data"] = compute_sentiment_factors(conn, trade_date)
        results["sentiment"]["status"] = "ok"
        print(f"  [情绪面] 完成 ({time.time()-t_s:.1f}s): breadth={results['sentiment']['data'].get('breadth','N/A'):.2f}")
    except Exception as e:
        results["sentiment"]["status"] = "failed"
        results["sentiment"]["error"] = str(e)
        print(f"  [情绪面] 失败 ({time.time()-t_s:.1f}s): {e}")

    # 宏观面 (全市场统一)
    t_m = time.time()
    try:
        results["macro"]["data"] = compute_macro_factors(trade_date, macro_db)
        results["macro"]["status"] = "ok"
        print(f"  [宏观面] 完成 ({time.time()-t_m:.1f}s): sp500={results['macro']['data'].get('sp500','N/A'):.2f}")
    except Exception as e:
        results["macro"]["status"] = "failed"
        results["macro"]["error"] = str(e)
        print(f"  [宏观面] 失败 ({time.time()-t_m:.1f}s): {e}")

    elapsed = time.time() - t0
    print(f"[Step 2] 因子计算完成 ({elapsed:.1f}s)")
    return results


def step_aggregate(conn, codes, trade_date):
    """Step 3: 多因子聚合 — 逐个标的计算综合评分"""
    t0 = time.time()
    from multi_factor_aggregator import aggregate_single
    from sentiment_factors import compute_sentiment_factors
    from macro_factors import compute_macro_factors
    import os as _os

    cache_dir = os.path.join(os.path.dirname(SCRIPT_DIR), "data", "cache")
    macro_db = os.path.join(os.path.dirname(SCRIPT_DIR), "data", "macro_cache.db")

    # 预计算全市场因子 (复用)
    sent_result = {}
    macro_result = {}
    try:
        sent_result = compute_sentiment_factors(conn, trade_date, cache_dir)
    except Exception:
        pass
    try:
        macro_result = compute_macro_factors(trade_date, macro_db)
    except Exception:
        pass

    results = []
    failed = 0
    for i, code in enumerate(codes):
        try:
            r = aggregate_single(conn, code, trade_date, cache_dir,
                                _sentiment_cache=sent_result,
                                _macro_cache=macro_result)
            results.append(r)
        except Exception as e:
            failed += 1
            results.append({"ts_code": code, "error": str(e), "total_score": 0.0,
                          "pred_direction": "error", "confidence": 0.0})
        # 进度 (每20只或最后)
        if (i + 1) % 20 == 0 or (i + 1) == len(codes):
            print(f"  [{i+1}/{len(codes)}] ...")

    # 按 score 降序
    results.sort(key=lambda x: x.get("total_score", 0), reverse=True)

    elapsed = time.time() - t0
    success = len(codes) - failed
    print(f"[Step 3] 聚合完成 ({elapsed:.1f}s): {success}/{len(codes)} 成功, {failed} 失败")
    return results


def step_evolution(args):
    """Step 4: 自进化权重 — 获取自适应权重 + 可选进化"""
    t0 = time.time()
    from self_evolution import get_adaptive_weights, evolve_weights, get_evolution_log

    # 获取当前权重
    weights = {}
    try:
        weights = get_adaptive_weights(STATE_DB)
        print(f"  [当前权重] dim={ {k: round(v,3) for k,v in weights.get('dim_weights',{}).items()} }")
    except Exception as e:
        print(f"  [当前权重] 获取失败: {e}")
        weights = {"dim_weights": {}, "sub_weights": {}, "market_regime": "unknown"}

    # 尝试进化 (如果距离上次进化≥7天)
    evo_result = None
    try:
        if os.path.exists(STATE_DB):
            evo_result = evolve_weights(STATE_DB)
            if evo_result.get("evolved"):
                print(f"  [进化] 已执行: {evo_result.get('changes', [])}")
            else:
                print(f"  [进化] 跳过: {evo_result.get('reason', 'unknown')}")
    except Exception as e:
        print(f"  [进化] 失败: {e}")

    # 获取进化日志
    evo_log = []
    try:
        if os.path.exists(STATE_DB):
            evo_log = get_evolution_log(limit=5, state_db_path=STATE_DB)
    except Exception as e:
        print(f"  [日志] 获取失败: {e}")

    elapsed = time.time() - t0
    print(f"[Step 4] 自进化完成 ({elapsed:.1f}s): regime={weights.get('market_regime','N/A')}")
    return weights, evo_log


def step_dashboard():
    """Step 5: Dashboard 生成"""
    t0 = time.time()
    try:
        import gen_dashboard
        gen_dashboard.generate()
        elapsed = time.time() - t0
        print(f"[Step 5] Dashboard 生成完成 ({elapsed:.1f}s)")
        return True
    except Exception as e:
        elapsed = time.time() - t0
        print(f"[Step 5] Dashboard 生成失败 ({elapsed:.1f}s): {e}")
        traceback.print_exc()
        return False


def step_notify(agg_results, weights, evo_log, total_elapsed):
    """Step 6: 结果推送 — 输出摘要"""
    print("\n" + "=" * 60)
    print("  流水线执行摘要")
    print("=" * 60)

    # TOP 多空
    top_long = [r for r in agg_results if r.get("pred_direction") in ("strong_up", "up")][:5]
    top_short = [r for r in agg_results if r.get("pred_direction") in ("strong_down", "down")][:5]

    print(f"\n🏆 TOP 看多 (前5):")
    for r in top_long:
        print(f"  {r['ts_code']:>12s}  score={r.get('total_score',0):+.2f}  "
              f"dir={r.get('pred_direction','?'):>10s}  conf={r.get('confidence',0):.0%}")

    print(f"\n📉 TOP 看空 (前5):")
    for r in top_short:
        print(f"  {r['ts_code']:>12s}  score={r.get('total_score',0):+.2f}  "
              f"dir={r.get('pred_direction','?'):>10s}  conf={r.get('confidence',0):.0%}")

    # 市场状态
    regime = weights.get("market_regime", "unknown")
    dim_w = weights.get("dim_weights", {})
    dim_w_str = ", ".join(f"{k}={v:.2f}" for k, v in dim_w.items()) if dim_w else "默认"

    print(f"\n📊 市场状态: {regime}  |  维度权重: {dim_w_str}")
    print(f"⏱️  总耗时: {total_elapsed:.1f}s")
    print("=" * 60)


# ====== 主流程 ============================================

def run_pipeline(args):
    """执行完整流水线，返回 (success: bool, summary: dict)"""
    total_t0 = time.time()
    step_results = {}

    # ── Step 1: DuckDB 加载 ──
    try:
        conn, db_path = step_duckdb_load(args)
        step_results["duckdb"] = {"status": "ok", "path": db_path}
    except Exception as e:
        print(f"\n❌ [Step 1] 致命错误: {e}")
        return False, {"error": str(e), "step": "duckdb"}

    # ── 获取交易日期 ──
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    print(f"\n📅 交易日期: {trade_date}")

    # ── 获取标的列表 ──
    try:
        codes = step_get_codes(conn, args)
    except Exception as e:
        print(f"[警告] 获取标列表失败: {e}")
        codes = ["603986.SH", "000001.SZ", "600519.SH"]

    # ── Step 2: 因子计算 ──
    factor_results = {"sentiment": {"status": "skipped"}, "macro": {"status": "skipped"}}
    try:
        factor_results = step_factors(conn, codes, trade_date)
        step_results["factors"] = {"status": "ok", "n_stocks": len(codes)}
    except Exception as e:
        print(f"[Step 2] 因子计算异常: {e}")
        step_results["factors"] = {"status": "partial", "error": str(e)}

    # ── Step 3: 多因子聚合 ──
    agg_results = []
    try:
        agg_results = step_aggregate(conn, codes, trade_date)
        step_results["aggregate"] = {"status": "ok", "n_results": len(agg_results)}
    except Exception as e:
        print(f"[Step 3] 聚合异常: {e}")
        traceback.print_exc()
        step_results["aggregate"] = {"status": "failed", "error": str(e)}

    # ── Step 4: 自进化 ──
    weights = {}
    evo_log = []
    try:
        weights, evo_log = step_evolution(args)
        step_results["evolution"] = {"status": "ok"}
    except Exception as e:
        print(f"[Step 4] 自进化异常: {e}")
        traceback.print_exc()
        step_results["evolution"] = {"status": "failed", "error": str(e)}

    # ── Step 5: Dashboard ──
    dash_ok = False
    try:
        dash_ok = step_dashboard()
        step_results["dashboard"] = {"status": "ok" if dash_ok else "failed"}
    except Exception as e:
        print(f"[Step 5] Dashboard 异常: {e}")
        step_results["dashboard"] = {"status": "failed", "error": str(e)}

    # ── Step 6: 结果推送 ──
    total_elapsed = time.time() - total_t0
    step_notify(agg_results, weights, evo_log, total_elapsed)

    # 关闭连接
    try:
        conn.close()
    except Exception:
        pass

    success = all(
        s.get("status") != "failed"
        for s in step_results.values()
        if isinstance(s, dict)
    )
    return success, step_results


def main():
    parser = argparse.ArgumentParser(
        description="端到端量化预测流水线 (Phase 8 集成)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/run_pipeline.py --date 2026-06-10 --codes 603986.SH,000001.SZ,600519.SH
  python scripts/run_pipeline.py --all
  python scripts/run_pipeline.py --date 2026-06-10 --all
        """)

    parser.add_argument("--date", default=None,
                        help="交易日期 YYYY-MM-DD (默认: 今天)")
    parser.add_argument("--codes", default=None,
                        help="标的代码逗号分隔, 如 '603986.SH,000001.SZ'")
    parser.add_argument("--all", action="store_true",
                        help="全市场扫描 (所有主板标的)")
    args = parser.parse_args()

    if not args.codes and not args.all:
        # 默认: 3只样本标的
        args.codes = "603986.SH,000001.SZ,600519.SH"
        print(f"[默认] 使用3只样本标的: {args.codes}")

    print("=" * 60)
    print("  A股量化预测流水线")
    print(f"  启动: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    success, summary = run_pipeline(args)

    if success:
        print("\n✅ 流水线执行完成")
        sys.exit(0)
    else:
        print(f"\n⚠️ 流水线部分步骤失败，详见上方日志")
        sys.exit(1)


if __name__ == "__main__":
    main()
