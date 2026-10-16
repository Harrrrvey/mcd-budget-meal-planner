# 优惠券测试夹具 · 来源说明

> **本目录的样本必须严格区分来源。任何把模拟数据当成真实接口结果的行为都算 bug。**
> 本文件不含 Token、不含手机号、不含订单号。

## 一、来源等级

| 等级 | 含义 | 允许用于 |
|---|---|---|
| `real` | 本项目**实际调用**麦当劳官方 MCP 得到的返回 | 断言「接口真实返回结构」 |
| `structure_sample` | **不是抓包**。依据官方工具自身给出的返回结构说明（字段名清单）手工构造 | 断言「我们解析器对官方声明结构的处理」 |
| `synthetic` | 完全模拟，用于覆盖真实样本缺失的场景（尤其是**带券核价**） | 断言「本地逻辑正确性」，**不得**当作接口能力证明 |

## 二、逐文件说明

| 文件 | 等级 | 来源与说明 |
|---|---|---|
| `real_available_coupons_2026-10-16.txt` | `real` | 2026-10-16 03:10（GMT+8）对 `available-coupons` 的真实调用返回。**已剥离图片链接**（含签名 URL），券标题与「可领取」状态逐字保留。 |
| `real_store_coupons_empty_3230019.json` | `real` | 2026-10-16 03:10 对 `query-store-coupons(storeCode=3230019, orderType=1, beType=1)` 的真实返回：`code=200` 且 `data` 为空数组。 |
| `real_wallet_coupons_empty.txt` | `real` | 2026-10-16 03:10 对 `query-my-coupons` 的真实返回：券包为空。 |
| `structure_sample_store_coupons.json` | `structure_sample` | **非真实抓包**。字段名取自官方工具对 `query-store-coupons` 的返回结构说明（`data[].couponId` / `couponCode` / `tradeDateTime` / `products[].productCode`），并特意包含「同一 couponId 对应多个券码」的情形。 |
| `synthetic_wallet_coupons.json` | `synthetic` | 模拟卡包，覆盖：可用券、无适用商品券、未生效券、已过期券、带 labels 的券。 |
| `synthetic_quotes.json` | `synthetic` | 模拟 `calculate-price` 返回，用于验证「实际节省 = 无券价 − 带券价」以及带券核价失败的处置。 |

## 三、必须记住的三件事

1. **真实的券包与门店券样本都是空的。** 本项目至今**没有取得任何一张真实优惠券**，因此
   **「真实带券核价成功」仍未验证**，`discount > 0` 的真实样本为零。
2. `available-coupons` 的真实返回**只有券名、状态和图片**，**没有 couponId**；
   因此未领取券在代码里的适用性恒为 `unassessable`。
3. 本目录的券**永远不会**被送进任何下单流程——`plan.py` 全流程只读，不含 `create-order`。
