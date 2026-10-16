---
name: mcd-budget-meal-planner
description: "麦麦预算点餐师。用麦当劳官方 MCP 的实时菜单与 calculate-price，在用户给定预算内搜索满足「必须含汉堡」等硬性条件的到店餐品组合，并用官方核价返回 2–3 个方案。当用户提到以下诉求时使用：预算 X 元点麦当劳 / X 元怎么搭配麦当劳或麦麦 / 必须有汉堡，帮我配一餐 / 到店点麦当劳想省钱搭配 / 一个汉堡加饮料怎么点最划算 / 帮我用 X 元在麦当劳点一顿。也用于追问「XX 元能不能满足必须有汉堡」。不适用于：麦当劳公司历史、品牌介绍、营养科普、门店加盟、投诉建议等与「按预算搭配餐品并用 MCP 核价」无关的问题。"
description_en: "McDonald's Budget Meal Planner. Use McDonald's official MCP (live menu + calculate-price) to search in-store meal combinations that satisfy hard constraints such as must-contain-a-burger within a user-given budget, then return 2-3 plans verified by official pricing. Triggers: order McDonald's within a budget, budget meal combo, must include a burger, cheapest McDonald's combo. Not for company history, brand info, nutrition education, or complaints."
agent_created: true
metadata:
  category: "food-ordering"
  tags: ["mcdonalds", "mcp", "budget", "meal-planner", "skill"]
  requires:
    - connector: mcd-mcp
---

# 麦麦预算点餐师

"告诉我预算和想吃什么，我帮你找到符合条件的麦当劳餐品组合，并用官方 MCP 核实价格。"

## 这个 Skill 做什么

用户在对话里说出预算、门店线索和点餐要求，本 Skill：

1. 调用麦当劳官方 MCP 查询门店与**实时到店菜单**；
2. **只读**盘点用户已有券与当前门店可用券（不领券）；
3. 用**已确认组成**的商品在本地搜索符合条件的组合，并把**已领取**的券按商品编码匹配到候选上；
4. 只对少量候选调用官方 `calculate-price` 核价（**同一组合分别算「不带券」与「带券」**）；
5. 返回 2–3 个**已核价**方案，如实给出原价 / 优惠金额 / 应付金额 / **实际节省**，或如实说明无解。

**交互全部发生在 WorkBuddy 对话中。** 不建网站、不建数据库、不做账户体系。

## 第一版范围（严格遵守）

支持：**到店自取**（`orderType=1`、`beType=1`）、预算约束、必须含汉堡、可选偏好饮料/小食、套餐按整份参与搜索、官方核价、**已有优惠券匹配**、无解解释。

不支持（不要实现、不要承诺）：外送、得来速、**自动领券（默认关闭）**、自动下单、支付、积分、套餐拆解与特调、营养/过敏原筛选、多人分餐、用户历史。

> **券相关的两条边界**：
> ① 只优化**已领取**的券；`available-coupons` 的未领取券只能"告知"，不能计入本单优惠。
> ② 领券是**写操作且不可撤销**（工具清单里没有退券工具），默认不做。

## 工作流

### 第 1 步：解析用户输入

| 槽位 | 必填 | 说明 |
|---|---|---|
| `budget` | 是 | 预算，单位元 |
| `city` | 是 | 城市名 |
| `storeKeyword` | 是 | 门店搜索关键词（地址/地标/商场名） |
| `mustContainBurger` | 否 | 是否强制含汉堡。第一版默认 `true`（见下方说明） |
| `mustContainDrink` | 否 | **硬约束**：是否强制含饮料 |
| `mustContainSide` | 否 | **硬约束**：是否强制含小食 |
| `preferDrink` | 否 | **软偏好**：有饮料则优先，不保证一定含饮料 |
| `preferSide` | 否 | **软偏好**：有小食则优先，不保证一定含小食 |
| `reservationDate` | 否 | 预约时间，`yyyy-MM-dd HH:mm`。**用户提到明早/今晚等未来时间点时必须传** |
| `desiredResultCount` | 否 | 期望方案数，默认 2–3 |

**缺 `budget` / `city` / `storeKeyword` 中任何一项时，必须向用户提问。**
禁止猜测城市、禁止猜测门店。用户只说"给我推荐麦当劳"时，先问预算和门店线索。

> ⚠️ **「必须有」和「尽量有」是两回事，不许混用。**
> - 用户说「必须有饮料」「要带饮料」「一定要有喝的」→ `mustContainDrink = true`，
>   传给内核用 `--require-drink`；**不要**用 `--prefer-drink` 顶替。
> - 用户说「尽量丰富」「最好有饮料」「能加杯饮料就加」→ 这才是 `preferDrink = true`，用 `--prefer-drink`。
>
> 原因（P1.14 实测）：`--prefer-*` 只影响排序打分，**不保证结果里真的有饮料**。
> 预算 ¥25 时内核产出的候选里就有「麦香鸡 + 派」这种**不含饮料**的组合。
> 只有 `--require-*` 才会把不满足的组合整个过滤掉。

> ⚠️ **第一版的已知取舍：`mustContainBurger` 默认为 `true` 且内核不能关闭。**
> 内核 `search_plans` 以「已确认含汉堡」商品为锚点，因此**无法表达"这餐不要汉堡"**。
> 用户明确说「不要汉堡」「只想吃点小食」时，**当前无法满足** —— 必须如实说明，
> 不得假装支持、也不得偷偷塞一个汉堡进去。

### 第 2 步：查门店

调用 `query-nearby-stores`：

```
beType = 1
searchType = 2
city = <用户给的城市>
keyword = <用户给的门店关键词>
```

**实测约束**：`searchType=1`（收藏）在无收藏时返回
`success:false, code 600050, "收藏餐厅列表为空，请您提供城市+关键词进行搜索"`。
所以必须先拿到城市和关键词，**不能用门店关键词当门店编码**。

- 返回多个门店 → 列出 `storeName` / `address` / `storeCode`，让用户选；
- 只有一个明显匹配 → 可直接使用，但要说明用的是哪家；
- 到店场景返回的门店**没有 `beCode`**，后续调用**一律不要传 `beCode`**（传了会报错）。

### 第 3 步：查实时菜单

调用 `query-meals`：

```
storeCode  = <上一步的 storeCode>
orderType  = 1
beType     = 1
reservationDate = <yyyy-MM-dd HH:mm>   ← **仅预约场景传**；不预约时**不要传**
```

**每次运行都必须重新调用。** 不得复用历史价格，不得硬编码价格。

> ⚠️ **`reservationDate` 是「拿到目标时段菜单」的唯一开关，忘了传就会拿到当前时段的菜单。**
> 判断规则：用户提到**未来**的具体时间（"明早 8 点""今晚 9 点""后天中午"）→ 必须传；
> 用户说"现在""今天中午"，或不提时间 → 不传。
>
> P1.13/P1.14 实测证据：门店 `3230019` 不带预约参数返回**午餐**菜单；
> 带 `reservationDate=2026-10-17 08:00` 返回的是**另一套早餐菜单**（分类与商品编码都不同）。
> 反证：早餐商品 `9900003431` 不带预约参数核价返回 `60006 已售罄或不可售`，
> 带预约参数则 `success:true` —— 说明参数确实生效，不是"同一份菜单重跑"。
>
> ⚠️ **预约菜单 ≠ 到店当时菜单**，两者是否逐项一致**尚未验证**
> （此前已观察到"菜单切换晚于预约窗口起点"）。向用户输出时要说明这一限制。

> ⚠️ **菜单「现价」可能是条件价，不能直接当成交价。**
> 若某商品的 `discountType` 是「随单购麦金卡优惠」，它的 `currentPrice` **需要额外随单购会员卡才成立**，
> 真实无条件价是 `originalPrice`（实测 26/26：裸核价 == 原价；`9900000890` 菜单 ¥28 而实付 ¥58）。
> 覆盖午餐（16 条）与早餐（10 条）两个时段 ——
> ⚠️ 但 `discountType` 是**时段级**属性：`510479` 午餐是 `随单购麦金卡优惠`、早餐是 `随单购早餐卡优惠`，
> 两者语义一致，但**不可跨时段复用绝对价格**。  
> **预算筛选一律用 `prefilterPriceYuan`（本地内核已处理），不得用 `currentPrice` 判断"买得起"。**
> 遇到 `discountType` 是**本店没验证过的取值**（如「麦金卡优惠」「早餐卡优惠」）或缺少原价时，
> 必须说"这个价格无法本地判定"，把该组合**直接交给官方 `calculate-price`**，不得猜。
> 规则与全部证据：`references/menu-price-semantics.md`。

> ⚠️ **菜单随时段整体切换。** 麦当劳在 早餐 / 午餐 / 下午茶 / 夜市 / 宵夜 之间会整体换菜单。
> 如果在早餐时段查常规汉堡/套餐，`query-meal-detail` 会返回 `60001 未匹配到商品，请重新拉取全量商品列表`。
> **这表示该商品当前时段不在售，不表示商品不存在、也不表示映射表失效。**
> 遇到 `60001` 时：如实告知用户"当前时段不供应"，必要时换到处于常规时段的门店或建议稍后再试；
> **不要**去修改映射表，也**不要**把映射表里的商品删掉。
>
> ⚠️ **`60001` 有三个成因，必须先分辨。** ① **时段**：该商品当前时段不供应（换时段/换门店即可）；
> ② **编码轮换**：商品仍在售但换了新 code（查 `query-meals` 的 `data.meals` 找同名新编码）；
> ③ **真失效**：既不在菜单、也没有同名替代。**只有第 ③ 种才允许改映射表**，且必须留证据。
> 已确认轮换的旧编码在映射表里标了 `retired`，本地内核会**强制排除**它们 ——
> 如果 `plan.py coverage` 报出 `retiredInMenu` 非空，说明该编码又被启用，**必须重新核实**，
> 不得直接沿用旧的 `retired` 结论。

> ⚠️ **价格是门店级 + 时段级的。** 同一商品在不同门店价格可能不同（已实测），
> 部分商品编码在门店间也不一致。因此**不得把 A 门店的核价结果用在 B 门店**。

把返回写成脚本可读的文件（任选其一）：

- 原样保存工具返回文本；或
- 精简为 `{"items":[{"code":"<code>","name":"<name>","currentPrice":"<currentPrice>"}]}`；或
- 保存 `{"meals":{...}}` 结构。

写入临时文件，例如 `/tmp/mcd_menu.json`。

### 第 4 步：券盘点（**只读**，不领券）

调用三个**只读**工具，再交给 `plan.py coupons` 归一化：

| 工具 | 入参 | 能得到什么 |
|---|---|---|
| `query-store-coupons` | `storeCode`、`orderType=1`、`beType=1` | **唯一**能判断「本店本渠道可用」的来源；含 `couponId`/`couponCode`/有效期/适用商品编码 |
| `query-my-coupons` | 无 | 用户卡包。官方明确**不校验门店与渠道**，只说明"拥有它" |
| `available-coupons` | 无 | **未领取**的券。只有券名/图片/状态，**没有 `couponId`** |

把三者原始返回写入一个 JSON，**必须显式带查询状态**（否则无法区分"没券"和"查询失败"）：

```json
{
  "wallet":    {"status": "ok|empty|error|not_queried", "raw": "<query-my-coupons 原始返回>"},
  "store":     {"status": "ok|empty|error|not_queried", "raw": "<query-store-coupons 原始返回>"},
  "available": {"status": "ok|empty|error|not_queried", "raw": "<available-coupons 原始返回>"}
}
```

写入 `/tmp/mcd_coupons_raw.json`，然后：

```bash
python3 scripts/plan.py coupons \
  --input /tmp/mcd_coupons_raw.json --out /tmp/mcd_coupons.json
```

输出区分四类：`coupons`（已领取）、`storeUsable`（本店可用）、`walletOnly`（有券但不在本店清单里）、`unclaimed`（未领取）。

**四条纪律**：

1. **查询失败 ≠ 没有优惠券。** `status=error` 时脚本会给出告警，必须如实转述"查询失败、无法判断"，**不得**说成"你没有券"。
2. **卡包券 ≠ 本店可用券。** 只有当 `couponId` 同时出现在 `storeUsable` 里，才能说"这张券本店可用"。
3. **`available-coupons` 的券名绝不能当作可用于本单的优惠券。** 它们没有 `couponId`，无法送进算价，适用性恒为 `unassessable`。只能说"另有 N 张可领取券，但**我无法判断它们能否降低这单价格**"。
4. **此步骤不得调用任何写工具**，尤其禁止 `auto-bind-coupons`。

### 第 5 步：本地搜索候选

在 Skill 根目录（`SKILL.md` 所在目录）执行：

```bash
python3 scripts/plan.py candidates \
  --menu /tmp/mcd_menu.json \
  --map references/product-map.json \
  --budget <预算> \
  --store-code <storeCode> \
  [--coupons /tmp/mcd_coupons.json] \
  [--prefer-drink] [--prefer-side] \
  [--require-drink] [--require-side] \
  --out /tmp/mcd_candidates.json
```

> `--require-drink` / `--require-side` 是**硬约束**（不满足的整个组合被丢弃）；
> `--prefer-drink` / `--prefer-side` 只是**软偏好**（只影响排序）。两者**不可互换**。

脚本输出：
- `candidates[]`：最多 6 个候选（每个含 `id`、`items`、`menuTotalYuan`、`satisfies`）；
  每件商品带 `menuUnitPriceYuan`（菜单展示价）、`prefilterUnitPriceYuan`（预筛价）、`priceStatus`；
- `meta.conditionalPriceNotice`：条件价商品的计数与**逐个处理原因**
  （被预筛排除的、以及交官方核价的），**必须如实转述，不得静默省略**；
- `meta.candidateComposition`：候选分层计数 —— `credibleCount`（预筛价可判定）与
  `priceDeferredCount`（预筛价无法判定）。**候选席位优先给可信候选**，未定价候选只在剩余席位补齐；
- `meta.priceUndeterminedNotice`：当**所有**候选价格都无法本地判定时给出。
  此时必须如实说明「本地无法先行判断是否超预算」，**并说明这不等于无解**，
  不得把菜单价当成成交价来宣称"可行"；
- `meta.menuPriceAnomalies`：缺 `currentPrice` 等无法使用的条目（不静默丢弃）；
- `noSolutionAdvice`：无可行组合时的解释（含最便宜已确认汉堡价与差额）；
- 给了 `--coupons` 时，每个候选还会带 `couponMatches[]` 与 `probePlan`。

券匹配只看**商品编码是否有交集**（`code_matched`），**不代表"确定能省"**。
官方未给出适用商品编码的券标 `unknown`，默认**不试算**，且**不得**当作"全品适用"。

**此时的价格是菜单预估价，尚未核价，不得作为最终报价输出。**
`menuTotalYuan` 是**菜单展示价**合计（条件价商品会明显低于实付），
`prefilterTotalYuan` 才是预算判断依据；两者不一致时说明该候选含条件价商品，输出时要讲清楚。

### 第 6 步：官方核价

对 `candidates[]` **逐个**调用 `calculate-price`。

**不带券的基线是必需的**（否则无法计算实际节省）：

```
storeCode = <storeCode>
orderType = 1
beType     = 1
items = [{ "productCode": "<code>", "quantity": 1 }, ...]
```

再对 `probePlan.coupons` 里的每张券各调一次，在对应商品行加上 `couponId`：

```
items = [{ "productCode": "<code>", "quantity": 1, "couponId": "<couponId>" }, ...]
```

把返回写成 `/tmp/mcd_quotes.json`：

```json
{"quotes": {
  "C1": {
    "none":  { /* 不带券的真实返回 */ },
    "<couponId>": { /* 带券的真实返回 */ }
  }
}}
```

**注意**：`calculate-price` 不能省。它是唯一的价格权威来源。
一张券**没能核价成功**时，就如实按"无法验证"处理，**不得**声称省了钱。

### 第 7 步：校验与比较

没有券时：

```bash
python3 scripts/plan.py verify \
  --candidates /tmp/mcd_candidates.json --quotes /tmp/mcd_quotes.json \
  --budget <预算> --out /tmp/mcd_verified.json
```

有券时（推荐）：

```bash
python3 scripts/plan.py compare \
  --candidates /tmp/mcd_candidates.json --quotes /tmp/mcd_quotes.json \
  --budget <预算> --out /tmp/mcd_verified.json
```

`compare` 对每个候选输出**原价 / 优惠金额 / 应付金额 / 实际节省**四个金额，
其中**实际节省 = 同一组合的无券官方价 − 带券官方价**，`savingSource` 会标明来源。

**无券路径（`verify`）同样输出四金额**：`originalPriceYuan` / `discountYuan` /
`officialPriceYuan` / `menuTotalYuan`，全部直接取自官方 `calculate-price` 返回，
缺失时为 `null`（**不得**拿菜单价推算填补）。⚠️ 无券路径的 `usedCoupon` 恒为 `false`：
**`discountYuan > 0` 只可能来自店内促销自动生效，绝不能说成"用了优惠券"**
（`meta.discountNote` 会写明这一点）。

输出分区：`verified[]`、`rejected[]`（`over_budget` / `baseline_unavailable`）、
`unverifiableOptions[]`（带券核价失败，**不得计入已省**）、`discrepancies[]`。

> ⚠️ **两个子命令的排序规则不同，必须按用户诉求选读**（`meta.rankingRule` 会写明）：
> - `verify`（无券）= **丰富度优先**（品类覆盖 → 有效件数 → 剩余预算）。**第一条不一定最便宜**。
>   用户说"最省钱"时，应按 `officialPriceYuan` 升序自己取最小值；
> - `compare`（有券）= **官方应付价升序**。
>
> 不要因为某个方案排在第一就说它"最划算" —— 先看排序规则。

**只能把 `verified[]` 里的方案说成"已核价"。**

### 第 8 步：输出

按下面的模板输出，只输出 `verified[]` 中的方案。数量不足 2 个就如实少给，**不要凑数**。

## 输出格式

```
根据你选择的门店（<门店名>，storeCode=<storeCode>）和 ¥<预算> 预算，
我找到了以下经过官方核价的方案。渠道：到店自取。

方案 A：<styleHint>
- 商品组合：<商品名> ×1 + ...
- 原价：¥<originalPriceYuan>
- 优惠金额：¥<discountYuan>（<用了哪张券 / 未用券>）
- 应付金额：¥<priceYuan>
- 实际节省：¥<savingYuan>
- 剩余预算：¥<remainingYuan>
- 满足的要求：汉堡 / 饮料 / 小食（只列实际的）
- 说明：<一句话>

方案 B：...

---
核价门店：<storeName>（storeCode=<storeCode>）
使用渠道：到店自取（orderType=1, beType=1）
核价工具：calculate-price（官方 MCP），核价时间 <时间>
券的来源：query-store-coupons（本店可用）
尚未验证的信息：<例如：饮料/小食分类来自名称推断；映射表只覆盖 49/106 个商品；
                 本账号尚无优惠券样本，券路径未经真实核价验证>
```

**金额四件套是硬性要求。**

- 原价 / 优惠金额 / 应付金额**一律照抄官方 `calculate-price` 的返回**，不得自行推算；
  官方未返回该字段时如实写"官方未提供该字段"，**不得用菜单价顶替**。
- 无券路径：优惠金额 = 官方 `discountYuan`。**若 `discountYuan > 0`，必须注明它来自
  店内促销自动生效、不是优惠券**；若为 0，写 ¥0.00 并注明"本次未使用优惠券"。
- 实际节省 = 官方无券基线价 − 官方带券价。**没走过带券核价就写 ¥0.00**，
  并说明"未使用优惠券"，不得用 `discountYuan` 充作实际节省。

**如果有可领取的券但你没领**，追加一句话，不要写成推荐：

> 另有 N 张可领取券，但官方接口在你领取前不返回券 ID、优惠金额、门槛与适用商品，
> **我无法判断它们能否降低这单价格**。领取工具只能一次性领取全部，需要我尝试吗？

**如果没有券查询失败**，必须说清是失败：

> 券查询未成功（<工具名>），因此**无法判断**你是否已有可用优惠券 —— 这不等于你没有券。

## 无解时怎么说

用脚本给出的 `noSolutionAdvice`，并**必须**同时说明适用范围，例如：

> 在当前门店、当前渠道、且**已确认组成**的商品集合中，没有找到 ¥10 以内的含汉堡方案。
> 目前确认的最低价含汉堡候选是「牛肉堡单品精选」¥14.00，差 ¥4.00。
> 由于部分商品组成尚未核实，这不代表整个菜单已被证明无解。

- 只能放宽**可选偏好**（如"可以减少小食要求"），**绝不能擅自取消"必须有汉堡"**；
- 没有实际核价之前，不得说某个新方案"已经核价成功"。

## 硬性纪律（违反即算 bug）

1. **只有 `references/product-map.json` 中 `confidence = confirmed_detail` 且 `containsBurgerByDefault = true` 的商品，才能满足"必须含汉堡"。** 名称推断的商品永远不行。
2. **价格只来自实时菜单与 `calculate-price`。** 不得把任何历史价格写进代码或映射表。
   **菜单现价若是有条件价（`discountType` 含「随单购」等），不得把它当作无条件成交价**；
   预算预筛用 `prefilterPriceYuan`，语义未验证的一律不做本地判断（见 `references/menu-price-semantics.md`）。
3. **不得把套餐拆成子项计价。** 套餐按整份参与搜索。
4. **不得编造 MCP 调用或返回。** 没调过的就说没调过。
5. **"已核价"只能用在 `verified[]` 上；核价失败或超预算的方案不得冒充。**
6. **不做写操作**：不创建订单、不支付、**不领券**、不兑换积分。
7. **不泄露 Token**：`.workbuddy/mcp.json` 等含真实凭证的文件不得复制进项目、不得打印。
8. **不夸大**：不写"全网最低价""最划算""营养最佳"。丰富度是**本产品自定义的排序规则**，不是客观营养结论。
9. **不虚构产品定位**：不要说自己是"独一无二"的产品。

### 券相关的额外纪律

10. **不得把券面金额当作实际节省。** 实际节省只能等于「同一组合的无券官方价 − 带券官方价」，
    且两者都必须是真实核价成功的结果。
11. **不得把适用商品编码缺失的券当作"全品适用"。** 这类券标 `unknown`，默认不试算。
12. **不得把 `available-coupons` 的券名当作可用于本单的优惠券。** 未领取券恒为 `unassessable`。
13. **不得把券查询失败说成"没有优惠券"。** 失败就是失败，要如实说明无法判断。
14. **不得声称"全局最优"。** 只能说"在当前已搜索并核价的范围内最低"，
    并说明搜索范围（映射表覆盖度、探针上限、券信息缺失）。
15. **不得在用户未明确同意时领取优惠券。** 即使同意，也必须先说明"会一次性领取全部 N 张"。
16. **不得把店内促销说成优惠券。** 官方 `discount` 字段大于 0 可能只是促销自动生效；
    无券路径 `usedCoupon` 恒为 `false`。只有「带券核价成功且价格确实更低」才能说"用了券 / 省了多少"。
17. **候选价格无法本地判定时，不得说成"没有可行方案"。**
    `meta.priceUndeterminedNotice` 非空只代表"本地无法先行判断"，
    必须如实说明并指出这些候选仍要交给官方核价。

## 参考文件

| 文件 | 用途 |
|---|---|
| `references/product-map.json` | 已确认组成的商品映射（**不含价格**）+ 名称兜底分类规则 |
| `references/category-rules.md` | 分类规则说明、已知陷阱、如何维护映射表 |
| `references/menu-price-semantics.md` | **菜单价格字段语义（条件价）、预筛规则、候选分层规则 + 实测证据** |
| `references/coupon-schema.md` | **券的数据结构、状态枚举、匹配规则、金额单位约定** |
| `scripts/plan.py` | 本地内核：组合搜索 + 券匹配 + 核价比较（离线、无副作用） |
| `tests/TEST_RESULTS.md` | 真实测试记录（哪些通过、哪些未测） |
| `tests/fixtures/coupons/README.md` | 券测试样本的**来源标注**（真实 / 结构样本 / 模拟） |

## 遇到问题时

- 映射表里没有某商品的组成 → 它只能作为"未知"，**不得用于满足硬性条件**；可以调 `query-meal-detail` 补齐，但要把结果写回映射表并注明核验时间。
- 映射表与当前菜单不一致（编码消失/改名）→ 停止对该商品作确定性判断。
- `calculate-price` 返回 `success:false` → 按核价失败处理，不猜测原因、不重试超过一次。
- 券的适用商品编码为空 → 标 `unknown`，**不得**当成"全品适用"，默认不试算。
- 券查询工具报错 → 如实说"查询失败、无法判断"，**不得**说成"你没有券"。
- 用户问"帮我领券" → 先说明会**一次性领取全部可领券、且不可撤销**，取得明确同意后再执行；
  默认不做这一步。
- `query-meal-detail` 返回 `60001` → 先用 `query-meals` 分辨是"当前时段不供应"还是"编码已轮换"：
  - 时段问题 → 如实说明，不得改映射表；
  - 编码轮换 → **不得**用同名新编码顶替旧编码去核价（等于伪造来源）；旧编码标 `retired`、新编码**独立建条目**；
  - **均不得**断言"这个商品不存在"。
- 映射表里 `requiresUserSelection = true` 的商品（如 `9900004066 堡堡随心拼`）→
  需要用户在多选项里做选择，**不得**用于满足"必须含汉堡"；如果用户点名要它，
  必须先问清具体选哪些，并把选好的组合交给 `calculate-price` 核价。
- 映射表覆盖度落后于当前菜单（`plan.py coverage` 会报告）→ 可以补，但**必须有真实
  `query-meal-detail` 返回**才写回，且同步更新 `evidence.at`；**不得**仅凭名称或常识写入。
- 用户提出**蛋白偏好**（"只要牛肉堡""不要鱼堡"）→ 当前 **不支持**该筛选。
  「堡」在菜单分类里包含鱼堡（`双层深海鳕鱼堡`、`鱼排堡` 算汉堡），映射表里的 `proteinType`
  只是**纯标注**、不参与任何判定。必须如实说明"我无法按蛋白类型筛选"，**不得**假装已支持。
