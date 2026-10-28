# 数据目录（不包含任何数据）

本仓库**不附带**行情/资金费数据，请用公开 API 自行拉取。

## 方法一：使用拉取脚本（推荐）

```bash
python src/r174a_fetch.py
```

脚本会从 Hyperliquid 公开 API 拉取：

- `fundingHistory`：各资产逐小时资金费（分页，无 key）；
- `candleSnapshot`：现货/永续 1d、1h K 线。

默认输出到 `data/R174A_HL_SPOT_PERP_CARRY/`（可用环境变量 `R174A_DIR` 覆盖）。

## 方法二：手动调用 API

```bash
curl -X POST https://api.hyperliquid.xyz/info \
  -H "Content-Type: application/json" \
  -d '{"type":"fundingHistory","coin":"HYPE","startTime":0}'
```

## 数据口径

| 字段 | 说明 |
|---|---|
| funding | 每小时结算的资金费率（历史累计求和为日资金费） |
| spot/perp 1d | 日线收盘价（UTC 日界） |
| 1h K 线 | 用于资金费结算时点校验 |

> 公开 API 无需密钥；请遵守交易所速率限制。
