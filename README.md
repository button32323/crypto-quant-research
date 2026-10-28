# Crypto Quant Research — Hyperliquid Funding-Rate Carry

一个可复现的加密货币量化研究仓库：**回测引擎、统一成本模型、Hyperliquid 现货-永续资金费 carry 策略的回测/验证/paper 交易框架、风控与对账工具**。

> ⚠️ 研究代码，非投资建议。不保证任何收益；实盘交易可能损失全部本金。

## 核心结论（历史回测，685 天，日线）

策略：买入现货 + 等额做空永续（delta-neutral），赚取资金费。数据区间 2024-12 ~ 2026-10。

| 指标 | 数值 |
|---|---:|
| 年化收益（A0 恒持有） | **+18.08%** |
| Sharpe | 4.71 |
| 最大回撤 | **0.74%** |
| 年化波动 | 4.13% |
| 收益归因 | 资金费贡献为主（约 +$395 / $1,000 资本），basis 小幅拖累 |
| Bootstrap 年化 p5–p95 | +14.3% ~ +24.9% |
| 压力测试 | 资金费减半 +8.7%；资金费归零 −1.4% |
| 可承载资本（动态） | 约 **$12–14k**（受小市值资产流动性约束） |

**这不是"暴富策略"**：它本质是低风险的"资金费利息"，收益天花板由本金和容量决定。仓库里同时有对 150+ 轮方向性实验的复盘结论：**扣成本后无可验证 edge**，因此本项目不提供方向性策略。

## 仓库结构

```
src/                    核心代码（Python，numpy/pandas）
  r156x_backtest_engine_v2.py   回测引擎 v2（修复了 v1 的持仓释放与 NAV 重复计入 bug）
  cost_model.py                 统一成本模型（maker/taker/跨所/资金费/滑点）
  paper_hl_carry.py             HL 现货-永续 carry 的 paper 交易引擎（逐小时资金费结算）
  validate_paper.py             30 天验证门禁（预注册条件）
  risk_rules.py                 风控规则（杠杆/回撤/连续负资金费/停止信号）
  reconcile_funding.py          资金费独立对账
  reconcile_nav.py              NAV 独立对账
  capacity_analysis.py          容量分析（日成交额 1% 口径）
  candidate_carry_screen.py     候选资产 carry 筛选
  historical_validation.py      历史回测/变体比较/Bootstrap/压力测试
  daily_backtest.py             每日历史回测（拉最新数据重跑）
  funding_environment.py        资金费环境分位分析
  live_order_plan.py            实盘下单计划（dry-run，不发送订单）
  exit_plan.py / reconcile.py   平仓计划与持仓对账
  test_*.py                     单元测试（黄金测试、门禁、对账、风控等）
docs/                   历史验证报告、预注册档案、数据完整性、风控验证、实盘手册
data/                   数据目录（不含数据，见 data/README.md）
```

## 快速开始

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
pip install -r requirements.txt
```

### 1) 回测引擎黄金测试（无需数据）

```bash
cd src
python test_r156x_engine_v2_golden.py
```

### 2) 拉取数据并跑历史回测

```bash
# 拉数据（Hyperliquid 公开 API，无需密钥；约 1–2 分钟）
python src/r174a_fetch.py

# 每日历史回测（拉最新资金费/日线后重算）
python src/daily_backtest.py

# 今年至今收益
python src/ytd_backtest.py 2026
```

输出为本地 JSON / Markdown，不涉及任何真实交易。

### 3) 运行测试

```bash
cd src
python test_risk_rules.py
python test_validate_paper.py
python test_reconcile_funding.py
python test_reconcile_nav.py
python test_live_plan.py
python test_ops_tools.py
python test_paper_calc.py
```

> 部分测试（`test_paper_spec.py`、`test_paper_log_identity.py`、`test_week_review.py`、`test_journal.py`、`test_final_review.py`）需要先初始化 paper 账户并运行一段时间产生运行时文件，见 `src/paper_hl_carry.py` 的 `init`/`run`；纯逻辑测试开箱即用。

## 策略原理（一页）

永续合约通过资金费锚定现货价格：永续溢价时多头付给空头。策略同时：

1. 买入现货（如 PURR/HYPE）；
2. 做空等额永续（1x，无需杠杆）；
3. 价格涨跌被对冲（delta-neutral），收益 ≈ 资金费 − 交易成本 ± 基差变化。

风险：资金费转负、基差走阔、交易所/合约风险、容量与滑点。详见 `docs/historical_validation_report.md` 与 `docs/risk_rule_validation.md`。

## 数据说明

- 数据来自 Hyperliquid 官方公开 API（`/info`），**不需要 API key、不涉及账户**；
- 本仓库不包含任何数据、私钥、API 密钥或钱包信息；
- 数据字段/获取方式见 `data/README.md` 与拉取脚本。

## 免责声明

- 本项目仅用于研究与教育，**不构成投资建议**；
- 历史回测不代表未来收益，实盘可能亏损全部本金；
- 作者不对任何使用本项目产生的损失负责；
- 禁止将本项目用于操纵市场、洗钱等违法用途。

## License

MIT — 见 [LICENSE](LICENSE)。
