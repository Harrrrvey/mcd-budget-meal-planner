# 优惠券数据结构与匹配规则

本文件定义 `scripts/plan.py` 在券维度上使用的数据结构、状态枚举与匹配规则。
**本文件不含任何价格、不含任何券面金额。** 价格永远来自实时菜单与 `calculate-price`。

> 当前状态：**本项目至今没有取得任何一张真实优惠券**。
> 券包与门店券的真实样本都是空的，因此「真实带券核价」**仍未验证**。
> 离线测试使用的非空券样本来源见 `tests/fixtures/coupons/README.md`，其中
> `structure_sample_*` 为依官方返回结构构造、`synthetic_*` 为完全模拟。

---

## 一、金额单位约定（必须先读）

| 场景 | 单位 | 处理函数 |
|---|---|---|
| 官方接口返回的 `price` / `originalPrice` / `discount` / `originalSubtotal` / `subtotal` / `productPrice` / `enjoyable.balance` | **分**（整数） | `from_cents()` 转元用于展示 |
| `query-meals` 菜单里的 `currentPrice` | **元** | `to_cents()` 转分后参与券金额运算 |

规则：

1. 券相关的一切金额运算**只在「分」上进行**，展示时才转元。
2. 输出 JSON 中 `*Cents` 字段单位是分，`*Yuan` 字段单位是元，二者同源。
3. 既有组合搜索仍在用 `priceYuan`（元）。它**不参与**券金额运算，仅为兼容保留。
4. **券的实际节省** = 同一组合的「无券官方价」−「带券官方价」。
   **任何时候都不得用券面金额代替实际节省。**

---

## 二、Coupon

```jsonc
{
  "couponId": "S3001",              // 券 ID；available-coupons 的券恒为 null
  "couponCodes": ["SC-3001A", "SC-3001B"],  // 同一 couponId 可对应多张券码
  "title": "派买一送一",
  "source": "wallet | store | unclaimed",
  "tradeStartDate": "2026-10-01 00:00:00",  // 可能缺失
  "tradeEndDate": "2026-12-31 23:59:59",    // 可能缺失
  "labels": ["到店专用"],            // 来自 instructions.labels[].text
  "applicableCodes": ["9900015165"], // 来自 products[].productCode
  "applicabilityCodesKnown": true,   // false = 官方未给出适用商品，**不得当作全品适用**
  "validity": "valid",               // 见下表
  "probeKey": "S3001",               // 送进 calculate-price 的键；为 null 则无法试算
  "sourceQueryStatus": "ok",         // 该来源本次查询的状态
  "rawKeys": ["couponCode", "couponId", "tradeDateTime", "title"]
}
```

### 三个来源的严格区分

| source | 来自 | 官方口径 | 能据此判断什么 |
|---|---|---|---|
| `wallet` | `query-my-coupons` | 官方明确：**不校验门店、渠道、配送方式**，"不承诺可用于当前订单" | 只能说明「用户拥有它」 |
| `store` | `query-store-coupons` | 按「门店 + 订单类型」返回 | **唯一**可据以判断「本店本渠道可用」的来源 |
| `unclaimed` | `available-coupons` | 只有券名 / 图片 / 状态 | **什么都不能判断**，适用性恒为 `unassessable` |

`wallet` 与 `store` 的 `couponId` 交集之外的卡包券，归入 `walletOnly`（"有券但本店可能用不了"）。

---

## 三、状态枚举

### `validity`（时间可用性）

| 值 | 含义 |
|---|---|
| `valid` | 在有效期内 |
| `expired` | 已过期（`tradeEndDate` 早于当前时间） |
| `not_yet` | 尚未生效（`tradeStartDate` 在未来，或 `labels` 含"未到使用时间"） |
| `validity_unknown` | **有效期字段缺失 —— 不猜**，仍需官方试算才能确认 |

### 适用性置信状态（`status`）

| 值 | 含义 | 是否安排官方试算 |
|---|---|---|
| `code_matched` | 券的适用商品编码与候选商品**有交集**。仍**不等于**"确定可省" | 是 |
| `unknown` | 官方**未给出**适用商品编码。**严禁当作全品适用** | 默认否（`--probe-unknown` 才试算） |
| `not_applicable` | 编码无交集，或时间上不可用 | 否 |
| `unverifiable` | 送进官方试算但**失败**，无法证明其效果 | 已被试算 |
| `unassessable` | 没有 `couponId` / `couponCode`，**压根无法送进官方试算** | 否 |

> `code_matched` 只说明"编码对得上"，**不说明能省多少钱**。金额一律以官方核价为准。

---

## 四、Candidate / Quote

```jsonc
// Candidate（由 candidates 子命令产出）
{
  "id": "C1",
  "items": [{"code": "9900015160", "name": "大堡口福三件套", "qty": 1, "menuUnitPriceYuan": 23.9}],
  "menuTotalYuan": 23.9,
  "satisfies": {"burger": true, "drink": true, "side": true},
  "preferenceHits": 0,
  "couponMatches": [
    {"couponId": "S3001", "title": "派买一送一", "source": "store",
     "status": "code_matched", "validity": "valid",
     "matchedCodes": ["9900015165"], "probePlanned": true}
  ],
  "probePlan": {"baseline": true, "coupons": ["S3001"], "totalProbes": 2, "prunedCount": 0}
}
```

```jsonc
// Quote（compare 子命令读取；键为候选 id）
{"quotes": {
  "C1": {
    "none":  { /* calculate-price 不带券的真实返回 */ },
    "S3001": { /* calculate-price 带 couponId=S3001 的真实返回 */ }
  }
}}
```

`compare` 对每个候选输出：

| 字段 | 含义 |
|---|---|
| `originalPriceCents` / `originalPriceYuan` | 官方 `data.originalPrice` |
| `discountCents` / `discountYuan` | 官方 `data.discount` |
| `priceCents` / `priceYuan` | 官方 `data.price`（应付） |
| `savingCents` / `savingYuan` | **本组合无券官方价 − 本组合带券官方价** |
| `savingSource` | `baseline_price_minus_coupon_price` / `baseline` / `baseline_unavailable` |
| `usedCoupon` | 是否用了券 |

---

## 五、匹配规则（本地、不联网）

1. `validity` 为 `expired` / `not_yet` → `not_applicable`，**且不提请试算**。
2. `probeKey` 为空（无 `couponId`）→ `unassessable`。
3. `applicabilityCodesKnown = false` → `unknown`，**不得视为全品适用**，默认不试算。
4. `applicableCodes ∩ 候选商品编码 ≠ ∅` → `code_matched`。
5. 否则 → `not_applicable`。

**探针预算**：每个候选的探针数 = 1（无券基线）+ 计划试算的券数；
单候选最多 `--max-coupons-per-candidate`（默认 3）张券，超出的裁剪并记录 `prunedCount`。

---

## 六、排序与结论措辞

排序：`官方应付价升序` → `偏好命中数降序` → `用券数升序`。
最后一条是**本产品自定义规则**（券是稀缺资源，能不用就不用），不是客观结论。

**唯一允许的结论措辞**：

> 在「当前门店 + 当前渠道 + 已确认组成的商品 + 已实际核价的组合」范围内，
> 这是已找到的最低应付价方案。**这不等于全局最优。**

---

## 七、明确未验证的事项

1. 券能否叠加、同一张券能否重复使用、满减券挂在哪一行 —— **未验证**。
2. 真实带券核价的 `discount > 0` —— **本账号尚无券样本，未验证**。
3. `query-my-coupons` 的真实返回字段（含 `instructions.labels`）—— **仅由第三方公开材料推断**。
4. 批量领券对有效期与后续领取的影响 —— **未验证**。
5. `unclaimed` 券的适用性 —— **官方接口不支持提前判断**。
