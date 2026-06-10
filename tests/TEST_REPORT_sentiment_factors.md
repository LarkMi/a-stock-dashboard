# 情绪面因子测试报告 — sentiment_factors.py

**测试日期**: 2026-06-10
**测试者**: qa-tester
**状态**: ❌ 不通过 (2个P0阻塞性Bug)

---

## 测试概要

| 指标 | 值 |
|------|-----|
| 测试文件 | test_sentiment_factors.py (571行, 54测试) + test_sentiment_deep_dive.py (新增, 28测试) |
| 总测试数 | 82 |
| 通过 | 76 |
| 失败(P0) | 4 |
| 跳过 | 2 (slow标记) |
| 执行耗时 | 约3分钟 |
| 环境 | Python 3.14.3, DuckDB 2.2GB, Windows 10 |

---

## P0 Bug 清单 (阻塞性 — 必须修复后Web因子才能工作)

### Bug #1: 北向资金API响应格式不匹配 [P0]

- **文件/行号**: `scripts/sentiment_factors.py` L284-329 (`_fetch_northbound_net`)
- **问题**: 代码解析 `j['data']['klines']`，但东方财富API实际返回结构为:
  ```json
  {
    "data": {
      "hk2sh": ["2026-06-10,0.00,5200000.00,0.00"],
      "hk2sz": ["2026-06-10,0.00,5200000.00,0.00"],
      "sh2hk": [...],
      "sz2hk": [...]
    }
  }
  ```
  无 `klines` 字段。
- **影响**: `klines` 始终为空列表 → `net_sh=0.0, net_sz=0.0` → northbound因子**永远返回0.0(中性)**，完全失效
- **复现步骤**: 调用 `factor_northbound('20260609')` → 始终返回 `0.0`
- **修复建议**: 修改解析逻辑，读取 `data.hk2sh[0]` 和 `data.hk2sz[0]`，解析各字段中的净买入金额(第4列)

### Bug #2: 融资融券API报告名无效 [P0]

- **文件/行号**: `scripts/sentiment_factors.py` L371-407 (`_fetch_margin_change`)
- **问题**: API reportName `RPTA_WEB_MARGIN_TRADEHIS` 不存在，东方财富返回:
  ```json
  {"success": false, "message": "报表配置不存在,RPTA_WEB_MARGIN_TRADEHIS", "code": 9501}
  ```
- **影响**: `_fetch_margin_change()` 始终返回 `None` → margin因子**永远回退到缓存或0.0(中性)**
- **复现步骤**: 清空缓存后调用 `factor_margin('20260609')` → 返回 `0.0`
- **修复建议**: 确认东方财富正确的融资融券数据报表名称，或切换到其他数据源API

### Bug #3: 北向API两次请求URL完全相同 [P2]

- **文件/行号**: `scripts/sentiment_factors.py` L292-309
- **问题**: `url_sh` 和 `url_sz` 使用完全相同的URL字符串，第二次HTTP请求完全冗余
- **影响**: 浪费一次HTTP请求(8秒超时开销)，不影响功能
- **修复建议**: 删除冗余请求；Bug#1修复后原始API一次请求即返回沪股通+深股通全部数据

---

## P2 可优化项

1. **节假日处理不完整**: `_is_weekend()`只检测周六日，不处理春节/国庆等A股法定节假日。非周末节假日会尝试调用Web API获取可能不存在的数据
2. **浮点精度**: `(0.6-0.5)/0.1` = 0.9999999999999998 (IEEE754浮点误差)，对因子值影响可忽略，不影响功能

---

## DuckDB因子验证 ✅ 全部通过

| 测试项 | 结果 |
|--------|------|
| ST/BJ/8%过滤 | ✅ 生效 (5515条→5198条, 过滤317条BJ) |
| breadth值域 ±2.0 | ✅ 正确clamp |
| limit_ratio值域 ±1.5 | ✅ 正确clamp |
| 100%上涨 → breadth=2.0 | ✅ |
| 100%下跌 → breadth=-2.0 | ✅ |
| 50%涨跌 → breadth=0.0 | ✅ |
| 不存在日期 → None | ✅ |
| 无涨跌停日 → 0.0 | ✅ |
| ST前缀正确排除 | ✅ |
| BJ前缀正确排除 | ✅ |
| 8开头前缀正确排除 | ✅ |

---

## 缓存系统验证 ✅ 全部通过

| 测试项 | 结果 |
|--------|------|
| SQLite sentiment_cache.db 自动创建 | ✅ |
| datetime('now','localtime') 本地时间 | ✅ |
| 2h内新鲜缓存命中 | ✅ |
| >2h但<24h 回退到24h缓存 | ✅ |
| >24h 过期返回None | ✅ |
| 写入→读取→更新→过期 完整链路 | ✅ |
| raw_data=None 正确处理 | ✅ |

---

## 性能基准

| 操作 | 平均耗时 | 阈值 | 状态 |
|------|---------|------|------|
| breadth单次计算 | 1.36s | <2s | ✅ |
| limit_ratio单次计算 | 1.29s | <2s | ✅ |
| 全因子(含Web API) | 9.8s | <30s | ✅ |
| 缓存读取 | 0.15ms/次 | <10ms | ✅ |
| DuckDB自动发现 | <0.1s | N/A | ✅ |

---

## 接口规范验证 ✅

| 测试项 | 结果 |
|--------|------|
| compute_sentiment_factors 返回7字段 | ✅ |
| compute_sentiment_factors_auto 自动发现 | ✅ |
| warnings列表正确填充 | ✅ |
| raw子字典含 breadth_raw/limit_up_count/limit_down_count | ✅ |
| computed_at 时间戳格式正确 | ✅ |
| 单因子崩溃不影响其他因子 | ✅ |
| 同输入幂等性 | ✅ |

---

## 结论

**❌ 不通过**

- DuckDB因子(breadth/limit_ratio): 通过全部测试, 代码质量良好
- Web因子(northbound/margin): 因API格式不匹配/报告名无效**完全失效**
- 缓存系统: 通过全部测试
- 接口规范: 通过全部测试

**阻塞**: Web因子需要修复P0 Bug #1和#2后才能投入使用。
