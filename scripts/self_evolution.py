#!/usr/bin/env python3
"""
self_evolution.py — 自进化系统 (Phase 6)

实现因子权重自适应、因子休眠/激活、市场状态自适应三大机制。

核心接口:
  evolve_weights(state_db_path)         → 执行一次进化(权重微调±5%)
  get_adaptive_weights(state_db_path)   → 返回当前自适应权重
  get_evolution_log(limit=20)           → 返回最近进化日志

约束:
  - 权重调整保守(±5%以内)
  - 不删除休眠因子数据
  - 所有时间用 datetime('now','localtime')
  - 每次调整必须记录到 evolution_log
"""

import os
import sys
import json
import copy
import sqlite3
from datetime import datetime, timedelta

# ─── 默认权重 (对齐 multi_factor_aggregator.py) ───────────

DEFAULT_DIM_WEIGHTS = {
    "tech":      0.35,
    "sentiment": 0.25,
    "macro":     0.20,
    "news":      0.20,
}

DEFAULT_SUB_WEIGHTS = {
    "tech":      {"trend": 0.30, "momentum": 0.30, "position": 0.15,
                  "volume": 0.15, "atr": 0.05, "distribution": 0.03, "sr": 0.02},
    "sentiment": {"breadth": 0.8, "limit_ratio": 0.5, "northbound": 0.5, "margin": 0.3},
    "macro":     {"sp500": 0.8, "nasdaq": 0.5, "fx": 0.4, "vix": 0.5},
    "news":      {"announcement": 0.8, "policy": 0.5, "dragon_tiger": 0.5},
}

# 市场状态预设
MARKET_PRESETS = {
    "bull": {
        "dim_delta":   {"tech": +0.10, "news": -0.05},
        "sub_scale":   {"tech": {"trend": 1.10, "momentum": 1.10}},
        "description": "牛市: 趋势+动量权重+10%, 消息面-5%"
    },
    "bear": {
        "dim_delta":   {"macro": +0.10, "tech": -0.05},
        "sub_scale":   {},
        "description": "熊市: 宏观面+10%, 技术面-5%"
    },
    "choppy": {
        "dim_delta":   {},
        "sub_scale":   {},
        "description": "震荡市: 均权(0.25各维度)"
    },
}

# 进化间隔: 7天
EVOLVE_INTERVAL_DAYS = 7
# 休眠阈值: 连续20次预测准确率<30%
HIBERNATE_THRESHOLD = 0.30
HIBERNATE_WINDOW = 20
# 休眠因子重新评估间隔: 30天
HIBERNATE_REEVAL_DAYS = 30
# 单次权重调整最大幅度
MAX_ADJUST = 0.05


# ─── 数据库初始化 ────────────────────────────────────────

def _ensure_tables(conn: sqlite3.Connection):
    """确保 evolution_log / evolution_state 表存在且列完整"""
    # 主日志表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evolution_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT,
            event_desc  TEXT,
            details     TEXT,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # 兼容已有表(旧表有 description 列, 无 event_desc/details)
    for col, typ in [("event_desc", "TEXT"), ("details", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE evolution_log ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # 列已存在

    # 进化状态表: 存储当前权重 + 因子表现追踪
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evolution_state (
            key          TEXT PRIMARY KEY,   -- 'dim_weights' / 'sub_weights' / '{dim}.{factor}.perf' / 'last_evolve' / 'market_regime'
            value        TEXT,               -- JSON
            updated_at   TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()


def _read_state(conn: sqlite3.Connection, key: str):
    """读取 evolution_state 中的 JSON 值, 不存在返回 None"""
    row = conn.execute(
        "SELECT value FROM evolution_state WHERE key=?", (key,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def _write_state(conn: sqlite3.Connection, key: str, value):
    """写入 evolution_state, JSON 序列化"""
    conn.execute(
        "INSERT INTO evolution_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=datetime('now','localtime')",
        (key, json.dumps(value, ensure_ascii=False))
    )


def _log_event(conn: sqlite3.Connection, event_type: str, event_desc: str,
               details: dict = None):
    """写 evolution_log"""
    conn.execute(
        "INSERT INTO evolution_log (event_type, event_desc, details) "
        "VALUES (?, ?, ?)",
        (event_type, event_desc,
         json.dumps(details, ensure_ascii=False, default=str) if details else None)
    )
    conn.commit()


# ─── 权重初始化 ──────────────────────────────────────────

def _init_weights(conn: sqlite3.Connection):
    """如果 evolution_state 中没有权重, 用默认值初始化"""
    if _read_state(conn, "dim_weights") is None:
        _write_state(conn, "dim_weights", DEFAULT_DIM_WEIGHTS)
        _write_state(conn, "sub_weights", DEFAULT_SUB_WEIGHTS)
        _write_state(conn, "last_evolve", datetime.now().strftime("%Y-%m-%d"))
        _log_event(conn, "init", "自进化系统初始化: 加载默认权重",
                   {"dim_weights": DEFAULT_DIM_WEIGHTS,
                    "sub_weights": DEFAULT_SUB_WEIGHTS})


# ─── 准确率读取 ──────────────────────────────────────────

def _get_overall_accuracy(conn: sqlite3.Connection) -> dict:
    """从 stock_performance 汇总全量准确率"""
    row = conn.execute(
        "SELECT SUM(total_preds), SUM(correct_preds), "
        "       SUM(spot_total), SUM(spot_correct) "
        "FROM stock_performance"
    ).fetchone()
    trend_total = row[0] or 0
    trend_correct = row[1] or 0
    spot_total = row[2] or 0
    spot_correct = row[3] or 0

    return {
        "trend_accuracy": round(trend_correct / trend_total, 4) if trend_total > 0 else 0.5,
        "spot_accuracy":  round(spot_correct / spot_total, 4) if spot_total > 0 else 0.5,
        "trend_total": trend_total,
        "spot_total": spot_total,
    }


# ─── 市场状态 ────────────────────────────────────────────

def _detect_regime_from_state(conn: sqlite3.Connection) -> str:
    """从 evolution_state 读取上次存储的市场状态, 默认 choppy"""
    regime = _read_state(conn, "market_regime")
    if regime in ("bull", "bear", "choppy"):
        return regime
    return "choppy"


def set_market_regime(state_db_path: str, regime: str):
    """
    外部设置市场状态 (供 market_watcher 调用).

    Args:
        state_db_path: SQLite 状态库路径
        regime: 'bull' / 'bear' / 'choppy'
    """
    if regime not in ("bull", "bear", "choppy"):
        raise ValueError(f"regime 必须为 bull/bear/choppy, 收到: {regime}")
    conn = sqlite3.connect(state_db_path)
    _ensure_tables(conn)
    _write_state(conn, "market_regime", regime)
    conn.commit()
    conn.close()


# ─── 核心接口1: 权重进化 ─────────────────────────────────

def evolve_weights(state_db_path: str) -> dict:
    """
    执行一次权重进化。每7天可执行一次(内部保护)。

    步骤:
      1. 从 stock_performance 读取近期准确率
      2. 评估当前各维度/子因子表现
      3. ±5% 微调维度权重
      4. 检查子因子休眠条件 (连续20次<30%)
      5. 每月重新评估休眠因子
      6. 应用市场状态预设
      7. 记录所有变更到 evolution_log

    Returns:
        {"trend_accuracy": float, "adjustments": [...], "hibernations": [...]}
    """
    conn = sqlite3.connect(state_db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_tables(conn)
    _init_weights(conn)

    log_entries = []
    adjustments = []
    hibernations = []
    awakenings = []

    # ── 检查进化间隔 ──
    last_evolve = _read_state(conn, "last_evolve")
    today_str = datetime.now().strftime("%Y-%m-%d")
    if last_evolve:
        try:
            last_date = datetime.strptime(last_evolve, "%Y-%m-%d")
            days_since = (datetime.now() - last_date).days
            if days_since < EVOLVE_INTERVAL_DAYS:
                conn.close()
                return {
                    "skipped": True,
                    "reason": f"距上次进化仅{days_since}天 (<{EVOLVE_INTERVAL_DAYS}天), 跳过",
                    "adjustments": [],
                }
        except ValueError:
            pass

    # ── 1. 读取当前权重 ──
    dim_weights = _read_state(conn, "dim_weights") or dict(DEFAULT_DIM_WEIGHTS)
    sub_weights = _read_state(conn, "sub_weights") or dict(DEFAULT_SUB_WEIGHTS)

    # ── 2. 读取准确率 ──
    accuracy = _get_overall_accuracy(conn)
    trend_acc = accuracy["trend_accuracy"]
    spot_acc = accuracy["spot_accuracy"]

    # ── 3. 维度权重微调 ──
    # 策略: 如果准确率 > 60%, 小幅增加当前高权重维度; < 40%, 探索调整
    prev_acc = _read_state(conn, "prev_accuracy") or {"trend": 0.5, "spot": 0.5}
    dim_changes = {}

    if accuracy["trend_total"] >= 10:  # 足够样本才调整
        acc_delta = trend_acc - prev_acc.get("trend", 0.5)

        if trend_acc > 0.60:
            # 高准确率 → 强化技术面和情绪面 (historical best performers)
            dim_changes["tech"] = +0.03
            dim_changes["sentiment"] = +0.02
            dim_changes["news"] = -0.02
        elif trend_acc < 0.40:
            # 低准确率 → 向均衡靠拢
            dim_changes["tech"] = -0.02
            dim_changes["macro"] = +0.03
        # else: 维持不变

    # 应用调整 (clamp 到 ±MAX_ADJUST)
    for dim, delta in dim_changes.items():
        delta = max(-MAX_ADJUST, min(MAX_ADJUST, delta))
        if abs(delta) < 0.005:
            continue
        old_w = dim_weights.get(dim, 0.25)
        new_w = round(old_w + delta, 4)
        # 边界保护
        new_w = max(0.10, min(0.50, new_w))
        dim_weights[dim] = new_w
        adjustments.append({
            "dimension": dim,
            "old_weight": old_w,
            "new_weight": new_w,
            "delta": round(new_w - old_w, 4),
            "reason": f"趋势准确率={trend_acc:.1%}, Δ={acc_delta:+.1%}"
        })

    # ── 归一化维度权重 ──
    total = sum(dim_weights.values())
    if total > 0:
        for k in dim_weights:
            dim_weights[k] = round(dim_weights[k] / total, 4)

    # ── 4. 子因子休眠检查 ──
    for dim_name, factors in DEFAULT_SUB_WEIGHTS.items():
        for fname in factors:
            perf_key = f"{dim_name}.{fname}.perf"
            perf = _read_state(conn, perf_key) or {"hits": 0, "misses": 0,
                                                     "hibernating": False,
                                                     "hibernate_since": None}
            # 如果已休眠, 检查是否满30天需重新评估
            if perf.get("hibernating"):
                h_since = perf.get("hibernate_since")
                if h_since:
                    try:
                        h_date = datetime.strptime(h_since, "%Y-%m-%d")
                        if (datetime.now() - h_date).days >= HIBERNATE_REEVAL_DAYS:
                            # 重新激活评估
                            perf["hibernating"] = False
                            perf["hits"] = 0
                            perf["misses"] = 0
                            perf["hibernate_since"] = None
                            _write_state(conn, perf_key, perf)
                            awakenings.append(f"{dim_name}.{fname}")
                            _log_event(conn, "awaken",
                                       f"休眠因子 {dim_name}.{fname} 重激活 (每月重新评估)",
                                       {"factor": f"{dim_name}.{fname}"})
                    except ValueError:
                        pass
                continue

            # 非休眠因子: 检查是否需要休眠
            total_trials = perf.get("hits", 0) + perf.get("misses", 0)
            if total_trials >= HIBERNATE_WINDOW:
                factor_acc = perf["hits"] / total_trials if total_trials > 0 else 0
                if factor_acc < HIBERNATE_THRESHOLD:
                    perf["hibernating"] = True
                    perf["hibernate_since"] = today_str
                    _write_state(conn, perf_key, perf)
                    hibernations.append({
                        "factor": f"{dim_name}.{fname}",
                        "accuracy": round(factor_acc, 4),
                        "trials": total_trials,
                    })

    # ── 5. 市场状态自适应 (仅影响返回值, 不修改存储的基础权重) ──
    regime = _detect_regime_from_state(conn)

    # 保存基础权重 (不含市场预设, 供下次进化使用)
    base_dim_weights = dict(dim_weights)
    base_sub_weights = copy.deepcopy(sub_weights)

    # 在基础权重上应用市场预设 (用于返回值和日志)
    if regime == "choppy":
        for k in dim_weights:
            dim_weights[k] = 0.25
    else:
        preset = MARKET_PRESETS.get(regime, {})
        for dim, delta in preset.get("dim_delta", {}).items():
            if dim in dim_weights:
                new_w = round(dim_weights[dim] + delta, 4)
                dim_weights[dim] = max(0.05, min(0.55, new_w))
        for dim, scale_map in preset.get("sub_scale", {}).items():
            if dim in sub_weights:
                for fname, scale in scale_map.items():
                    if fname in sub_weights[dim]:
                        sub_weights[dim][fname] = round(
                            sub_weights[dim][fname] * scale, 4
                        )
    # 归一化
    total = sum(dim_weights.values())
    if total > 0:
        for k in dim_weights:
            dim_weights[k] = round(dim_weights[k] / total, 4)

    # ── 6. 持久化基础权重 (不含市场预设) ──
    _write_state(conn, "dim_weights", base_dim_weights)
    _write_state(conn, "sub_weights", base_sub_weights)
    _write_state(conn, "last_evolve", today_str)
    _write_state(conn, "prev_accuracy", {"trend": trend_acc, "spot": spot_acc})

    # ── 7. 记录日志 ──
    if adjustments:
        _log_event(conn, "weight_adjust", f"维度权重调整: {len(adjustments)}个维度变动",
                   {"adjustments": adjustments, "regime": regime,
                    "trend_accuracy": trend_acc})
    if hibernations:
        _log_event(conn, "hibernate", f"休眠因子: {len(hibernations)}个",
                   {"hibernations": hibernations})
    if awakenings:
        _log_event(conn, "awaken", f"重激活因子: {len(awakenings)}个",
                   {"awakenings": awakenings})

    if not adjustments and not hibernations and not awakenings:
        _log_event(conn, "no_change",
                   f"本轮无变更 (trend_acc={trend_acc:.1%}, regime={regime})",
                   {"trend_accuracy": trend_acc, "regime": regime})

    conn.commit()
    conn.close()

    return {
        "trend_accuracy": trend_acc,
        "spot_accuracy": spot_acc,
        "regime": regime,
        "adjustments": adjustments,
        "hibernations": hibernations,
        "awakenings": awakenings,
    }


# ─── 核心接口2: 获取自适应权重 ────────────────────────────

def get_adaptive_weights(state_db_path: str) -> dict:
    """
    返回当前自适应权重, 合并市场状态预设。

    Returns:
        {
            "dim_weights":   {"tech": 0.35, "sentiment": 0.25, ...},
            "sub_weights":   {"tech": {"trend": 0.30, ...}, ...},
            "hibernating":   ["tech.momentum", ...],   # 休眠因子列表
            "regime":        "choppy",
            "last_evolve":   "2026-06-10",
        }
    """
    conn = sqlite3.connect(state_db_path)
    _ensure_tables(conn)
    _init_weights(conn)

    dim_weights = _read_state(conn, "dim_weights") or dict(DEFAULT_DIM_WEIGHTS)
    sub_weights_raw = _read_state(conn, "sub_weights") or dict(DEFAULT_SUB_WEIGHTS)
    regime = _detect_regime_from_state(conn)
    last_evolve = _read_state(conn, "last_evolve") or "N/A"

    # 深拷贝子权重, 防止修改缓存
    sub_weights = copy.deepcopy(sub_weights_raw)

    # 实时应用市场状态预设
    if regime == "choppy":
        # 均权
        dim_weights_applied = {k: 0.25 for k in dim_weights}
    else:
        dim_weights_applied = dict(dim_weights)
        preset = MARKET_PRESETS.get(regime, {})
        for dim, delta in preset.get("dim_delta", {}).items():
            if dim in dim_weights_applied:
                old = dim_weights_applied[dim]
                dim_weights_applied[dim] = max(0.05, min(0.55, round(old + delta, 4)))
        for dim, scale_map in preset.get("sub_scale", {}).items():
            if dim in sub_weights:
                for fname, scale in scale_map.items():
                    if fname in sub_weights[dim]:
                        sub_weights[dim][fname] = round(
                            sub_weights[dim][fname] * scale, 4
                        )
        # 归一化
        total = sum(dim_weights_applied.values())
        if total > 0:
            for k in dim_weights_applied:
                dim_weights_applied[k] = round(dim_weights_applied[k] / total, 4)

    # 收集休眠因子
    hibernating = []
    for dim_name, factors in DEFAULT_SUB_WEIGHTS.items():
        for fname in factors:
            perf = _read_state(conn, f"{dim_name}.{fname}.perf")
            if perf and perf.get("hibernating"):
                hibernating.append(f"{dim_name}.{fname}")

    # 休眠因子权重置0但不删除
    adapted_sub = {}
    for dim_name, factors in DEFAULT_SUB_WEIGHTS.items():
        adapted_sub[dim_name] = {}
        for fname in factors:
            w = sub_weights.get(dim_name, {}).get(fname,
                                                   DEFAULT_SUB_WEIGHTS[dim_name][fname])
            if f"{dim_name}.{fname}" in hibernating:
                w = 0.0
            adapted_sub[dim_name][fname] = w

    conn.close()

    return {
        "dim_weights": dim_weights_applied,
        "sub_weights": adapted_sub,
        "hibernating": hibernating,
        "regime": regime,
        "last_evolve": last_evolve,
    }


# ─── 核心接口3: 进化日志 ─────────────────────────────────

def get_evolution_log(limit: int = 20, state_db_path: str = None) -> list:
    """
    返回最近N条进化日志。

    Args:
        limit: 最多返回条数
        state_db_path: 状态库路径。如果为 None, 使用环境变量或默认路径

    Returns:
        list[dict]: 每条日志包含 id, event_type, event_desc, details, created_at
    """
    if state_db_path is None:
        state_db_path = os.environ.get(
            "MARKET_WATCHER_STATE_DB",
            os.path.expandvars(
                r"%LOCALAPPDATA%\hermes\data\market_watcher_state.db"
            )
        )
    conn = sqlite3.connect(state_db_path)
    _ensure_tables(conn)

    # 兼容旧表: 优先读 event_desc, fallback 到 description
    try:
        rows = conn.execute(
            "SELECT id, event_type, event_desc, details, created_at "
            "FROM evolution_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        # 新格式: 5列 → id, event_type, event_desc, details, created_at
        has_details_col = True
    except sqlite3.OperationalError:
        # 旧表无 event_desc/details 列
        rows = conn.execute(
            "SELECT id, event_type, description, "
            "NULL, created_at "
            "FROM evolution_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        # 旧格式: 5列 → id, event_type, description, NULL, created_at
        has_details_col = False

    conn.close()

    result = []
    for row in rows:
        # row: [id, event_type, desc, details_or_null, created_at]
        desc_text = row[2] or ""
        created_at = row[4] if len(row) >= 5 else str(row[3])
        details = None
        if has_details_col and row[3]:
            try:
                details = json.loads(row[3])
            except (json.JSONDecodeError, TypeError):
                details = row[3]

        result.append({
            "id": row[0],
            "event_type": row[1],
            "event_desc": desc_text,
            "details": details,
            "created_at": created_at,
        })

    return result


# ─── 辅助: 更新因子表现 ──────────────────────────────────

def record_factor_performance(state_db_path: str,
                               factor_hits: dict[str, bool]):
    """
    记录因子在某次预测中的表现 (供外部回测/验证时调用)。

    Args:
        state_db_path: 状态库路径
        factor_hits: {factor_name: bool}, e.g.
            {"tech.trend": True, "tech.momentum": False, "sentiment.breadth": True}
    """
    conn = sqlite3.connect(state_db_path)
    _ensure_tables(conn)

    for fkey, is_correct in factor_hits.items():
        if "." not in fkey:
            continue
        perf_key = f"{fkey}.perf"
        perf = _read_state(conn, perf_key) or {"hits": 0, "misses": 0,
                                                 "hibernating": False,
                                                 "hibernate_since": None}
        if is_correct:
            perf["hits"] += 1
        else:
            perf["misses"] += 1
        _write_state(conn, perf_key, perf)

    conn.commit()
    conn.close()


# ─── CLI 自检 ────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="自进化系统 (Phase 6)")
    parser.add_argument("--state-db", default=None,
                        help="SQLite 状态库路径 (默认: %%LOCALAPPDATA%%\\hermes\\data\\market_watcher_state.db)")
    parser.add_argument("--evolve", action="store_true",
                        help="执行一次权重进化")
    parser.add_argument("--weights", action="store_true",
                        help="显示当前自适应权重")
    parser.add_argument("--log", type=int, default=0,
                        help="显示最近N条进化日志")
    parser.add_argument("--set-regime", choices=["bull", "bear", "choppy"],
                        help="设置市场状态")

    args = parser.parse_args()

    if args.state_db is None:
        args.state_db = os.path.expandvars(
            r"%LOCALAPPDATA%\hermes\data\market_watcher_state.db"
        )

    if not os.path.exists(args.state_db):
        print(f"X 状态库不存在: {args.state_db}")
        print("  提示: 先运行 market_watcher.py 初始化数据库")
        exit(1)

    if args.set_regime:
        set_market_regime(args.state_db, args.set_regime)
        print(f"市场状态已设为: {args.set_regime}")

    if args.evolve:
        print("=== 执行权重进化 ===")
        result = evolve_weights(args.state_db)
        if result.get("skipped"):
            print(f"  跳过: {result['reason']}")
        else:
            print(f"  趋势准确率: {result['trend_accuracy']:.1%}")
            print(f"  时点准确率: {result['spot_accuracy']:.1%}")
            print(f"  市场状态: {result['regime']}")
            print(f"  权重调整: {len(result['adjustments'])} 项")
            for adj in result['adjustments']:
                print(f"    {adj['dimension']}: {adj['old_weight']:.3f} → "
                      f"{adj['new_weight']:.3f} ({adj['delta']:+.4f})")
            print(f"  休眠因子: {len(result['hibernations'])} 个")
            for h in result['hibernations']:
                print(f"    {h['factor']}: 准确率={h['accuracy']:.1%} "
                      f"({h['trials']}次)")
            print(f"  重激活: {len(result['awakenings'])} 个")

    if args.weights:
        print("\n=== 当前自适应权重 ===")
        w = get_adaptive_weights(args.state_db)
        print(f"  市场状态: {w['regime']}")
        print(f"  上次进化: {w['last_evolve']}")
        print(f"  维度权重: {json.dumps(w['dim_weights'], indent=2)}")
        print(f"  子因子权重:")
        for dim, factors in w['sub_weights'].items():
            print(f"    {dim}:")
            for fname, fw in factors.items():
                tag = " [休眠]" if f"{dim}.{fname}" in w['hibernating'] else ""
                print(f"      {fname:20s} = {fw:.3f}{tag}")

    if args.log > 0:
        print(f"\n=== 最近 {args.log} 条进化日志 ===")
        entries = get_evolution_log(limit=args.log, state_db_path=args.state_db)
        for e in entries:
            print(f"  [{e.get('created_at', '?')}] {e['event_type']}: {e['event_desc']}")

    if not any([args.evolve, args.weights, args.log, args.set_regime]):
        parser.print_help()
