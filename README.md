# 📊 A股实时分析看板

全市场 3654 只主板股票实时海选，Top5 做多 + Top5 做空 + 自定义标的，双预测输出（趋势 1-3 日 + 时点下一时段），盘中即时验证 + 准确率反馈学习。

## 🔗 在线看板

**[https://larkmi.github.io/a-stock-dashboard/](https://larkmi.github.io/a-stock-dashboard/)**

## 🏗 架构

```
cron (7次/交易日)
  │
  ▼
market_watcher.py          ← 全市场海选 + 双预测 + 验证 + 学习
  │
  ▼
gen_dashboard.py → dashboard.html
  │
  ▼
server.py:8888 → ngrok → 公网
```

## 📁 文件

| 文件 | 功能 |
|------|------|
| `scripts/market_watcher.py` | 核心分析引擎 |
| `scripts/gen_dashboard.py` | 看板生成器 |
| `scripts/ngrok_watchdog.py` | ngrok 隧道守护 |
| `scripts/update_daily_db.py` | DuckDB 日线增量更新 |
| `server.py` | HTTP 静态服务 + API |
| `dashboard.html` | 生成的看板页面 |

## 🔧 技术栈

- **数据**: DuckDB (1780万行A股日线) + Tushare Pro
- **预测**: 4因子连续评分 (MA偏离 + 动量 + 位置 + 量价)
- **验证**: 趋势24h窗口 + 时点1h窗口，COALESCE防覆盖
- **学习**: 准确率反馈 (≥3条验证后生效)
- **实时价**: 新浪财经 API (交易时段)
- **前端**: 纯 HTML/CSS/JS，移动端优先，深色主题

## 📊 预测模型

| 类型 | 函数 | 时间窗口 | 输出 |
|------|------|---------|------|
| 趋势 | `generate_trend_prediction()` | 1-3 日 | strong_up/up/neutral/down/strong_down |
| 时点 | `generate_spot_prediction()` | 下一时段 | up/down/flat |

## 🛡 数据防护

- L1: `is_trading_day()` — DuckDB 交易日检查
- L2: `is_market_hours()` — 9:25-15:05 交易时段
- L3: `fetch_realtime_*()` — 非交易时段返回空，阻止假数据
