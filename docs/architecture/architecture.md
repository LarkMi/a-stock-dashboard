# A股智能预测系统 — 系统架构设计

## 1. 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                    Web Dashboard (SPA)                        │
│  dark theme / mobile-first / 自动刷新 / 微信分享              │
├──────────────────────────────────────────────────────────────┤
│                     HTTP Server (server.py)                    │
│  ThreadingHTTPServer / 静态文件 + REST API / CORS             │
├──────────────────────────────────────────────────────────────┤
│                 Prediction Engine (market_watcher.py)          │
│                                                               │
│  ┌──────────────┬──────────────┬──────────────┬─────────────┐│
│  │  技术面因子  │  消息面因子  │  情绪面因子  │ 宏观面因子  ││
│  │ (7因子)      │ (3因子)      │ (4因子)      │ (4因子)     ││
│  └──────┬───────┴──────┬───────┴──────┬───────┴─────┬───────┘│
│         └──────────────┴──────────────┴─────────────┘        │
│                          ↓                                    │
│            ┌─────────────────────────┐                        │
│            │  Multi-Factor Scoring    │                        │
│            │  归一化 + 自适应加权     │                        │
│            └───────────┬─────────────┘                        │
│                        ↓                                      │
│          ┌─────────────┴──────────────┐                       │
│          │  趋势预测 (1-3日方向)      │                       │
│          │  时点预测 (下一时段方向)    │                       │
│          └─────────────┬──────────────┘                       │
├────────────────────────┼──────────────────────────────────────┤
│              Self-Evolution System                             │
│  ┌──────────────┬──────────────┬──────────────┐              │
│  │  自动验证     │  错误分类     │  自动打补丁   │              │
│  │  (30m+24h)   │  (方向/幅度/时│  (factor权重  │              │
│  │              │   间)         │   自适应)     │              │
│  └──────────────┴──────────────┴──────────────┘              │
├───────────────────────────────────────────────────────────────┤
│                     Data Layer                                 │
│  ┌──────────────┬──────────────┬──────────────┐              │
│  │  DuckDB 日线  │  SQLite State │  Web Sources  │              │
│  │  (2.2GB)     │  (预测/验证)   │  (新浪/搜索)   │              │
│  └──────────────┴──────────────┴──────────────┘              │
├───────────────────────────────────────────────────────────────┤
│                     Notification Layer                          │
│  ┌───────────────────────────────────────────┐               │
│  │  WeChat Gateway / Cron / 异常推送          │               │
│  └───────────────────────────────────────────┘               │
└───────────────────────────────────────────────────────────────┘
```

## 2. 数据流详解

### 2.1 预测主流程

```
Cron触发 (9:00/9:30/10:30/11:30/13:00/14:00/15:00)
  │
  ├─① 加载数据
  │   ├─ DuckDB: 全市场日线 (pct_chg/vol/close/high/low)
  │   ├─ SQLite: 历史预测 + 准确率统计
  │   └─ 新浪API: 盘中实时价格 (仅交易时段)
  │
  ├─② 计算因子 (18因子 → 归一化)
  │   ├─ 技术面 (7): MA趋势/动量/位置/量价/ATR/筹码分布/SR位
  │   ├─ 消息面 (3): 公告事件/行业政策/龙虎榜
  │   ├─ 情绪面 (4): 市场宽度/涨跌停比/北向资金/融资融券
  │   └─ 宏观面 (4): 美股隔夜/汇率/大宗商品/VIX
  │
  ├─③ 自适应加权 (基于市场阶段)
  │   ├─ regime_detector: 牛市→趋势权重↑ / 震荡→反转权重↑
  │   └─ 历史准确率: 单股级别权重微调 (±0.1)
  │
  ├─④ 全市场海选 (排除ST/BJ/688/300/8xx)
  │   ├─ 做多Top10: 综合分最高
  │   └─ 做空Top10: 综合分最低
  │
  ├─⑤ 双预测生成
  │   ├─ 趋势预测: 1-3日方向 (strong_up/up/neutral/down/strong_down)
  │   └─ 时点预测: 下一时段方向 (up/flat/down)
  │
  ├─⑥ 保存预测 (SQLite, 60min去重)
  │
  ├─⑦ 刷新看板 (gen_dashboard.py → dashboard.html)
  │
  └─⑧ 推送通知 (微信, 仅异常/预警)
```

### 2.2 验证+自进化流程

```
每次预测保存后 (异步):
  │
  ├─ 30分钟后 → 时点验证
  │   └─ update_actuals() → 比对新实际价 vs 预测方向
  │
  ├─ 24小时后 → 趋势验证
  │   └─ update_actuals() → 比对新实际价 vs 预测方向
  │
  ├─ 错误分类
  │   ├─ direction_error: 方向反了
  │   ├─ magnitude_error: 方向对但幅度不匹配
  │   └─ timing_error: 方向对但时间窗口不对
  │
  └─ 自动打补丁
      ├─ 单因子准确率回测 → 调整权重
      ├─ 个股准确率 < 40% → 轮换标的
      └─ 系统级偏差检测 → 全局阈值调整
```

## 3. 模块依赖关系

```
market_watcher.py (核心引擎)
  ├─── news_event_db.py       [消息面因子]
  ├─── regime_detector.py     [市场阶段 + 自适应权重]
  ├─── sentiment_factors.py  [NEW: 情绪面因子]
  ├─── macro_factors.py       [NEW: 宏观面因子]
  ├─── tech_factors.py        [NEW: 技术面因子(重构)]
  └─── evolution.py           [NEW: 自进化系统]

gen_dashboard.py (看板生成)
  ├─── market_watcher.py 结果
  └─── SQLite state (历史统计)

server.py (HTTP服务)
  └─── dashboard.html + API端点
```

## 4. 数据库设计变更

### 4.1 SQLite State DB 新增表

```sql
-- 因子权重表（自进化调整记录）
CREATE TABLE factor_weights (
    factor_name TEXT PRIMARY KEY,  -- 'tech_trend', 'tech_momentum', etc.
    current_weight REAL,           -- 当前权重
    baseline_weight REAL,          -- 基线权重
    accuracy_20d REAL,             -- 近20日准确率
    last_updated TIMESTAMP
);

-- 错误分类表
CREATE TABLE error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pred_id INTEGER REFERENCES predictions(id),
    error_type TEXT,  -- 'direction'/'magnitude'/'timing'
    error_detail TEXT,
    created_at TIMESTAMP
);

-- 因子快照（每次预测时保存各因子得分，供回测分析）
CREATE TABLE factor_snapshots (
    pred_id INTEGER REFERENCES predictions(id),
    ts_code TEXT, pred_time TEXT,
    tech_score REAL, news_score REAL,
    sentiment_score REAL, macro_score REAL,
    PRIMARY KEY (pred_id)
);

-- 美股隔夜缓存（避免重复请求）
CREATE TABLE us_overnight_cache (
    trade_date TEXT PRIMARY KEY,
    sp500_chg REAL, nasdaq_chg REAL,
    vix REAL, usd_cny REAL,
    updated_at TIMESTAMP
);
```

### 4.2 predictions 表新增字段

```sql
ALTER TABLE predictions ADD COLUMN factor_scores TEXT;  -- JSON: {"tech":2.5,"news":0.3,...}
ALTER TABLE predictions ADD COLUMN spot_correct INTEGER; -- 时点验证结果(-1/0/1)
ALTER TABLE predictions ADD COLUMN trend_correct INTEGER; -- 趋势验证结果(-1/0/1)
```

## 5. API端点扩展

```
GET  /api/status          → 系统状态(预测数/准确率/最后刷新)
GET  /api/factors          → 当前因子权重 + 各维度得分
GET  /api/us-overnight     → 美股隔夜数据
GET  /api/custom-groups    → 自定义分组列表
POST /api/custom-groups    → 创建/更新分组
DELETE /api/custom-groups  → 删除分组
POST /api/custom-stocks    → 批量添加自定义标的
GET  /api/evolution-log    → 自进化事件日志
POST /api/backtest         → 触发指定日期回测
```

## 6. 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 因子归一化方式 | Continuous z-score | 比离散档位更细粒度，同分概率低 |
| 自适应权重 | 滚动20日准确率 Softmax | 简单可解释，不需要复杂ML |
| 美股隔夜数据源 | web_search + 缓存 | 免费，T+1足够用 |
| 消息面数据源 | web_search(个股公告) + 固定政策日历 | 免费，无需付费新闻API |
| 北向资金数据 | web_search(东方财富) | 免费公开数据 |
| 模型 vs 规则 | 保留因子打分规则，暂不加DL | 可解释性优先，规则可调试 |
| 看板技术栈 | 纯HTML/JS/CSS (保持现有) | 零依赖，server.py直接serve |
