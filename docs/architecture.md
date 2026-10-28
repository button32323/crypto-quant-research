# 系统架构

本仓库是从一个更大的研究项目中抽取出的**可复现核心**，聚焦 Hyperliquid 现货-永续资金费 carry。

## 数据流

```
Hyperliquid 公开 API
  ├─ fundingHistory（逐小时资金费）
  ├─ candleSnapshot 1d（现货/永续日线）
  └─ candleSnapshot 1h（结算时点校验）
        │
        ▼
  r174a_fetch.py → data/R174A_HL_SPOT_PERP_CARRY/
        │
        ├─ historical_validation.py  全期回测 / 变体比较 / Bootstrap / 压力测试
        ├─ daily_backtest.py         每日重跑（最新数据）
        ├─ ytd_backtest.py           指定年份收益
        ├─ capacity_analysis.py      容量上限（日成交额 1% 口径）
        └─ funding_environment.py    资金费环境分位
```

## Paper 交易框架

```
paper_hl_carry.py init   初始化虚拟账户（$1,000、PURR/HYPE）
        │
        ├─ run     每小时抓取资金费增量 → 更新 NAV/资金费/basis → 写状态与日志
        ├─ validate 30 天门禁（预注册条件，见 docs/preregistration.md）
        ├─ reconcile_funding / reconcile_nav   独立重算对账
        ├─ risk_rules  风控信号（杠杆/回撤/连续负资金费/停止）
        └─ live_order_plan / exit_plan / reconcile  实盘 dry-run 工具（不发单）
```

关键设计：

- **资金费按结算时间戳增量累计**，不是按"运行次数"，因此漏跑/重启可追赶；
- **重复结算时点去重**，防止 API 异常导致重复计费；
- **两套独立对账**：资金费独立重算、NAV 独立重算，任何不一致即 FAIL；
- **预注册门禁**：30 天、收益 >0、年化 >10%、回撤 <5%、正费率占比 >60%、对账 PASS；条件冻结后不可改。

## 回测引擎 v2

`r156x_backtest_engine_v2.py` 修复了 v1 的两个实质 bug：

1. 持仓在平仓后未释放，导致反向开仓被忽略、敞口上限失效；
2. NAV 在每个自然日重复计入当日盈亏，导致年化虚高。

配套 `cost_model.py` 统一成本口径（taker/maker/跨所/资金费/滑点），`test_r156x_engine_v2_golden.py` 用 85 项断言锁定行为。

## 局限

- 容量受小市值资产流动性限制（约 $12–14k）；
- 只覆盖 Hyperliquid 单一交易所；
- 历史数据不含订单簿，滑点用常数近似；
- 不包含方向性策略（历史实验扣成本后无可验证 edge）。
