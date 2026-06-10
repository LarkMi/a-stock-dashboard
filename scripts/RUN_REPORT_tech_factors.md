# 技术面因子模块重构 — 运行报告

## 任务

从 `market_watcher.py` 提取7个技术因子到独立模块 `tech_factors.py`

## 产出文件

- **主模块**: `C:/Users/LarkMi/projects/a-stock-dashboard/scripts/tech_factors.py` (14,853 bytes)

## 7个因子

| 因子 | 函数 | 值域 | 数据源 | 说明 |
|------|------|------|--------|------|
| trend | `factor_trend()` | ±3.5 | close, MA5, MA10 | 均线多头/空头排列连续评分（继承原 screen_all_stocks 逻辑） |
| momentum | `factor_momentum()` | ±4.0 | chg_5d | 5日涨跌幅 × 0.2 |
| position | `factor_position()` | ±1.0 | close, 10d high/low | 10日高低位百分比 → (pos-50)×0.02 |
| volume | `factor_volume()` | ±1.5 | vol_ratio, chg_1d | 放量(>1.3倍)时方向×强度 |
| atr | `factor_atr()` | ±1.5 | atr_pct, chg_5d | 14日ATR波动率, 高波动偏空 |
| distribution | `factor_distribution()` | ±2.0 | closes, vols | VWAP vs 现价偏离 |
| sr | `factor_sr()` | ±1.5 | close, 10d high/low | 距边界反转潜力（均值回归） |

## 接口

```python
compute_tech_factors(conn, ts_code, trade_date) -> dict
compute_tech_factors_batch(conn, ts_codes, trade_date) -> list[dict]
_discover_duckdb() -> str | None
```

返回 dict 格式兼容 `multi_factor_aggregator.py` 的 `_weighted_sum()` + `_generate_signals()` 调用。

## 验证结果

| 检查项 | 状态 |
|--------|------|
| Python 语法编译 | ✅ PASS |
| AST 解析 | ✅ PASS |
| 模块导入 (所有7因子) | ✅ PASS |
| 真实 DuckDB 数据测试 (603986.SH, 000001.SZ) | ✅ PASS |
| multi_factor_aggregator 兼容性 | ✅ PASS |
| 因子值域范围 | ✅ 均在声明范围内 |

### 真实数据样本 (日期: 2026-06-10)

```
603986.SH (兆易创新):
  trend=+1.72  momentum=+1.22  position=+0.00  volume=+0.00
  atr=-1.08    distribution=+0.94  sr=+0.00

000001.SZ (平安银行):
  trend=+1.88  momentum=+0.10  position=+0.70  volume=+0.00
  atr=+0.48    distribution=+0.70  sr=-0.71
```

## 局限性

1. **与原有代码的数值差异**: 原 `screen_all_stocks()` 中趋势/动量/位置/量价的原始评分逻辑已完整保留，但 `atr`/`distribution`/`sr` 3个新因子的评分函数是全新设计的，与原代码无对比基准
2. **ATR 方向敏感**: ATR 因子包含趋势方向偏置(chg_5d)，在剧烈震荡市中可能产生噪音
3. **筹码分布简化**: distribution 因子仅用15日 VWAP，未考虑更复杂的筹码峰/密集区
4. **支撑阻力简化**: sr 因子只在边界20%区域激活，中位区无信号 — 这是有意为之（避免与 position 因子重叠）

## 运行方式

```bash
cd C:/Users/LarkMi/projects/a-stock-dashboard/scripts
python tech_factors.py          # CLI 自检 (3只测试股)
```

依赖: `duckdb`, `pandas`, `numpy` (均已在 venv 中)
