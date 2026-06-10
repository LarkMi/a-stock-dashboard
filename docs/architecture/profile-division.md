# A股智能预测系统 — Profile 分工方案

## 可用 Profiles

| Profile | 角色 | 模型 | 职责 |
|---------|------|------|------|
| **pm-architect** | 架构师/项目经理 | deepseek-v4-pro | 需求拆解、架构设计、任务分派、交付审核 |
| **quant-dev** | 量化开发工程师 | deepseek-v4-pro | 代码实现、脚本编写、模型开发 |
| **qa-tester** | QA测试工程师 | deepseek-v4-pro | 测试验证、回归测试、边界覆盖 |

---

## 阶段1: 技术面因子扩展 + 重构

### Task 1.1 技术面因子模块 [quant-dev]
**文件**: `scripts/tech_factors.py` (NEW)
**内容**:
- 将 market_watcher.py 中 calc_indicators() 和 screen_all_stocks() 的技术因子计算逻辑提取到独立模块
- 新增: ATR波动率因子 (1.5)
- 新增: 筹码分布因子 (1.6)
- 新增: 支撑阻力位因子 (1.7)
- 每个因子独立函数，返回标准化得分 (-N ~ +N)
- 入参: (DuckDB conn, ts_code, cutoff_date=None) → dict of factor scores
- 出参: `{"trend": 2.1, "momentum": 1.5, "position": 0.3, "volume": 0.8, "atr": -0.2, "distribution": 0.5, "sr": -0.3}`

### Task 1.2 技术面因子单元测试 [qa-tester]
**依赖**: Task 1.1
**内容**:
- 验证各因子计算正确性(与DuckDB直接SQL对比)
- 验证边界: 新股(<5日)、退市股、极端波动(涨跌停)
- 验证归一化: 各因子得分在声明范围内

---

## 阶段2: 情绪面因子

### Task 2.1 情绪面因子模块 [quant-dev]
**文件**: `scripts/sentiment_factors.py` (NEW)
**内容**:
- 市场宽度因子 (3.1): 从DuckDB全市场统计
- 涨跌停比因子 (3.2): 从DuckDB pct_chg计数
- 北向资金因子 (3.3): web_search + 缓存
- 融资融券因子 (3.4): web_search + 缓存
- 出参: `{"breadth": 1.2, "limit_ratio": 0.5, "northbound": 0.8, "margin": -0.3}`
- 缓存策略: 北向/融资数据缓存2h，市场宽度/涨跌停比实时计算

### Task 2.2 情绪面因子测试 [qa-tester]
**依赖**: Task 2.1
**内容**:
- 验证DuckDB因子计算准确性
- 验证web_search因子fallback逻辑(搜索失败时用缓存)
- 验证缓存过期机制(缓存>2h强制刷新)

---

## 阶段3: 宏观面因子

### Task 3.1 宏观面因子模块 [quant-dev]
**文件**: `scripts/macro_factors.py` (NEW)
**内容**:
- 美股隔夜-SP500 (4.1): web_search + US Overnight缓存表
- 美股隔夜-Nasdaq (4.2): 同上
- 人民币汇率 (4.3): web_search + 缓存
- VIX (4.4): web_search + 缓存
- 出参: `{"sp500": 0.8, "nasdaq": 1.2, "fx": -0.3, "vix": -0.5}`
- US Overnight缓存: 每日9:00刷新，非交易时段跳过
- 失败回退: 缓存≤48h可用，超过则返回0(中性)

### Task 3.2 宏观面因子测试 [qa-tester]
**依赖**: Task 3.1
**内容**:
- 验证美股数据获取(对比Yahoo Finance)
- 验证USDCNY准确性(对比新浪外汇)
- 验证非交易时段逻辑(美股周末不返回假数据)
- 验证缓存48h过期

---

## 阶段4: 消息面因子

### Task 4.1 消息面因子模块 [quant-dev]
**文件**: `scripts/news_factors.py` (NEW) — 扩展现有 news_event_db.py
**内容**:
- 个股公告因子 (2.1): web_search 搜索结果NLP提取
- 行业政策因子 (2.2): 维护政策日历JSON + web_search补充
- 龙虎榜因子 (2.3): web_search + 简单解析
- 出参: `{"announcement": 1.5, "policy": 0.0, "dragon_tiger": 0.8}`
- 缓存策略: 每标的每日仅查一次，结果缓存到SQLite

### Task 4.2 消息面因子测试 [qa-tester]
**依赖**: Task 4.1
**内容**:
- 验证公告提取准确性(对比东方财富公告页)
- 验证政策日历覆盖度
- 验证龙虎榜数据解析
- 验证web_search超时/失败处理

---

## 阶段5: 多因子聚合 + 自适应权重

### Task 5.1 因子聚合引擎 [quant-dev]
**文件**: 修改 `market_watcher.py` + 新增 `scripts/factor_aggregator.py`
**内容**:
- 整合四个维度因子模块到统一接口
- 实现维度内归一化
- 实现维度加权 (初始权重 + 市场阶段调整)
- 实现自适应权重更新 (基于20日准确率)
- 修改 screen_all_stocks() 使用多因子评分
- 修改 generate_trend_prediction() / generate_spot_prediction() 接入多因子

### Task 5.2 多因子聚合测试 [qa-tester]
**依赖**: Task 5.1
**内容**:
- 回测对比: 新18因子 vs 旧4因子 准确率
- 验证归一化不产生奇异值(NaN/Inf)
- 验证自适应权重机制(模拟20日准确率变化)
- 验证市场阶段调整准确性

---

## 阶段6: 自进化系统

### Task 6.1 自进化模块 [quant-dev]
**文件**: `scripts/evolution.py` (NEW)
**内容**:
- 错误分类系统: 自动标记 direction_error / magnitude_error / timing_error
- 自动补丁生成: 基于错误模式调整因子权重
- 将现有 update_actuals() / get_acc_adjustment() 逻辑迁移到新模块
- DB迁移: factor_weights表、error_log表、factor_snapshots表
- 进化日志: 自动记录到 evolution_log 表

### Task 6.2 自进化测试 [qa-tester]
**依赖**: Task 6.1
**内容**:
- 模拟错误注入,验证分类准确性
- 验证权重自适应调整的正确性
- 验证完整自进化流程(预测→验证→分类→补丁)
- 验证误分类不破坏系统稳定性

---

## 阶段7: Web看板升级

### Task 7.1 看板升级 [quant-dev]
**文件**: 修改 `gen_dashboard.py` + `server.py`
**内容**:
- 多因子信号拆解面板: 技术/消息/情绪/宏观各自打分展示
- 美股隔夜数据展示区域
- 自定义标的批量添加: CSV导入 / web界面
- 自定义标的: 分组功能 (如"科技"、"消费"分组)
- API端点: /api/us-overnight, /api/custom-groups, /api/factors
- 保持现有深色主题 + 移动优先设计

### Task 7.2 看板测试 [qa-tester]
**依赖**: Task 7.1
**内容**:
- 移动端响应式测试 (320px/768px/1024px)
- API端点功能测试
- 批量导入边界测试(空CSV/超大量/重复标的)
- 自动刷新功能验证(各交易时段触发)
- 微信分享链接验证

---

## 阶段8: 系统集成 + 端到端测试

### Task 8.1 系统集成 [quant-dev]
**内容**:
- 更新cron配置: 集成新模块路径
- 更新server.py启动脚本
- 确保所有双副本同步(projects/ ↔ hermes/scripts/)
- 迁移现有数据: predictions表添加新字段

### Task 8.2 端到端测试 [qa-tester]
**依赖**: Task 8.1
**内容**:
- 完整流程: Cron触发→数据加载→18因子计算→海选→预测→看板刷新→微信通知
- 回测验证: 对比旧系统结论一致性
- 压力测试: 3654只全市场海选耗时
- 内存检查: 18因子计算内存峰值
- Web Search速率限制验证

---

## 任务依赖关系图

```
阶段1 (技术面)  ──┐
阶段2 (情绪面)  ──┤
阶段3 (宏观面)  ──┼──→ 阶段5 (聚合) ──→ 阶段6 (自进化) ──→ 阶段8 (集成)
阶段4 (消息面)  ──┘                                         │
                                                            │
阶段7 (看板升级, 可并行于阶段1-6) ────────────────────────────┘
```

阶段7(Web看板)不依赖新因子模块，可以与阶段1-6并行开发。

每个阶段内: quant-dev 先交付 → qa-tester 审核 → pm-architect 确认后进入下一阶段。
