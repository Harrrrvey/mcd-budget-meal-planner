#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_plan.py — 麦麦预算点餐师 的测试入口

只用标准库，不依赖 pytest（也可被 pytest 直接收集）。
运行：
    python3 tests/test_plan.py

覆盖内容：
  1. 结构一致性：映射表不含价格、格式合法
  2. 分类安全性：名称推断的商品永不满足硬性条件；已知反例被正确排除
  3. 搜索正确性：不超预算、不重复商品、一个方案最多一杯饮料
  4. 核价校验：区分 quote_missing / quote_failed / over_budget，只有核价成功且不超预算才算通过
  5. 无解场景：给出最低候选价与差额
  6. 端到端：用真实菜单快照 + 真实 calculate-price 记录，跑通 candidates → verify
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import plan  # noqa: E402  （来自 scripts/plan.py）

FIXTURE_MENU = ROOT / "tests/fixtures/menu_plan_fixture.json"
PRODUCT_MAP = ROOT / "references/product-map.json"

# ---------------------------------------------------------------------------
# 核价记录来源（QUOTES_CONTEXT）
# ---------------------------------------------------------------------------
# ⚠️ 核价结果是「门店级 + 时段级」的，**绝对不可跨门店/跨时段搬用**。
# 实测（2026-10-16）：同一商品在 3230019 与 3230442 价格不同，且商品编码集合也不同。
#
# 本文件包含两个批次，来源如下：
#
#   historical_2026-10-16_0220（10 条）
#     - 批次级来源：MCP_INTEGRATION.md §7.1–7.2 记录了该次会话使用
#       storeCode=3230019（麦当劳大连华南餐厅）、orderType=1、beType=1。
#     - ⚠️ 当时**未逐条采集**门店字段，因此这 10 条**没有**逐条 storeCode。
#       按 P1.6 规程「历史记录缺少门店信息不得凭猜测补齐」，**故意保持原样**。
#     - 时段：宵夜（00:00–04:45）；时间范围 02:20:11–02:46:21（+08:00）
#     - 菜单快照：**当时**是 02:19:53 的宵夜快照；该快照已在 P1.7 被刷新取代。
#     - ⚠️ 其中 8 条引用了现已确认失效/轮换的编码（见 RETIRED_SKUS）。
#       **保留它们是刻意的**：这是真实调用过的历史证据，不得因为编码轮换而删除或改写。
#       但它们的价格**不再**与当前菜单快照对应，任何新候选都不得复用这些记录。
#
#   batch_2026-10-16_1048（2 条，P1.6 新增）
#     - 逐条携带 storeCode / orderType / beType / batch，可逐条追溯。
#     - 门店：3230019；时段：午餐（10:44–14:15）；核价时间 10:48:14（+08:00）
#     - ⚠️ 该批对应的实时菜单（10:47）已含编码轮换后的新编码。
#
#   batch_2026-10-16_1100（12 条，P1.7 新增）
#     - 菜单快照刷新后重取，门店/时段与 tests/fixtures/menu_plan_fixture.json 一致。
#     - 首次取得真实 discount > 0（店内促销，非券）。
#
# 新增记录时的硬性要求：**必须**带 storeCode，且必须与本文件既有门店一致（有测试断言保证）。
# ---------------------------------------------------------------------------
QUOTES_CONTEXT = {
    "historical_2026-10-16_0220": {
        "provenance": "batch_level",
        "storeCode": "3230019",
        "storeName": "麦当劳大连华南餐厅",
        "orderType": 1,
        "beType": 1,
        "daypart": "宵夜（00:00–04:45）",
        "timeRange": "2026-10-16T02:20:11+08:00 ~ 2026-10-16T02:46:21+08:00",
        "menuSnapshot": "tests/fixtures/menu_plan_fixture.json",
        "evidence": "MCP_INTEGRATION.md §7.1–7.2",
        "caveat": ("批次级来源；当时未逐条采集门店字段，不得据此推断单条记录所属门店以外的信息。"
                   "⚠️ 其中 8 条引用了现已确认失效/轮换的旧编码（见 RETIRED_SKUS 与 product-map.json 的 codeRotation），"
                   "这些记录**刻意保留**为真实调用历史，但不再与当前菜单快照对应，任何新候选都不得复用。"),
    },
    "batch_2026-10-16_1048": {
        "provenance": "per_record",
        "storeCode": "3230019",
        "storeName": "麦当劳大连华南餐厅",
        "orderType": 1,
        "beType": 1,
        "daypart": "午餐（10:44–14:15）",
        "timeRange": "2026-10-16T10:48:14+08:00",
        "menuSnapshot": None,
        "evidence": "P1.6 本轮真实调用；tests/TEST_RESULTS.md §8",
        "caveat": "此批对应的是 10:47 的实时菜单。该次菜单已包含编码轮换后的新编码，其菜单价与随后刷新的 menu_plan_fixture.json（10:55 快照）一致。",
    },
    "batch_2026-10-16_1100": {
        "provenance": "per_record",
        "storeCode": "3230019",
        "storeName": "麦当劳大连华南餐厅",
        "orderType": 1,
        "beType": 1,
        "daypart": "午餐（10:44–14:15）",
        "timeRange": "2026-10-16T11:00:11+08:00 ~ 2026-10-16T11:00:22+08:00",
        "menuSnapshot": "tests/fixtures/menu_plan_fixture.json（10:55:39 快照，与本批同店同时段）",
        "evidence": "P1.7 本轮真实调用；tests/TEST_RESULTS.md §9",
        "caveat": ("这是**首次取得真实 discount > 0** 的一批（521986 的店内促销自动生效，非券）。"
                   "但**仍未取得任何真实的「带券核价」**：本账号券包与门店券官方返回都是空的。"),
    },
    "batch_2026-10-16_1114": {
        "provenance": "per_record",
        "storeCode": "3230019",
        "storeName": "麦当劳大连华南餐厅",
        "orderType": 1,
        "beType": 1,
        "daypart": "午餐（10:44–14:15）",
        "timeRange": "2026-10-16T11:14:20+08:00",
        "menuSnapshot": "tests/fixtures/menu_plan_fixture.json（10:55:39 快照，与本批同店同时段）",
        "evidence": "P1.8 本轮真实调用；tests/TEST_RESULTS.md §10",
        "caveat": ("映射表补齐 11 个疑似汉堡商品后重取。仍**没有**任何真实的带券核价 —— "
                   "本批中 discount > 0 的 2 条同样来自 521986 的**店内促销**，不是券。"),
    },
    "batch_2026-10-16_1125": {
        "provenance": "per_record",
        "storeCode": "3230019",
        "storeName": "麦当劳大连华南餐厅",
        "orderType": 1,
        "beType": 1,
        "daypart": "午餐（10:44–14:15）",
        "timeRange": "2026-10-16T11:25:42+08:00 ~ 2026-10-16T11:25:54+08:00",
        "menuSnapshot": "tests/fixtures/menu_plan_fixture.json（10:55:39 快照，与本批同店同时段）",
        "evidence": "P1.9 本轮真实调用；tests/TEST_RESULTS.md §11。用途：验证「菜单现价／原价／条件价」的字段语义。",
        "caveat": ("本批是**单商品裸核价**，用来证明字段语义而非提供候选报价："
                   "13 个 discountType = 随单购麦金卡优惠 的商品，裸核价**全部**等于 originalPrice；"
                   "促销优惠 507409 与无 discountType 的 9900005456 则等于 currentPrice。"
                   "仍**没有**任何真实的带券核价。"),
    },
    "batch_2026-10-16_1206": {
        "provenance": "per_record",
        "storeCode": "3230019",
        "storeName": "麦当劳大连华南餐厅",
        "orderType": 1,
        "beType": 1,
        "daypart": "午餐（10:44–14:15）",
        "timeRange": "2026-10-16T12:06:05+08:00 ~ 2026-10-16T12:06:07+08:00",
        "menuSnapshot": "tests/fixtures/menu_plan_fixture.json（11:55:12 快照，与本批同店同时段）",
        "evidence": "P1.12 本轮真实调用；tests/TEST_RESULTS.md §14。",
        "caveat": ("P1.12 新纳入映射的 3 个「随单购麦金卡优惠」商品的裸核价："
                   "9900000881 / 9900000882 / 9900006957 三者的 price 均为 6650 分，"
                   "**全部等于各自 originalPrice（6650 分）** → 把原价证据从 13 条扩到 16 条。"
                   "仍**没有**任何真实的带券核价。"),
    },
    "batch_2026-10-17_0800_reservation": {
        "provenance": "per_record",
        "storeCode": "3230019",
        "storeName": "麦当劳大连华南餐厅",
        "orderType": 1,
        "beType": 1,
        "daypart": "早餐（预约时段 2026-10-17 05:14–10:15）",
        "timeRange": "2026-10-16T13:15:02+08:00 ~ 2026-10-16T13:16:06+08:00",
        "menuSnapshot": None,
        "reservationDate": "2026-10-17 08:00",
        "evidence": "P1.13 本轮真实调用；tests/TEST_RESULTS.md §15",
        "caveat": ("⚠️ 本批是**跨时段证据**，取得方式与前几批**不同**，引用时必须带上这个前提：\n"
                   "  · 前几批是「当前时段菜单」的直接核价；\n"
                   "  · 本批是通过 `query-meals` / `calculate-price` 的 **`reservationDate` 参数**\n"
                    "    指定「2026-10-17 08:00」取得的**早餐时段**菜单与价格。\n"
                   "  · 已验证该参数确实生效：不带 `reservationDate` 核价早餐商品 `9900003431`\n"
                    "    会返回 `60006 商品已售罄或不可售`，带参数则 `success:true`。\n"
                   "  · 因此**不可**把它与午餐批次的记录混用比较绝对价格 ——\n"
                    "    它的用途是验证「条件价语义」，不是提供跨时段的候选报价。\n"
                   "仍**没有**任何真实的带券核价。"),
    },
}

# ---------------------------------------------------------------------------
# P1.9：条件价（menuPrice 是条件价）商品清单 —— 由真实 fixture 推导，不写死
# ---------------------------------------------------------------------------
# 判定依据见 references/menu-price-semantics.md。
CONDITIONAL_DISCOUNT_PREFIX = "随单购"

# ---------------------------------------------------------------------------
# P1.8 新增的 11 个商品（本轮从 coverage.priorityToVerify 清单核实并转正）
# ---------------------------------------------------------------------------
# 全部为**单选**结构；全部有真实 query-meal-detail 返回。
P1_8_NEW_CODES = ["1100", "4648", "9900003586", "9900005466", "9900005413",
                  "9900005411", "9900000888", "9900000886", "9900000893",
                  "9900010110", "9900016058"]

# 其中蛋白类型为「鱼」的 4 个（海鲜类堡）。它们**仍然算含汉堡**（菜单把牛/鱼/肉同列为「堡」），
# 但输出时应能说明这一点 —— 当前 Skill 不支持「只要牛肉堡」这类蛋白偏好（见 proteinTypeNote）。
FISH_BURGER_CODES = {"9900003586", "9900005413", "9900000893", "9900016058"}

# ---------------------------------------------------------------------------
# 已确认失效 / 已轮换的旧编码（必须与 references/product-map.json 的 retired 集合一致）
# ---------------------------------------------------------------------------
# 这些编码已不在 3230019 的在售清单中，且经真实返回确认（详见 product-map.json 的 codeRotation）。
# 它们**只能**出现在历史核价记录里；任何**带 batch 的新记录**都不得引用它们。
RETIRED_SKUS = {"9900015160", "9900015162", "9900015164",
                "9900016054", "9900015183", "9900015187", "9900015188"}

# 快照刷新前曾在菜单中、现已不存在，但**明确不标记为 retired** 的编码：
#   - 9900015165 派：有同名同价的新编码 9900008752，但未比对 rounds 结构 → 只记「可能轮换」
#   - 521143 4块麦乐鸡：午餐菜单无同名替代，缺原时段证据
#   - 521337 鲜萃冰咖：同名编码只作为套餐子项出现，无顶层替代编码
# 这三个既不能当作「仍在售」，也不能当作「已下架」，必须保留不确定性。
UNVERIFIABLE_SKUS = {"9900015165", "521143", "521337"}

# 真实 calculate-price 返回记录（按商品集合索引）
REAL_QUOTES: dict[frozenset, dict] = {
    frozenset(["9900015160"]): {
        "success": True, "code": 200, "at": "2026-10-16T02:20:11+08:00",
        "data": {"price": 2390, "productList": [
            {"productCode": "9900015160", "productName": "大堡口福三件套",
             "quantity": 1, "originalSubtotal": 2390, "subtotal": 2390}]}},
    frozenset(["9900015187", "9900015165"]): {
        "success": True, "code": 200, "at": "2026-10-16T02:45:48+08:00",
        "data": {"price": 2340, "productList": [
            {"productCode": "9900015187", "productName": "人气经典随心配",
             "quantity": 1, "originalSubtotal": 1490, "subtotal": 1490},
            {"productCode": "9900015165", "productName": "派",
             "quantity": 1, "originalSubtotal": 850, "subtotal": 850}]}},
    frozenset(["1450", "9900015165"]): {
        "success": True, "code": 200, "at": "2026-10-16T02:20:11+08:00",
        "data": {"price": 2500, "productList": [
            {"productCode": "1450", "productName": "麦香鸡",
             "quantity": 1, "originalSubtotal": 1650, "subtotal": 1650},
            {"productCode": "9900015165", "productName": "派",
             "quantity": 1, "originalSubtotal": 850, "subtotal": 850}]}},
    frozenset(["9900015160", "9900015165"]): {
        "success": True, "code": 200, "at": "2026-10-16T02:45:48+08:00",
        "data": {"price": 3240, "productList": [
            {"productCode": "9900015160", "productName": "大堡口福三件套",
             "quantity": 1, "originalSubtotal": 2390, "subtotal": 2390},
            {"productCode": "9900015165", "productName": "派",
             "quantity": 1, "originalSubtotal": 850, "subtotal": 850}]}},
    frozenset(["9900011128", "6102"]): {
        "success": True, "code": 200, "at": "2026-10-16T02:45:58+08:00",
        "data": {"price": 3500, "productList": [
            {"productCode": "9900011128", "productName": "“堡”你欢乐套餐",
             "quantity": 1, "originalSubtotal": 2800, "subtotal": 2800},
            {"productCode": "6102", "productName": "苹果片一袋(60g)",
             "quantity": 1, "originalSubtotal": 700, "subtotal": 700}]}},
    frozenset(["9900011128", "521143"]): {
        "success": True, "code": 200, "at": "2026-10-16T02:45:57+08:00",
        "data": {"price": 4000, "productList": [
            {"productCode": "9900011128", "productName": "“堡”你欢乐套餐",
             "quantity": 1, "originalSubtotal": 2800, "subtotal": 2800},
            {"productCode": "521143", "productName": "4块麦乐鸡",
             "quantity": 1, "originalSubtotal": 1200, "subtotal": 1200}]}},
    frozenset(["9900016054", "9900015165"]): {
        "success": True, "code": 200, "at": "2026-10-16T02:46:20+08:00",
        "data": {"price": 3200, "productList": [
            {"productCode": "9900016054", "productName": "汉堡开心乐园餐",
             "quantity": 1, "originalSubtotal": 2350, "subtotal": 2350},
            {"productCode": "9900015165", "productName": "派",
             "quantity": 1, "originalSubtotal": 850, "subtotal": 850}]}},
    frozenset(["9900016054", "6102", "9900015165"]): {
        "success": True, "code": 200, "at": "2026-10-16T02:46:21+08:00",
        "data": {"price": 3900, "productList": [
            {"productCode": "9900016054", "productName": "汉堡开心乐园餐",
             "quantity": 1, "originalSubtotal": 2350, "subtotal": 2350},
            {"productCode": "6102", "productName": "苹果片一袋(60g)",
             "quantity": 1, "originalSubtotal": 700, "subtotal": 700},
            {"productCode": "9900015165", "productName": "派",
             "quantity": 1, "originalSubtotal": 850, "subtotal": 850}]}},
    frozenset(["9900015160", "6102", "9900015165"]): {
        "success": True, "code": 200, "at": "2026-10-16T02:46:20+08:00",
        "data": {"price": 3940, "productList": [
            {"productCode": "9900015160", "productName": "大堡口福三件套",
             "quantity": 1, "originalSubtotal": 2390, "subtotal": 2390},
            {"productCode": "6102", "productName": "苹果片一袋(60g)",
             "quantity": 1, "originalSubtotal": 700, "subtotal": 700},
            {"productCode": "9900015165", "productName": "派",
             "quantity": 1, "originalSubtotal": 850, "subtotal": 850}]}},
    frozenset(["9900015183"]): {
        "success": True, "code": 200, "at": "2026-10-16T02:20:11+08:00",
        "data": {"price": 3500, "productList": [
            {"productCode": "9900015183", "productName": "精选单人餐三件套",
             "quantity": 1, "originalSubtotal": 3500, "subtotal": 3500}]}},
    # ---- P1.6 新增：逐条带来源（storeCode / orderType / beType / batch）----
    frozenset(["6102", "9900000890"]): {
        "success": True, "code": 200, "at": "2026-10-16T10:48:14+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1048", "daypart": "午餐",
        "data": {"price": 6500, "originalPrice": 6500, "discount": 0, "productList": [
            {"productCode": "6102", "productName": "苹果片一袋(60g)",
             "quantity": 1, "originalSubtotal": 700, "subtotal": 700},
            {"productCode": "9900000890", "productName": "板烧鸡腿堡四件套",
             "quantity": 1, "originalSubtotal": 5800, "subtotal": 5800}]}},
    frozenset(["6102", "9900000891"]): {
        "success": True, "code": 200, "at": "2026-10-16T10:48:14+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1048", "daypart": "午餐",
        "data": {"price": 6500, "originalPrice": 6500, "discount": 0, "productList": [
            {"productCode": "6102", "productName": "苹果片一袋(60g)",
             "quantity": 1, "originalSubtotal": 700, "subtotal": 700},
            {"productCode": "9900000891", "productName": "麦辣鸡腿堡四件套",
             "quantity": 1, "originalSubtotal": 5800, "subtotal": 5800}]}},
    # ---- 2026-10-16 11:00 批次（P1.7）：菜单快照刷新后重取的真实核价 ----
    frozenset(["9900008747"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:00:11+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1100", "daypart": "午餐",
        "data": {"price": 1400, "originalPrice": 1400, "discount": 0, "productList": [
            {"productCode": "9900008747", "productName": "牛肉堡单品精选",
             "quantity": 1, "originalSubtotal": 1400, "subtotal": 1400}]}},
    frozenset(["9900016057"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:00:11+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1100", "daypart": "午餐",
        "data": {"price": 2350, "originalPrice": 2350, "discount": 0, "productList": [
            {"productCode": "9900016057", "productName": "汉堡开心乐园餐",
             "quantity": 1, "originalSubtotal": 2350, "subtotal": 2350}]}},
    frozenset(["9900015602"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:00:11+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1100", "daypart": "午餐",
        "data": {"price": 2290, "originalPrice": 2290, "discount": 0, "productList": [
            {"productCode": "9900015602", "productName": "大堡口福三件套",
             "quantity": 1, "originalSubtotal": 2290, "subtotal": 2290}]}},
    frozenset(["1450", "9900008752"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:00:11+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1100", "daypart": "午餐",
        "data": {"price": 2500, "originalPrice": 2500, "discount": 0, "productList": [
            {"productCode": "1450", "productName": "麦香鸡",
             "quantity": 1, "originalSubtotal": 1650, "subtotal": 1650},
            {"productCode": "9900008752", "productName": "派",
             "quantity": 1, "originalSubtotal": 850, "subtotal": 850}]}},
    frozenset(["9900008752", "9900013304"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:00:12+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1100", "daypart": "午餐",
        "data": {"price": 2340, "originalPrice": 2340, "discount": 0, "productList": [
            {"productCode": "9900008752", "productName": "派",
             "quantity": 1, "originalSubtotal": 850, "subtotal": 850},
            {"productCode": "9900013304", "productName": "人气经典随心配",
             "quantity": 1, "originalSubtotal": 1490, "subtotal": 1490}]}},
    # ⭐ 首个真实 discount > 0 的官方案例：521986（马苏里拉拉丝芝士条）菜单价已是促销价 ¥11，
    #    calculate-price 把「原价 16 → 促销价 11」的 500 分差额记在 originalPrice/discount 上。
    #    这不是券的效果，而是店内促销自动生效 —— 输出时必须按字段含义分别引用。
    frozenset(["521986", "9900008747"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:00:11+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1100", "daypart": "午餐",
        "data": {"price": 2500, "originalPrice": 3000, "discount": 500, "productList": [
            {"productCode": "521986", "productName": "马苏里拉拉丝芝士条",
             "quantity": 1, "originalSubtotal": 1600, "subtotal": 1100},
            {"productCode": "9900008747", "productName": "牛肉堡单品精选",
             "quantity": 1, "originalSubtotal": 1400, "subtotal": 1400}]}},
    frozenset(["521986", "9900016057"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:00:22+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1100", "daypart": "午餐",
        "data": {"price": 3450, "originalPrice": 3950, "discount": 500, "productList": [
            {"productCode": "9900016057", "productName": "汉堡开心乐园餐",
             "quantity": 1, "originalSubtotal": 2350, "subtotal": 2350},
            {"productCode": "521986", "productName": "马苏里拉拉丝芝士条",
             "quantity": 1, "originalSubtotal": 1600, "subtotal": 1100}]}},
    frozenset(["514782", "9900016057"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:00:22+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1100", "daypart": "午餐",
        "data": {"price": 3950, "originalPrice": 3950, "discount": 0, "productList": [
            {"productCode": "514782", "productName": "脆脆薯条",
             "quantity": 1, "originalSubtotal": 1600, "subtotal": 1600},
            {"productCode": "9900016057", "productName": "汉堡开心乐园餐",
             "quantity": 1, "originalSubtotal": 2350, "subtotal": 2350}]}},
    frozenset(["521951", "521986", "9900016057"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:00:22+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1100", "daypart": "午餐",
        "data": {"price": 3950, "originalPrice": 4450, "discount": 500, "productList": [
            {"productCode": "521951", "productName": "灰焰圆筒",
             "quantity": 1, "originalSubtotal": 500, "subtotal": 500},
            {"productCode": "521986", "productName": "马苏里拉拉丝芝士条",
             "quantity": 1, "originalSubtotal": 1600, "subtotal": 1100},
            {"productCode": "9900016057", "productName": "汉堡开心乐园餐",
             "quantity": 1, "originalSubtotal": 2350, "subtotal": 2350}]}},
    # ---- 真实 over_budget 案例：菜单「现价¥28」是随单购麦金卡条件价，裸核价 ¥58 ----
    frozenset(["521951", "6102", "9900000890"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:00:22+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1100", "daypart": "午餐",
        "data": {"price": 7000, "originalPrice": 7000, "discount": 0, "productList": [
            {"productCode": "521951", "productName": "灰焰圆筒",
             "quantity": 1, "originalSubtotal": 500, "subtotal": 500},
            {"productCode": "6102", "productName": "苹果片一袋(60g)",
             "quantity": 1, "originalSubtotal": 700, "subtotal": 700},
            {"productCode": "9900000890", "productName": "板烧鸡腿堡四件套",
             "quantity": 1, "originalSubtotal": 5800, "subtotal": 5800}]}},
    frozenset(["521951", "6102", "9900000891"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:00:22+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1100", "daypart": "午餐",
        "data": {"price": 7000, "originalPrice": 7000, "discount": 0, "productList": [
            {"productCode": "521951", "productName": "灰焰圆筒",
             "quantity": 1, "originalSubtotal": 500, "subtotal": 500},
            {"productCode": "6102", "productName": "苹果片一袋(60g)",
             "quantity": 1, "originalSubtotal": 700, "subtotal": 700},
            {"productCode": "9900000891", "productName": "麦辣鸡腿堡四件套",
             "quantity": 1, "originalSubtotal": 5800, "subtotal": 5800}]}},
    frozenset(["521951", "6102", "9900011128"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:00:22+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1100", "daypart": "午餐",
        "data": {"price": 4000, "originalPrice": 4000, "discount": 0, "productList": [
            {"productCode": "521951", "productName": "灰焰圆筒",
             "quantity": 1, "originalSubtotal": 500, "subtotal": 500},
            {"productCode": "6102", "productName": "苹果片一袋(60g)",
             "quantity": 1, "originalSubtotal": 700, "subtotal": 700},
            {"productCode": "9900011128", "productName": "“堡”你欢乐套餐",
             "quantity": 1, "originalSubtotal": 2800, "subtotal": 2800}]}},
    # ---- 2026-10-16 11:14 批次（P1.8）：映射表补齐 11 个疑似汉堡商品后重取的真实核价 ----
    frozenset(["9900016058"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:14:20+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1114", "daypart": "午餐",
        "data": {"price": 2400, "originalPrice": 2400, "discount": 0, "productList": [
            {"productCode": "9900016058", "productName": "鱼排堡开心乐园餐",
             "quantity": 1, "originalSubtotal": 2400, "subtotal": 2400}]}},
    frozenset(["521986", "9900016058"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:14:20+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1114", "daypart": "午餐",
        "data": {"price": 3500, "originalPrice": 4000, "discount": 500, "productList": [
            {"productCode": "521986", "productName": "马苏里拉拉丝芝士条",
             "quantity": 1, "originalSubtotal": 1600, "subtotal": 1100},
            {"productCode": "9900016058", "productName": "鱼排堡开心乐园餐",
             "quantity": 1, "originalSubtotal": 2400, "subtotal": 2400}]}},
    frozenset(["521951", "9900016057"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:14:20+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1114", "daypart": "午餐",
        "data": {"price": 2850, "originalPrice": 2850, "discount": 0, "productList": [
            {"productCode": "521951", "productName": "灰焰圆筒",
             "quantity": 1, "originalSubtotal": 500, "subtotal": 500},
            {"productCode": "9900016057", "productName": "汉堡开心乐园餐",
             "quantity": 1, "originalSubtotal": 2350, "subtotal": 2350}]}},
    frozenset(["521951", "521986", "9900016058"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:14:20+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1114", "daypart": "午餐",
        "data": {"price": 4000, "originalPrice": 4500, "discount": 500, "productList": [
            {"productCode": "521951", "productName": "灰焰圆筒",
             "quantity": 1, "originalSubtotal": 500, "subtotal": 500},
            {"productCode": "521986", "productName": "马苏里拉拉丝芝士条",
             "quantity": 1, "originalSubtotal": 1600, "subtotal": 1100},
            {"productCode": "9900016058", "productName": "鱼排堡开心乐园餐",
             "quantity": 1, "originalSubtotal": 2400, "subtotal": 2400}]}},
    frozenset(["521951", "6102", "9900016057"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:14:20+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1114", "daypart": "午餐",
        "data": {"price": 3550, "originalPrice": 3550, "discount": 0, "productList": [
            {"productCode": "521951", "productName": "灰焰圆筒",
             "quantity": 1, "originalSubtotal": 500, "subtotal": 500},
            {"productCode": "6102", "productName": "苹果片一袋(60g)",
             "quantity": 1, "originalSubtotal": 700, "subtotal": 700},
            {"productCode": "9900016057", "productName": "汉堡开心乐园餐",
             "quantity": 1, "originalSubtotal": 2350, "subtotal": 2350}]}},
    frozenset(["514782", "9900016058"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:14:20+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1114", "daypart": "午餐",
        "data": {"price": 4000, "originalPrice": 4000, "discount": 0, "productList": [
            {"productCode": "514782", "productName": "脆脆薯条",
             "quantity": 1, "originalSubtotal": 1600, "subtotal": 1600},
            {"productCode": "9900016058", "productName": "鱼排堡开心乐园餐",
             "quantity": 1, "originalSubtotal": 2400, "subtotal": 2400}]}},
    # ---- 2026-10-16 11:25 批次（P1.9）：为验证「菜单价 / 原价 / 条件价」的字段语义，
    #      对全部 13 个 discountType = 随单购麦金卡优惠 的商品**逐一裸核价**，
    #      并各取 1 个「促销优惠」与「无 discountType」的对照商品。全部为单商品核价。----
    frozenset(["9900005456"]): {   # 对照：无 discountType，菜单价 ¥34 / 原价 ¥47.5
        "success": True, "code": 200, "at": "2026-10-16T11:25:42+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 3400, "originalPrice": 3400, "discount": 0, "productList": [
            {"productCode": "9900005456", "productName": "麦辣鸡腿汉堡三件套",
             "quantity": 1, "originalSubtotal": 3400, "subtotal": 3400}]}},
    frozenset(["507409"]): {      # 对照：促销优惠，菜单价 ¥13.9 / 原价 ¥21
        "success": True, "code": 200, "at": "2026-10-16T11:25:42+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 1390, "originalPrice": 2100, "discount": 710, "productList": [
            {"productCode": "507409", "productName": "麦咖啡™冰奶铁中杯",
             "quantity": 1, "originalSubtotal": 2100, "subtotal": 1390}]}},
    # 以下 13 条：菜单现价是「随单购麦金卡优惠」条件价，裸核价一律等于 originalPrice。
    frozenset(["9900004236"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:25:42+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 5800, "originalPrice": 5800, "discount": 0, "productList": [
            {"productCode": "9900004236", "productName": "人气超值四件套随心选",
             "quantity": 1, "originalSubtotal": 5800, "subtotal": 5800}]}},
    frozenset(["9900016309"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:25:54+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 5800, "originalPrice": 5800, "discount": 0, "productList": [
            {"productCode": "9900016309", "productName": "龙焰鸡腿堡四件套",
             "quantity": 1, "originalSubtotal": 5800, "subtotal": 5800}]}},
    frozenset(["9900016310"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:25:53+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 5800, "originalPrice": 5800, "discount": 0, "productList": [
            {"productCode": "9900016310", "productName": "龙焰芝士棒鸡腿堡四件套",
             "quantity": 1, "originalSubtotal": 5800, "subtotal": 5800}]}},
    frozenset(["9900016311"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:25:54+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 5800, "originalPrice": 5800, "discount": 0, "productList": [
            {"productCode": "9900016311", "productName": "龙焰美味四件套",
             "quantity": 1, "originalSubtotal": 5800, "subtotal": 5800}]}},
    frozenset(["9900010110"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:25:53+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 5950, "originalPrice": 5950, "discount": 0, "productList": [
            {"productCode": "9900010110", "productName": "鱼牛汉堡四件套随心选",
             "quantity": 1, "originalSubtotal": 5950, "subtotal": 5950}]}},
    frozenset(["9900000886"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:25:54+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 5950, "originalPrice": 5950, "discount": 0, "productList": [
            {"productCode": "9900000886", "productName": "不素之霸双层牛堡四件套",
             "quantity": 1, "originalSubtotal": 5950, "subtotal": 5950}]}},
    frozenset(["9900000888"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:25:54+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 5950, "originalPrice": 5950, "discount": 0, "productList": [
            {"productCode": "9900000888", "productName": "巨无霸四件套",
             "quantity": 1, "originalSubtotal": 5950, "subtotal": 5950}]}},
    frozenset(["9900000893"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:25:53+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 5950, "originalPrice": 5950, "discount": 0, "productList": [
            {"productCode": "9900000893", "productName": "双层深海鳕鱼堡四件套",
             "quantity": 1, "originalSubtotal": 5950, "subtotal": 5950}]}},
    frozenset(["510479"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:25:42+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 2300, "originalPrice": 2300, "discount": 0, "productList": [
            {"productCode": "510479", "productName": "冰燕麦奶铁中杯",
             "quantity": 1, "originalSubtotal": 2300, "subtotal": 2300}]}},
    frozenset(["9900011056"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:25:43+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 11650, "originalPrice": 11650, "discount": 0, "productList": [
            {"productCode": "9900011056", "productName": "全明星双人分享餐八件套",
             "quantity": 1, "originalSubtotal": 11650, "subtotal": 11650}]}},
    frozenset(["9900005321"]): {
        "success": True, "code": 200, "at": "2026-10-16T11:25:42+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1125", "daypart": "午餐",
        "data": {"price": 12450, "originalPrice": 12450, "discount": 0, "productList": [
            {"productCode": "9900005321", "productName": "BFF四宫格小食盘双人餐",
             "quantity": 1, "originalSubtotal": 12450, "subtotal": 12450}]}},
    # ---- P1.12：新纳入映射的 3 个「随单购麦金卡优惠」商品的裸核价 ----
    # 目的：把「原价 == 官方裸核价」的证据从 P1.9 的 13 条扩到 16 条 ——
    # 规则按 discountType **取值**匹配，但每一条新纳入的条件价商品仍应有自己的实测背书。
    frozenset(["9900000881"]): {
        "success": True, "code": 200, "at": "2026-10-16T12:06:05+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1206", "daypart": "午餐",
        "data": {"price": 6650, "originalPrice": 6650, "discount": 0, "productList": [
            {"productCode": "9900000881", "productName": "培根安格斯厚牛堡四件套",
             "quantity": 1, "originalSubtotal": 6650, "subtotal": 6650}]}},
    frozenset(["9900000882"]): {
        "success": True, "code": 200, "at": "2026-10-16T12:06:07+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1206", "daypart": "午餐",
        "data": {"price": 6650, "originalPrice": 6650, "discount": 0, "productList": [
            {"productCode": "9900000882", "productName": "芝士安格斯厚牛堡四件套",
             "quantity": 1, "originalSubtotal": 6650, "subtotal": 6650}]}},
    frozenset(["9900006957"]): {
        "success": True, "code": 200, "at": "2026-10-16T12:06:06+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-16_1206", "daypart": "午餐",
        "data": {"price": 6650, "originalPrice": 6650, "discount": 0, "productList": [
            {"productCode": "9900006957", "productName": "安格斯厚牛堡四件套随心选",
             "quantity": 1, "originalSubtotal": 6650, "subtotal": 6650}]}},
    # ---- P1.13：早餐时段（预约 2026-10-17 08:00）「随单购早餐卡优惠」裸核价 ----
    # 用途：**验证条件价语义跨时段成立**，不是给午餐候选提供报价。
    # 与 `随单购麦金卡优惠` 的差别只在卡类型，语义一致：裸核价 == originalPrice。
    frozenset(["9900003431"]): {
        "success": True, "code": 200, "at": "2026-10-16T13:15:02+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-17_0800_reservation", "daypart": "早餐",
        "data": {"price": 1350, "originalPrice": 1350, "discount": 0, "productList": [
            {"productCode": "9900003431", "productName": "大脆鸡扒麦满分组合",
             "quantity": 1, "originalSubtotal": 1350, "subtotal": 1350}]}},
    frozenset(["9900013411"]): {
        "success": True, "code": 200, "at": "2026-10-16T13:15:02+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-17_0800_reservation", "daypart": "早餐",
        "data": {"price": 2600, "originalPrice": 2600, "discount": 0, "productList": [
            {"productCode": "9900013411", "productName": "猪柳蛋麦满分套餐",
             "quantity": 1, "originalSubtotal": 2600, "subtotal": 2600}]}},
    # ⚠️ `510479 燕麦奶铁` **故意不在这里重复登记** —— 它已存在于
    #    `batch_2026-10-16_1125`（午餐，price 2300）且裸核价数值一致。
    #    Python 字典字面量重复键会静默覆盖，重复登记会把午餐记录错标成早餐。
    #    它的跨时段对照结论（同一商品 discountType 随时段变，但语义不变）
    #    记录在 CROSS_DAYPART_EVIDENCE 常量里，而不是再建一条核价记录。
    frozenset(["9900011708"]): {
        "success": True, "code": 200, "at": "2026-10-16T13:15:03+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-17_0800_reservation", "daypart": "早餐",
        "data": {"price": 1350, "originalPrice": 1350, "discount": 0, "productList": [
            {"productCode": "9900011708", "productName": "火腿扒麦满分组合",
             "quantity": 1, "originalSubtotal": 1350, "subtotal": 1350}]}},
    frozenset(["9900003453"]): {
        "success": True, "code": 200, "at": "2026-10-16T13:16:05+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-17_0800_reservation", "daypart": "早餐",
        "data": {"price": 1700, "originalPrice": 1700, "discount": 0, "productList": [
            {"productCode": "9900003453", "productName": "吉士蛋麦满分套餐",
             "quantity": 1, "originalSubtotal": 1700, "subtotal": 1700}]}},
    frozenset(["9900003457"]): {
        "success": True, "code": 200, "at": "2026-10-16T13:16:05+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-17_0800_reservation", "daypart": "早餐",
        "data": {"price": 2300, "originalPrice": 2300, "discount": 0, "productList": [
            {"productCode": "9900003457", "productName": "原味板烧鸡腿麦满分套餐",
             "quantity": 1, "originalSubtotal": 2300, "subtotal": 2300}]}},
    frozenset(["9900003458"]): {
        "success": True, "code": 200, "at": "2026-10-16T13:16:05+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-17_0800_reservation", "daypart": "早餐",
        "data": {"price": 2600, "originalPrice": 2600, "discount": 0, "productList": [
            {"productCode": "9900003458", "productName": "猪柳蛋麦满分套餐",
             "quantity": 1, "originalSubtotal": 2600, "subtotal": 2600}]}},
    frozenset(["9900003459"]): {
        "success": True, "code": 200, "at": "2026-10-16T13:16:05+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-17_0800_reservation", "daypart": "早餐",
        "data": {"price": 2700, "originalPrice": 2700, "discount": 0, "productList": [
            {"productCode": "9900003459", "productName": "双层原味板烧鸡腿麦满分套餐",
             "quantity": 1, "originalSubtotal": 2700, "subtotal": 2700}]}},
    frozenset(["9900003469"]): {
        "success": True, "code": 200, "at": "2026-10-16T13:16:05+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-17_0800_reservation", "daypart": "早餐",
        "data": {"price": 2450, "originalPrice": 2450, "discount": 0, "productList": [
            {"productCode": "9900003469", "productName": "图林根香肠早安营养卷套餐",
             "quantity": 1, "originalSubtotal": 2450, "subtotal": 2450}]}},
    frozenset(["9900012729"]): {
        "success": True, "code": 200, "at": "2026-10-16T13:16:05+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-17_0800_reservation", "daypart": "早餐",
        "data": {"price": 1100, "originalPrice": 1100, "discount": 0, "productList": [
            {"productCode": "9900012729", "productName": "脆香油条组合",
             "quantity": 1, "originalSubtotal": 1100, "subtotal": 1100}]}},
    # 对照组：早餐时段的「促销优惠」与「无 discountType」
    frozenset(["507328"]): {
        "success": True, "code": 200, "at": "2026-10-16T13:16:06+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-17_0800_reservation", "daypart": "早餐",
        "data": {"price": 1390, "originalPrice": 2100, "discount": 710, "productList": [
            {"productCode": "507328", "productName": "麦咖啡™热奶铁中杯",
             "quantity": 1, "originalSubtotal": 2100, "subtotal": 1390}]}},
    frozenset(["5022"]): {
        "success": True, "code": 200, "at": "2026-10-16T13:15:03+08:00",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "batch": "batch_2026-10-17_0800_reservation", "daypart": "早餐",
        "data": {"price": 1800, "originalPrice": 1800, "discount": 0, "productList": [
            {"productCode": "5022", "productName": "猪柳蛋麦满分",
             "quantity": 1, "originalSubtotal": 1800, "subtotal": 1800}]}},
}

# ---------------------------------------------------------------------------
# 跨时段证据（P1.13）
# ---------------------------------------------------------------------------
# 目的：证明「随单购*」条件价的语义（裸核价 == originalPrice）**不只在午餐成立**。
#
# 取得方式：门店 `3230019` 全天营业（00:00–23:59），`query-meals` / `calculate-price`
# 都支持 `reservationDate`，于是用 **预约明天早餐时段** 的方式取得真实的早餐菜单。
#
# ⚠️ 已先验证该参数确实生效（否则"早餐"证据不成立）：
#    同一商品 `9900003431 大脆鸡扒麦满分组合`
#      · 不带 `reservationDate` 核价 → `success:false, code 60006`（当前午餐时段不售）
#      · 带 `reservationDate=2026-10-17 08:00` 核价 → `success:true, price 1350`
#    → 菜单确实按时段整体切换，本批是**真实早餐时段**数据，不是同一份菜单重跑。
#
# ★ 最有价值的一条对照：`510479 燕麦奶铁` 在两个时段的 discountType **不同**
#     （午餐 `随单购麦金卡优惠` / 早餐 `随单购早餐卡优惠`），
#     但两次裸核价**都等于 originalPrice ¥23.00**
#   → 说明条件价语义是"随单购"这一类共有的，**跨时段成立**；
#     同时说明 **discountType 是时段级属性，不是商品固有属性**。
CROSS_DAYPART_EVIDENCE = {
    "storeCode": "3230019",
    "storeName": "麦当劳大连华南餐厅",
    "orderType": 1,
    "beType": 1,
    "breakfast": {
        "reservationDate": "2026-10-17 08:00",
        "queriedAt": "2026-10-16T13:13:49+08:00",
        "daypartLabel": "早餐（05:14–10:15）",
        "evidenceType": "REAL_LIVE",
    },
    "lunch": {
        "reservationDate": None,
        "queriedAt": "2026-10-16T13:13:50+08:00",
        "daypartLabel": "午餐（10:44–14:15）",
        "evidenceType": "REAL_LIVE",
    },
    # 同一商品、两个时段、两种 discountType，裸核价都 == originalPrice
    "sameItemDifferentDiscountType": [
        {
            "code": "510479",
            "menuNameLunch": "燕麦奶铁",
            "discountTypeLunch": "随单购麦金卡优惠",
            "quoteLunch": 2300,
            "menuNameBreakfast": "燕麦奶铁",
            "discountTypeBreakfast": "随单购早餐卡优惠",
            "quoteBreakfast": 2300,
            "originalPriceYuan": 23.0,
            "currentPriceYuan": 9.9,
            "batchLunch": "batch_2026-10-16_1125",
            "batchBreakfast": "batch_2026-10-17_0800_reservation",
        },
    ],
    # 早餐菜单里观察到 `discountType = 促销优惠` 的商品（REAL_LIVE）
    # 注意与午餐快照的集合**不同**：早餐是热饮（507328/507368），午餐是小食（521986）
    "breakfastPromoCodes": ["507328", "507368", "507409", "507411"],
    # 早餐时段实测的「随单购早餐卡优惠」样本（10/10 price == originalPrice）
    "breakfastConditionalSamples": [
        ("9900003431", "大脆鸡扒麦满分组合", 8.1, 13.5, 1350),
        ("9900013411", "精选麦满分套餐", 15.6, 26.0, 2600),
        ("510479", "燕麦奶铁", 9.9, 23.0, 2300),
        ("9900011708", "火腿扒麦满分组合", 8.1, 13.5, 1350),
        ("9900003453", "吉士蛋麦满分套餐", 10.2, 17.0, 1700),
        ("9900003457", "原味板烧鸡腿麦满分套餐", 13.8, 23.0, 2300),
        ("9900003458", "猪柳蛋麦满分套餐", 15.6, 26.0, 2600),
        ("9900003459", "双层原味板烧鸡腿麦满分套餐", 16.2, 27.0, 2700),
        ("9900003469", "图林根香肠早安营养卷套餐", 14.7, 24.5, 2450),
        ("9900012729", "脆香油条组合", 6.6, 11.0, 1100),
    ],
    # 早餐时段对照组：促销价 / 无优惠声明 → 裸核价 == 菜单现价
    "breakfastControlSamples": [
        ("507328", "麦咖啡™热奶铁中杯", 13.9, 21.0, 1390, 710),
        ("507368", "麦咖啡™热美式中杯", 11.9, 19.0, 1190, 710),
        ("5022", "猪柳蛋麦满分", 18.0, 18.0, 1800, 0),
    ],
    # 未验证的官方取值 —— 这两个**不带「随单购」前缀**，至今零样本
    "unverifiedDiscountTypes": ["麦金卡优惠", "早餐卡优惠"],
}

# ---------------------------------------------------------------------------
# 编码轮换证据（P1.6）—— **不是** fixture 候选的核价记录，严禁混入 REAL_QUOTES
# ---------------------------------------------------------------------------
# 2026-10-16 10:48 实测：旧编码在常规菜单中已下架（query-meal-detail 返回 60001），
# 而**同名新编码**可正常核价且价格与 fixture 里的旧编码完全一致
# → 证明是 **SKU 编码轮换**，不是商品下线。
#
# 为什么不放进 REAL_QUOTES：
#   ① 它们不对应 tests/fixtures/menu_plan_fixture.json 的任何候选；
#   ② 它们属于午餐时段，而 fixture 是宵夜时段快照，混入会造成跨时段串味。
# 之所以要单独记录：P1.6 要求「每次真实调用后保留返回的必要字段与来源」。
CODE_ROTATION_EVIDENCE = {
    "9900015162->9900008747": {
        "retiredCode": "9900015162", "retiredName": "牛肉堡单品精选",
        "currentCode": "9900008747", "currentName": "牛肉堡单品精选",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "at": "2026-10-16T10:48:14+08:00",
        "retiredProbeAt": "2026-10-16T10:48:27+08:00",
        "retiredProbeResult": "success:false, code 60001, 未匹配到商品，请重新拉取全量商品列表",
        "price": 1400, "originalPrice": 1400, "discount": 0,
        "fixturePriceOfRetiredCode": 1400,
        "verdict": "同名同价，判定为编码轮换",
    },
    "9900016054->9900016057": {
        "retiredCode": "9900016054", "retiredName": "汉堡开心乐园餐",
        "currentCode": "9900016057", "currentName": "汉堡开心乐园餐",
        "storeCode": "3230019", "orderType": 1, "beType": 1,
        "at": "2026-10-16T10:48:14+08:00",
        "retiredProbeAt": "2026-10-16T10:47:37+08:00",
        "retiredProbeResult": "success:false, code 60001, 未匹配到商品，请重新拉取全量商品列表",
        "price": 2350, "originalPrice": 2350, "discount": 0,
        "fixturePriceOfRetiredCode": 2350,
        "verdict": "同名同价，判定为编码轮换",
    },
}

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(cond), detail))


def load_pool():
    menu = plan.extract_menu(plan.load_json(FIXTURE_MENU))
    pmap = plan.load_json(PRODUCT_MAP)
    return menu, pmap, plan.build_pool(menu, pmap)


# ---------------------------------------------------------------- 1. 结构一致性
def test_product_map_has_no_price():
    pmap = plan.load_json(PRODUCT_MAP)
    raw = PRODUCT_MAP.read_text(encoding="utf-8")
    banned = ["price", "Price", "价格"]
    # 只允许出现在说明性文本里，不允许出现价格数值字段
    for key in ("priceYuan", "currentPrice", "originalPrice", "\"price\""):
        check(f"映射表不含价格字段 {key}", key not in raw)
    for rec in pmap["verifiedProducts"].values():
        check(f"{rec['nameFromMenu']} 记录不含价格键",
              not any(k in rec for k in ("price", "priceYuan", "currentPrice")))
    check("映射表已标注 Schema 版本", "schemaVersion" in pmap)


# ---------------------------------------------------------- 2. 分类安全性
def test_classification_safety():
    _, _, pool = load_pool()
    inferred = [i for i in pool if not i["verified"]]
    check("存在名称推断商品（池非空）", len(inferred) > 0, f"{len(inferred)} 条")
    check("名称推断商品一律不满足硬性条件",
          all(not i["canSatisfyBurgerConstraint"] for i in inferred))

    by_code = {i["code"]: i for i in pool}
    for code, name in [("9900011126", "酥脆无双套餐"), ("9900011123", "“辣”么快乐套餐"),
                       ("9900004835", "麦乐鸡套餐"), ("9900015568", "精选超值随心配")]:
        item = by_code.get(code)
        check(f"反例「{name}」不被当作含汉堡",
              item is not None and not item["canSatisfyBurgerConstraint"])

    # 改用**当前在售**的编码：旧编码 9900015160 / 9900015183 / 9900015187 已在 P1.7
    # 确认失效并被排除，继续断言它们会测出一个「数据已消失」的假失败。
    for code, name in [("9900015602", "大堡口福三件套"), ("9900004064", "精选单人餐三件套"),
                       ("9900013304", "人气经典随心配"), ("1450", "麦香鸡"),
                       ("1406", "板烧鸡腿堡")]:
        item = by_code.get(code)
        check(f"「{name}」可用于满足硬性条件",
              item is not None and item["canSatisfyBurgerConstraint"])

    check("「蘸酱炸鸡」未被「酱」规则误排除",
          "9900016077" in by_code and by_code["9900016077"]["categories"]["side"])
    check("「北美招牌牧场酱」被正确排除", "521797" not in by_code)

    # 池规模改为**由数据推导**，不再硬编码历史快照的条目数（原断言写死 64）。
    menu = plan.extract_menu(plan.load_json(FIXTURE_MENU))
    rules = plan.load_json(PRODUCT_MAP)["atomicNameRules"]
    excluded = [c for c in menu if plan.name_classify(menu[c]["name"], rules) == "exclude"]
    # P1.12：菜单演进后 exclude 项不再只有调味酱，还包含周边（帽 / 贴 / 徽章）。
    # 真正的不变量是「被排除的必须都是非餐品」，而不是写死具体编码。
    check("被排除的全部是非餐品（调味酱后缀 / 周边关键词）且至少 2 项",
          len(excluded) >= 2
          and all(plan.name_classify(menu[c]["name"], rules) == "exclude" for c in excluded),
          str([(c, menu[c]["name"]) for c in sorted(excluded)]))
    check("调料酱类仍被正确排除（suffix 规则的回归保护）",
          {"521797", "521799"}.issubset(set(excluded)), str(sorted(excluded)))
    check("周边类（帽 / 贴 / 徽章）也被排除，不参与组合",
          {"521929", "521946", "521955"}.issubset(set(excluded)), str(sorted(excluded)))
    check("商品池规模 == 菜单商品数 − 被排除数",
          len(pool) == len(menu) - len(excluded),
          f"池 {len(pool)} vs 菜单 {len(menu)} − 排除 {len(excluded)}")


# ------------------------------------------------------------ 3. 搜索正确性
def test_search_constraints():
    _, _, pool = load_pool()
    budget = 25.0
    plans = plan.dedupe_plans(plan.search_plans(pool, budget, True, True))
    check("25 元能搜到可行组合", len(plans) > 0, f"{len(plans)} 个")
    check("不产生超预算组合",
          all(sum(i["priceYuan"] for i in p) <= budget + 0.005 for p in plans))
    check("组合内不重复同一商品编码",
          all(len({i["code"] for i in p}) == len(p) for p in plans))
    check("每个组合都含至少一个已确认汉堡",
          all(any(i["canSatisfyBurgerConstraint"] for i in p) for p in plans))

    # 饮料上限：锚点已含饮料时不得再追加；否则最多 1 杯
    for p in plans:
        drinks = [i for i in p if i["categories"].get("drink")]
        if p[0]["categories"].get("drink"):
            check("锚点含饮料时不再追加饮料", len(drinks) == 1)
        else:
            check("非锚点饮料最多 1 项", len(drinks) <= 1)

    no_plan = plan.dedupe_plans(plan.search_plans(pool, 10.0, False, False))
    check("预算 10 元时无可行组合", len(no_plan) == 0, f"实际 {len(no_plan)} 个")


# ------------------------------------------------------------ 4. 核价校验
def test_quote_parsing():
    ok = plan._parse_quote({"success": True, "data": {"price": 2390}})
    failed = plan._parse_quote({"success": False, "code": 60006,
                                "message": "商品已售罄或不可售，换一个商品或检查orderType/beType"})
    missing = plan._parse_quote(None)
    check("正常返回解析出 price", ok is not None and ok.get("price") == 2390)
    check("失败返回被识别为「真实调用但失败」",
          failed is not None and failed.get("success") is False and failed.get("code") == 60006)
    check("无返回记录解析为 None", missing is None)


def _quote_cents(raw):
    """从一条核价记录里取官方应付价（分）。取不到（缺失/失败）返回 None。"""
    if not isinstance(raw, dict) or raw.get("success") is False:
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        return None
    price = data.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    return int(price)


def test_end_to_end_with_real_quotes():
    """端到端：真实菜单快照 + 真实核价记录，跑通 candidates → verify。

    ⚠️ 语义变更说明（P1.6，有实测证据）：
      旧断言是「核价通过 ≥ 3 个」。这是**历史候选快照预期**，同时依赖
      「搜索选中了哪些候选」与「这些候选恰好有核价记录」，**不是业务不变量**。
      P1.6 实测证明它**已不可能成立**：本 fixture 的候选中，
      `9900015160` / `9900015162` / `9900015164` / `9900016054` 等编码
      在门店 3230019 的当前常规菜单中**已下架**（2026-10-16 10:48 实测
      `query-meal-detail` 返回 `60001 未匹配到商品`），
      因此这些候选**永远拿不到真实核价**，只能被判 `quote_missing`。
      （同一批实测也确认了对照商品仍可正常返回，排除了"工具故障"这一解释。）

      新断言改为验证**业务不变量**（更强、与候选排序无关）：
        ① `verified` 集合 == 「核价成功且不超预算」的候选集合（由数据推导，不硬编码数量）
        ② 通过的方案一律不超预算，且价格一律来自 `calculate-price`
        ③ 无核价记录的候选必须判 `quote_missing`，绝不进入 `verified`
        ④ 官方价超预算的候选必须判 `over_budget`，绝不进入 `verified`
        ⑤ 菜单价 ≠ 官方价的候选必须**如实记入** `discrepancies`，且 delta 计算正确
        ⑥ 采用的价格一律等于官方核价（官方价优先，不得静默采用菜单价）
    """
    import tempfile

    for budget in (25.0, 35.0, 40.0):
        menu = plan.extract_menu(plan.load_json(FIXTURE_MENU))
        pmap = plan.load_json(PRODUCT_MAP)
        pool = plan.build_pool(menu, pmap)
        plans = plan.dedupe_plans(plan.search_plans(pool, budget, True, True))
        selected = plan.select_candidates(plans, budget, True, True, plan.DEFAULT_MAX_CANDIDATES)
        candidates = [{
            "id": e["id"], "styleHint": e.get("styleHint"),
            "items": [{"code": i["code"], "name": i["name"], "qty": 1,
                       "menuUnitPriceYuan": i["priceYuan"]} for i in e["items"]],
            "menuTotalYuan": e["metrics"]["menuTotalYuan"],
            "satisfies": e["metrics"]["categories"],
        } for e in selected]
        quotes = {c["id"]: REAL_QUOTES[frozenset(i["code"] for i in c["items"])]
                  for c in candidates if frozenset(i["code"] for i in c["items"]) in REAL_QUOTES}

        with tempfile.TemporaryDirectory() as td:
            cp, qp, op = Path(td) / "c.json", Path(td) / "q.json", Path(td) / "o.json"
            cp.write_text(json.dumps({"candidates": candidates}), encoding="utf-8")
            qp.write_text(json.dumps({"quotes": quotes}), encoding="utf-8")
            plan.cmd_verify(type("A", (), {"candidates": str(cp), "quotes": str(qp),
                                           "budget": budget, "out": str(op)})())
            out = json.loads(op.read_text(encoding="utf-8"))

        tag = f"预算 {budget:.0f} 元"
        budget_cents = int(round(budget * 100))
        menu_total = {c["id"]: c["menuTotalYuan"] for c in candidates}
        cents = {cid: _quote_cents(raw) for cid, raw in quotes.items()}
        ok_ids = {cid for cid in quotes if cents[cid] is not None and cents[cid] <= budget_cents}
        over_ids = {cid for cid in quotes if cents[cid] is not None and cents[cid] > budget_cents}
        missing_ids = set(menu_total) - set(quotes)
        got = {v["id"] for v in out["verified"]}
        rej = {r["id"]: r["status"] for r in out["rejected"]}

        check(f"{tag}：verified 集合 == 「核价成功且不超预算」的候选集合（由数据推导）",
              got == ok_ids, f"verified={sorted(got)} 期望={sorted(ok_ids)}")
        check(f"{tag}：通过的方案全部不超预算",
              all(v["withinBudget"] for v in out["verified"]))
        check(f"{tag}：通过的方案价格一律来自官方 calculate-price",
              all(v["officialPriceSource"] == "calculate-price" for v in out["verified"]))
        check(f"{tag}：无核价记录的候选一律判 quote_missing，且绝不进入 verified",
              all(rej.get(cid) == "quote_missing" for cid in missing_ids)
              and not (missing_ids & got), f"missing={sorted(missing_ids)}")
        check(f"{tag}：官方价超预算的候选一律判 over_budget，且绝不进入 verified",
              all(rej.get(cid) == "over_budget" for cid in over_ids) and not (over_ids & got),
              f"over={sorted(over_ids)}")

        disc = {d["id"]: d for d in out["discrepancies"]}
        expected_disc = {cid for cid in quotes
                         if cents[cid] is not None
                         and abs(cents[cid] / 100 - menu_total[cid]) > 0.005}
        check(f"{tag}：菜单价 ≠ 官方价的候选已全部如实记入 discrepancies",
              set(disc) == expected_disc,
              f"实际={sorted(disc)} 期望={sorted(expected_disc)}")
        check(f"{tag}：discrepancies 的 delta 计算正确（official − menu）",
              all(abs(d["deltaYuan"] - (d["officialPriceYuan"] - d["menuTotalYuan"])) <= 0.005
                  for d in out["discrepancies"]))
        check(f"{tag}：官方价优先——采用的价格一律等于官方核价",
              all(abs(v["officialPriceYuan"] - cents[v["id"]] / 100) <= 0.005
                  for v in out["verified"]))


def test_reject_paths():
    import tempfile

    # over_budget：官方价 35.00 > 预算 25
    cand = {"candidates": [{"id": "H1", "items": [
        {"code": "9900015183", "name": "精选单人餐三件套", "qty": 1, "menuUnitPriceYuan": 35.0}],
        "menuTotalYuan": 35.0, "satisfies": {"burger": True, "drink": True, "side": True}}]}
    quotes = {"quotes": {"H1": REAL_QUOTES[frozenset(["9900015183"])]}}
    with tempfile.TemporaryDirectory() as td:
        cp, qp, op = Path(td) / "c.json", Path(td) / "q.json", Path(td) / "o.json"
        cp.write_text(json.dumps(cand), encoding="utf-8")
        qp.write_text(json.dumps(quotes), encoding="utf-8")
        plan.cmd_verify(type("A", (), {"candidates": str(cp), "quotes": str(qp),
                                       "budget": 25.0, "out": str(op)})())
        out = json.loads(op.read_text(encoding="utf-8"))
    check("超预算方案被拒绝", out["meta"]["verifiedCount"] == 0
          and out["rejected"][0]["status"] == "over_budget")

    # quote_failed：真实的不可售商品返回
    cand = {"candidates": [{"id": "G1", "items": [
        {"code": "0000000000", "name": "不存在的商品编码", "qty": 1, "menuUnitPriceYuan": 10.0}],
        "menuTotalYuan": 10.0, "satisfies": {"burger": True}}]}
    quotes = {"quotes": {"G1": {"success": False, "code": 60006,
                                "message": "商品已售罄或不可售，换一个商品或检查orderType/beType"}}}
    with tempfile.TemporaryDirectory() as td:
        cp, qp, op = Path(td) / "c.json", Path(td) / "q.json", Path(td) / "o.json"
        cp.write_text(json.dumps(cand), encoding="utf-8")
        qp.write_text(json.dumps(quotes), encoding="utf-8")
        plan.cmd_verify(type("A", (), {"candidates": str(cp), "quotes": str(qp),
                                       "budget": 25.0, "out": str(op)})())
        out = json.loads(op.read_text(encoding="utf-8"))
    check("核价失败方案被识别为 quote_failed（不是 quote_missing）",
          out["rejected"][0]["status"] == "quote_failed")
    check("核价失败方案不得出现在已核价列表", out["meta"]["verifiedCount"] == 0)


def test_no_solution_advice(capsys=None):
    import tempfile
    menu_path = FIXTURE_MENU
    with tempfile.TemporaryDirectory() as td:
        op = Path(td) / "o.json"
        plan.cmd_candidates(type("A", (), {
            "menu": str(menu_path), "map": str(PRODUCT_MAP), "budget": 10.0,
            "prefer_drink": True, "prefer_side": False, "max_items": 4,
            "max_candidates": 6, "store_code": "3230019", "out": str(op)})())
        out = json.loads(op.read_text(encoding="utf-8"))
    advice = out["noSolutionAdvice"]
    check("预算 10 元时给出无解说明", out["candidates"] == [] and advice is not None)
    check("无解说明含最低已确认汉堡价",
          advice["cheapestConfirmedBurger"]["name"] == "牛肉堡单品精选")
    check("无解说明含差额", advice["shortfallYuan"] == 4.0, f"实际 {advice['shortfallYuan']}")
    check("无解说明带适用范围免责声明", bool(advice["disclaimer"]))


def test_quote_provenance():
    """核价记录的来源可追溯性（P1.6 新增）。

    - 每条记录都要有核价时间与价格字段；
    - P1.6 新增批次必须**逐条**带 storeCode / orderType / beType / batch；
    - 任何带 storeCode 的记录门店必须一致 → 禁止跨门店混用；
    - 明确禁止出现 3230442 的报价被当成 3230019 使用。
    """
    for key, rec in REAL_QUOTES.items():
        label = "+".join(sorted(key))
        check(f"核价记录 {label} 有核价时间 at",
              isinstance(rec.get("at"), str) and rec["at"].startswith("2026-10-16T"),
              str(rec.get("at")))
        check(f"核价记录 {label} 含 data.price",
              isinstance((rec.get("data") or {}).get("price"), (int, float)))

    per_record = {k: v for k, v in REAL_QUOTES.items() if v.get("batch")}
    declared_per_record = {n for n, c in QUOTES_CONTEXT.items() if c["provenance"] == "per_record"}
    check("逐条带来源的记录数 ≥ 20（P1.6 的 2 + P1.7 的 12 + P1.8 的 6）",
          len(per_record) >= 20, f"实际 {len(per_record)}")
    check("每条带 batch 的记录都引用了一个已声明的 per_record 批次",
          {v["batch"] for v in per_record.values()} <= declared_per_record,
          str(sorted({v["batch"] for v in per_record.values()})))
    check("每个 per_record 批次都至少有一条记录",
          {v["batch"] for v in per_record.values()} == declared_per_record,
          str(sorted(declared_per_record)))
    for key, rec in per_record.items():
        label = "+".join(sorted(key))
        check(f"新记录 {label} 带 storeCode=3230019",
              rec.get("storeCode") == "3230019", str(rec.get("storeCode")))
        check(f"新记录 {label} 带 orderType=1 / beType=1",
              rec.get("orderType") == 1 and rec.get("beType") == 1)
        check(f"新记录 {label} 记录了时段",
              isinstance(rec.get("daypart"), str) and rec["daypart"])

    stores = {rec["storeCode"] for rec in REAL_QUOTES.values() if rec.get("storeCode")}
    check("凡带门店字段的核价记录门店一致（禁止跨店混用）", stores == {"3230019"}, str(stores))
    check("核价记录中不出现其他门店（尤其 3230442）",
          all(rec.get("storeCode") != "3230442" for rec in REAL_QUOTES.values()))
    # P1.12：改为**数据推导** —— QUOTES_CONTEXT 必须恰好覆盖「历史批次 + REAL_QUOTES 里出现的所有 batch」。
    # 这样新增批次时不必改断言，但"声明了却没记录 / 有记录却没声明"仍会被抓到。
    per_record_batches = {v["batch"] for v in REAL_QUOTES.values() if v.get("batch")}
    check("QUOTES_CONTEXT 的 key == 历史批次 ∪ REAL_QUOTES 里出现的批次（数据推导）",
          set(QUOTES_CONTEXT) == per_record_batches | {"historical_2026-10-16_0220"},
          str(sorted(QUOTES_CONTEXT)))
    check("历史批次被明确标注为批次级来源（未逐条采集门店，故不补齐）",
          QUOTES_CONTEXT["historical_2026-10-16_0220"]["provenance"] == "batch_level")
    for batch in sorted(per_record_batches):
        check(f"{batch} 被标注为逐条来源",
              QUOTES_CONTEXT[batch]["provenance"] == "per_record")


def test_retired_skus_are_excluded_everywhere():
    """已确认失效/轮换的旧编码：既不得进入商品池，也不得出现在新核价记录里（P1.7 新增）。"""
    pmap = plan.load_json(PRODUCT_MAP)
    retired_in_map = {c for c, r in pmap["verifiedProducts"].items() if r.get("retired")}

    check("RETIRED_SKUS 与映射表 retired 集合完全一致（防止两个文件各自漂移）",
          RETIRED_SKUS == retired_in_map,
          f"测试={sorted(RETIRED_SKUS)} 映射表={sorted(retired_in_map)}")
    check("映射表声明了编码轮换表", "codeRotation" in pmap)
    check("轮换表覆盖全部 retired 编码",
          {p["retiredCode"] for p in pmap["codeRotation"]["pairs"]} == retired_in_map,
          str(sorted(p["retiredCode"] for p in pmap["codeRotation"]["pairs"])))

    menu, _, pool = load_pool()
    codes_in_pool = {i["code"] for i in pool}
    check("retired 编码不在商品池中（即便它们曾被写进菜单）",
          not (RETIRED_SKUS & codes_in_pool), str(sorted(RETIRED_SKUS & codes_in_pool)))
    check("当前菜单快照里确实已无这些编码（refresh 已完成）",
          not (RETIRED_SKUS & set(menu)), str(sorted(RETIRED_SKUS & set(menu))))

    # 机制验证：即使把 retired 编码塞进菜单，build_pool 也必须拒绝它
    synthetic = dict(menu)
    synthetic["9900015162"] = {"name": "牛肉堡单品精选", "priceYuan": 14.0}
    injected_pool = plan.build_pool(synthetic, pmap)
    injected_codes = {i["code"] for i in injected_pool}
    check("★ 机制：retired 编码即使出现在菜单里也不进入商品池",
          "9900015162" not in injected_codes)
    check("机制：注入 retired 编码不影响其他商品的入池",
          len(injected_pool) == len(pool))

    # 新核价记录不得引用 retired 编码
    for key, rec in REAL_QUOTES.items():
        if not rec.get("batch"):
            continue
        check(f"新记录 {'+'.join(sorted(key))} 不引用已失效编码",
              not (set(key) & RETIRED_SKUS))

    # 历史记录里引用已失效编码是**允许且刻意保留**的，但必须每一条都有明确状态解释。
    # 不变量：历史记录的每个编码都必须 ∈ {当前在售} ∪ {已标 retired} ∪ {已记录的不确定集合}。
    menu_codes = set(plan.extract_menu(plan.load_json(FIXTURE_MENU)))
    documented = RETIRED_SKUS | UNVERIFIABLE_SKUS
    for key, rec in REAL_QUOTES.items():
        if rec.get("batch"):
            continue
        unexplained = set(key) - menu_codes - documented
        check(f"历史记录 {'+'.join(sorted(key))} 的编码都有明确状态（在售/已失效/已记录的不确定）",
              not unexplained, f"未解释：{sorted(unexplained)}")
    historical_with_retired = [k for k, v in REAL_QUOTES.items()
                               if not v.get("batch") and (set(k) & RETIRED_SKUS)]
    check("引用 retired 编码的历史记录被如实保留（不删除、不隐瞒）",
          len(historical_with_retired) >= 7, f"实际 {len(historical_with_retired)}")
    check("历史批次 caveat 明确说明这些记录不再与当前快照对应",
          ("失效" in QUOTES_CONTEXT["historical_2026-10-16_0220"].get("caveat", "")
           or "轮换" in QUOTES_CONTEXT["historical_2026-10-16_0220"].get("caveat", "")))


def test_menu_fixture_provenance():
    """菜单快照的来源/门店/时间信息必须完整，且与映射表、与核价记录自洽（P1.7 新增）。"""
    raw = json.loads(FIXTURE_MENU.read_text(encoding="utf-8"))
    notice = raw.get("_fixtureNotice")
    check("快照带 _fixtureNotice 来源块", isinstance(notice, dict))
    for field in ("source", "capturedAt", "storeCode", "storeName", "daypart", "channel"):
        check(f"快照来源块含 {field}", bool(notice.get(field)), str(notice.get(field)))
    check("快照来源写明是 query-meals 的真实返回",
          "query-meals" in str(notice.get("source", "")))
    check("快照来源块声明了采集条目数", notice.get("itemCount") == len(raw["data"]["meals"]),
          f"{notice.get('itemCount')} vs {len(raw['data']['meals'])}")

    _, pmap, _ = load_pool()
    check("快照门店 == 映射表核实门店",
          notice["storeCode"] == pmap["verificationContext"]["storeCode"],
          f"{notice['storeCode']} vs {pmap['verificationContext']['storeCode']}")
    check("快照渠道 == 映射表渠道（orderType=1 / beType=1）",
          notice["channel"] == {"orderType": 1, "beType": 1}, str(notice["channel"]))

    check("快照保留了此前被丢弃的条件价字段 discountType",
          any("discountType" in m for m in raw["data"]["meals"].values()))
    check("快照保留了 canWithOrder 字段",
          any("canWithOrder" in m for m in raw["data"]["meals"].values()))

    # 条件价（随单购）不变量：凡声明随单购优惠的商品，必须同时给出 withOrder 且 canWithOrder=true。
    # 不硬编码数量 —— P1.6 报告里曾把这类商品说成 4 个，实际是 13 个，硬编码只会把错误固化。
    meals = raw["data"]["meals"]
    conditional = [c for c, m in meals.items()
                   if str(m.get("discountType", "")).startswith("随单购")]
    check("快照中存在随单购条件价商品", len(conditional) > 0, f"{len(conditional)} 个")
    check("凡随单购条件价商品都带 withOrder 且 canWithOrder=true",
          all(meals[c].get("withOrder") and meals[c].get("canWithOrder") is True
              for c in conditional),
          str([c for c in conditional
               if not (meals[c].get("withOrder") and meals[c].get("canWithOrder") is True)]))
    check("非随单购商品不带 withOrder",
          all(not meals[c].get("withOrder") for c in meals if c not in conditional))

    # 真实证据联动：条件价商品「菜单现价 ≠ 官方裸核价」必须有真实核价支撑
    conditional_evidence = [
        (cid, rec) for cid, rec in REAL_QUOTES.items()
        if cid & set(conditional) and rec.get("data")
    ]
    check("条件价商品已有真实 calculate-price 证据（证明菜单价确实是条件价）",
          len(conditional_evidence) >= 1, f"{len(conditional_evidence)} 条")
    for cid, rec in conditional_evidence:
        codes = sorted(cid & set(conditional))
        price = rec["data"]["price"] / 100
        menu_total = sum(float(meals[c]["currentPrice"]) for c in cid)
        check(f"条件价商品 {codes}：官方裸核价明显高于菜单现价合计（说明菜单价是有条件的）",
              price > menu_total + 1.0, f"官方 {price} vs 菜单 {menu_total}")

    check("快照记录了被取代的历史快照（不掩盖过期事实）",
          isinstance(notice.get("history"), list) and len(notice["history"]) >= 1)
    superseded = set(notice["history"][0].get("supersededCodes") or [])
    check("历史快照的失效编码集合 == 已确认轮换 ∪ 已记录的不确定",
          superseded == RETIRED_SKUS | UNVERIFIABLE_SKUS,
          f"快照={sorted(superseded)} 期望={sorted(RETIRED_SKUS | UNVERIFIABLE_SKUS)}")
    check("这些失效编码已全部不在刷新后的快照菜单里",
          not (superseded & set(meals)), str(sorted(superseded & set(meals))))
    status = notice["history"][0]["supersededCodeStatus"]
    check("快照把失效编码按证据等级分了三类（已确认轮换 / 数据推导 / 当前不可验证）",
          set(status["rotationConfirmed"]) == RETIRED_SKUS
          and set(status["unverifiableCurrentDaypart"]) == {"521143", "521337"}
          and set(status["notInMapDataDriven"]) == {"9900015165"},
          str(status))


def test_p1_8_new_burgers():
    """P1.8 新增的 11 个商品：结构、证据完整性、以及 proteinType 只是标注（P1.8 新增）。"""
    pmap = plan.load_json(PRODUCT_MAP)
    v = pmap["verifiedProducts"]
    menu, _, pool = load_pool()
    by_code = {i["code"]: i for i in pool}
    anchors = {i["code"] for i in pool if i["canSatisfyBurgerConstraint"]}

    check("11 个新条目全部已在映射表中", all(c in v for c in P1_8_NEW_CODES),
          str([c for c in P1_8_NEW_CODES if c not in v]))
    for code in P1_8_NEW_CODES:
        rec = v[code]
        check(f"{code} 为 confirmed_detail", rec.get("confidence") == "confirmed_detail")
        check(f"{code} roundsVerified 为 True", rec.get("roundsVerified") is True)
        check(f"{code} 已确认默认含汉堡", rec.get("containsBurgerByDefault") is True)
        check(f"{code} 不需要用户多选（requiresUserSelection 未置位）",
              not rec.get("requiresUserSelection"))
        check(f"{code} 默认组成条数 == roundsLen",
              len(rec.get("defaultComposition") or []) == rec.get("roundsLen"),
              f"{len(rec.get('defaultComposition') or [])} vs {rec.get('roundsLen')}")
        check(f"{code} 可进入硬约束候选池", code in anchors)
        check(f"{code} 在菜单快照中确实在售", code in menu)
        ev = rec.get("evidence") or {}
        check(f"{code} 证据完整（tool/storeCode/daypart/at/raw）",
              ev.get("tool") == "query-meal-detail" and ev.get("storeCode") == "3230019"
              and ev.get("daypart") == "午餐"
              and str(ev.get("at", "")).startswith("2026-10-16T11:1")
              and len(str(ev.get("raw", ""))) > 30,
              str(ev.get("at")))
        check(f"{code} 记录了 proteinType 标注",
              rec.get("proteinType") in {"beef", "fish"}, str(rec.get("proteinType")))

    check("4 个鱼堡被标注为 fish（但仍然是汉堡）",
          {c for c in P1_8_NEW_CODES if v[c].get("proteinType") == "fish"} == FISH_BURGER_CODES,
          str(sorted(c for c in P1_8_NEW_CODES if v[c].get("proteinType") == "fish")))
    for code in FISH_BURGER_CODES:
        check(f"{code}（鱼堡）默认仍含汉堡，未被排除在锚点外", code in anchors)

    # ★ proteinType 只是**标注**：内核从不读取它，也不得据此筛选。
    src = (ROOT / "scripts" / "plan.py").read_text(encoding="utf-8")
    check("内核不读取 proteinType（保证它不参与任何筛选）",
          "proteinType" not in src)
    check("映射表声明了 proteinType 只是标注",
          "proteinTypeNote" in pmap
          and "不参与任何筛选" in pmap["proteinTypeNote"])

    # 疑似汉堡清单必须已清空（本轮目标达成）
    stats = plan.coverage_stats(menu, pmap)
    check("「名称疑似含汉堡但未核实」清单已清空",
          stats["uncoveredBurgerLike"] == [],
          str([(x["code"], x["name"]) for x in stats["uncoveredBurgerLike"]]))
    # P1.12：数量断言改为**下限哨兵** —— 条目只增不减，写死会在每次核实后过期。
    check("映射条目数 ≥ 56（38 + P1.8 的 11 + P1.12 的 7）", len(v) >= 56, f"实际 {len(v)}")
    check("P1.12 的 7 个安格斯商品已全部入表且可作锚点",
          all(v.get(c, {}).get("containsBurgerByDefault") is True
              and not v.get(c, {}).get("retired")
              for c in ("511781", "511782", "9900005428", "9900005430",
                        "9900000881", "9900000882", "9900006957")))


def test_first_real_discount_evidence():
    """真实 discount > 0 的官方核价必须被如实标注为「店内促销」而非「券」（P1.7 建立，P1.9 修正）。

    P1.13 修正：折让来源必须按**记录所属时段的菜单**判定，不能拿午餐快照去套早餐记录 ——
    早餐菜单里的促销商品（`507328`/`507368`）在午餐菜单里根本不存在，反之亦然。
    """
    menu = plan.extract_menu(plan.load_json(FIXTURE_MENU))
    # 午餐时段的促销商品（来自午餐快照）
    promo_codes = {c for c, i in menu.items() if i.get("discountType") == "促销优惠"}
    # 早餐时段观察到的促销商品（REAL_LIVE，取自预约 2026-10-17 08:00 的早餐菜单）
    breakfast_promo_codes = set(CROSS_DAYPART_EVIDENCE["breakfastPromoCodes"])

    discounted = {k: v for k, v in REAL_QUOTES.items()
                  if (v.get("data") or {}).get("discount")}
    check("存在真实 discount > 0 的核价记录", len(discounted) >= 1, f"{len(discounted)} 条")
    check("店内存在「促销优惠」商品（用于解释折让来源）", len(promo_codes) > 0,
          str(sorted(promo_codes)))
    check("早餐菜单也存在「促销优惠」商品（跨时段对照）",
          len(breakfast_promo_codes) > 0, str(sorted(breakfast_promo_codes)))

    for key, rec in discounted.items():
        data = rec["data"]
        label = "+".join(sorted(key))
        check(f"{label}：discount = originalPrice − price",
              data["discount"] == data["originalPrice"] - data["price"],
              f"{data['discount']} vs {data['originalPrice']}-{data['price']}")
        if rec.get("daypart") == "早餐":
            # 早餐批次 → 与早餐菜单里观察到的促销码比对
            check(f"{label}：折让来自早餐菜单「促销优惠」商品（非券）",
                  bool(set(key) & breakfast_promo_codes),
                  f"折让商品={sorted(key)} 早餐促销={sorted(breakfast_promo_codes)}")
        else:
            # 其余批次 → 与午餐快照比对
            check(f"{label}：折让来自店内「促销优惠」商品（非券）",
                  bool(set(key) & promo_codes),
                  f"折让商品={sorted(key)} 促销={sorted(promo_codes)}")
    check("全部 discount>0 的记录都不含任何券字段（证明这是店内促销，不是券）",
          all(not (set(v.get("data", {}).keys()) & {"couponId", "couponCode"})
              for v in discounted.values()))
    check("Caveat 明确该批并未取得带券核价",
          "带券核价" in QUOTES_CONTEXT["batch_2026-10-16_1100"].get("caveat", ""))


def test_code_rotation_evidence():
    """编码轮换取证的完整性，与映射表 codeRotation 的一致性，以及与 REAL_QUOTES 的隔离。

    P1.6 建立（2 组实测），P1.7 扩展为**与映射表交叉校验**，防止测试内嵌数据与映射表各自漂移。
    """
    pmap = plan.load_json(PRODUCT_MAP)
    pairs = {p["retiredCode"]: p for p in pmap["codeRotation"]["pairs"]}

    check("轮换表记录了 7 对", len(pairs) == 7, str(sorted(pairs)))
    check("轮换表每条都给出证据强度",
          all(p.get("evidenceStrength") for p in pairs.values()))
    check("轮换表每条都区分了 A/B 等级",
          all(p.get("equivalence") in {"A", "B"} for p in pairs.values()),
          str(sorted(p.get("equivalence") for p in pairs.values())))
    check("A 级一律声明「默认组成等价」",
          all(p["defaultCompositionIdentical"] for p in pairs.values()
              if p["equivalence"] == "A"))
    check("B 级一律声明「默认组成不等价」",
          all(not p["defaultCompositionIdentical"] for p in pairs.values()
              if p["equivalence"] == "B"))
    check("轮换表的每条 retired 编码都在映射表中标了 retired",
          all(pmap["verifiedProducts"][c].get("retired") for c in pairs))
    check("轮换表声明的 supersededBy 与映射表 retired.supersededBy 一致",
          all(pmap["verifiedProducts"][c]["retired"].get("supersededBy") == p["currentCode"]
              for c, p in pairs.items()))
    check("有 1 个编码被列为「当前不可验证（C 级）」（521143，未标 retired）",
          [u["code"] for u in pmap["codeRotation"]["unresolved"]] == ["521143"],
          str([u["code"] for u in pmap["codeRotation"]["unresolved"]]))
    check("521143 未被标 retired（缺原时段证据，不猜）",
          "retired" not in pmap["verifiedProducts"].get("521143", {"retired": None})
          if "521143" in pmap["verifiedProducts"] else True)

    # P1.6 的两条实测记录必须与轮换表一致
    check("测试内嵌的轮换证据为 2 组（P1.6 实测）", len(CODE_ROTATION_EVIDENCE) == 2,
          str(sorted(CODE_ROTATION_EVIDENCE)))
    for label, rec in CODE_ROTATION_EVIDENCE.items():
        check(f"{label} 带门店/渠道且为 3230019 到店",
              rec["storeCode"] == "3230019" and rec["orderType"] == 1 and rec["beType"] == 1)
        check(f"{label} 记录了旧编码的真实失败返回（60001）",
              "60001" in rec["retiredProbeResult"] and rec["retiredCode"] != rec["currentCode"])
        check(f"{label} 记录了新编码的真实核价",
              isinstance(rec.get("price"), int) and rec["price"] > 0, str(rec.get("price")))
        check(f"{label} 同名同价 → 判定为编码轮换而非商品下线",
              rec["currentName"] == rec["retiredName"]
              and rec["price"] == rec["fixturePriceOfRetiredCode"])
        check(f"{label} 有核价时间", rec["at"].startswith("2026-10-16T"))
        check(f"{label} 与映射表轮换表一致（新编码 + 等级）",
              pairs.get(rec["retiredCode"], {}).get("currentCode") == rec["currentCode"],
              f"映射表={pairs.get(rec['retiredCode'], {}).get('currentCode')}")

    # 隔离规则（P1.7 收紧）：**旧编码**的单条核价记录不得存在 —— 否则就是把新编码的
    # 核价冒充成旧编码的，等于伪造证据。新编码自己可以有记录（它是真实在售商品）。
    check("轮换证据中的旧编码未被混入 REAL_QUOTES",
          not any(frozenset([rec["retiredCode"]]) in REAL_QUOTES
                  for rec in CODE_ROTATION_EVIDENCE.values()))
    for rec in CODE_ROTATION_EVIDENCE.values():
        key = frozenset([rec["currentCode"]])
        if key in REAL_QUOTES:
            check(f"新编码 {rec['currentCode']} 若有核价记录，必须带批次来源（可追溯）",
                  bool(REAL_QUOTES[key].get("batch")),
                  str(REAL_QUOTES[key].get("batch")))


# ---------------------------------------------------------------------- P1.9
def test_price_semantics_resolver():
    """字段语义解析器：只有**已验证**的 discountType 才敢动预筛价，其余一律保守（P1.9）。"""
    R = plan.resolve_price_semantics

    # ① 无 discountType → 菜单现价即预筛价（行为与修复前一致）
    s = R({"currentPrice": "34", "originalPrice": "47.5"})
    check("无 discountType → 用菜单现价预筛（普通商品行为不变）",
          s["prefilterPriceYuan"] == 34.0 and s["priceStatus"] == plan.PRICE_STATUS_MENU,
          str(s["prefilterPriceYuan"]))

    # ② 促销优惠 → 已实测，菜单现价就是实付价
    s = R({"currentPrice": "13.9", "originalPrice": "21", "discountType": "促销优惠"})
    check("促销优惠 → 仍用菜单现价预筛（已实测 507409 裸核价 ¥13.9）",
          s["prefilterPriceYuan"] == 13.9 and s["priceStatus"] == plan.PRICE_STATUS_PROMO_MENU)

    # ③ 随单购 → 已实测 26/26（午餐 16 + 早餐 10），预筛改用原价
    s = R({"currentPrice": "28", "originalPrice": "58",
           "discountType": "随单购麦金卡优惠", "withOrder": {"membershipCode": "106"}})
    check("随单购 → 预筛改用原价（已实测 26/26 裸核价 == 原价）",
          s["prefilterPriceYuan"] == 58.0
          and s["priceStatus"] == plan.PRICE_STATUS_CONDITIONAL)
    # P1.13 新增：早餐时段的「随单购早餐卡优惠」实测 10/10 语义相同
    s2 = R({"currentPrice": "8.1", "originalPrice": "13.5",
            "discountType": "随单购早餐卡优惠", "withOrder": {"cardId": "CRD...", "cardType": 2}})
    check("★ 跨时段：早餐 `随单购早餐卡优惠` → 同样改用原价（早餐 10/10 实测）",
          s2["prefilterPriceYuan"] == 13.5
          and s2["priceStatus"] == plan.PRICE_STATUS_CONDITIONAL)
    check("随单购 → 菜单现价保留在 menuPriceYuan（供「菜单价≠官方价」差异对比）",
          s["menuPriceYuan"] == 28.0 and s["originalPriceYuan"] == 58.0)
    check("随单购 → 记录 withOrder / canWithOrder 供追溯",
          s["hasWithOrder"] is True and "canWithOrder" in s)

    # ④ 未验证的取值 → 保守（★ 不得因为「名字看着像」就套用原价）
    #    P1.13 之前，`随单购早餐卡优惠` 也在这个「未验证」列表里 —— 它名字与已验证的
    #    `随单购麦金卡优惠` 几乎一样，但当时**没有证据**。P1.13 用 `reservationDate`
    #    取得早餐菜单后实测 10/10，才把它移入白名单。
    #    ⚠️ 仍在列表里的 `麦金卡优惠` / `早餐卡优惠` **不带「随单购」前缀**，
    #    至今零样本 —— 绝不可因为「都带『卡优惠』三个字」就套用同一结论。
    for dt in ("麦金卡优惠", "早餐卡优惠", "买一送一"):
        s = R({"currentPrice": "28", "originalPrice": "58", "discountType": dt})
        check(f"★ 未验证的 discountType「{dt}」→ 保守：不本地判断",
              s["prefilterPriceYuan"] is None
              and s["priceStatus"] == plan.PRICE_STATUS_UNKNOWN, str(s["prefilterPriceYuan"]))
        check(f"★「{dt}」不得擅自套用原价当预筛价", s["prefilterPriceYuan"] != 58.0)
    # P1.13：白名单扩为 2 个取值 —— 两个都各自有实测背书（午餐 16 条 / 早餐 10 条）。
    check("已验证的条件价取值是**精确匹配**的白名单（两个取值各有实测，不做前缀泛化）",
          plan.VERIFIED_CONDITIONAL_DISCOUNT_TYPES == {"随单购麦金卡优惠", "随单购早餐卡优惠"},
          str(plan.VERIFIED_CONDITIONAL_DISCOUNT_TYPES))
    # ★ 关键：白名单**不得**包含不带「随单购」前缀的取值
    check("★ 白名单不含任何不带「随单购」前缀的取值（`麦金卡优惠`/`早餐卡优惠` 零证据）",
          all(str(v).startswith(CONDITIONAL_DISCOUNT_PREFIX)
              for v in plan.VERIFIED_CONDITIONAL_DISCOUNT_TYPES),
          str(plan.VERIFIED_CONDITIONAL_DISCOUNT_TYPES))

    # ⑤ 缺必要字段 → 保守
    s = R({"currentPrice": "28", "discountType": "随单购麦金卡优惠"})
    check("条件价但缺 originalPrice → 保守：不做本地预算判断",
          s["prefilterPriceYuan"] is None
          and s["priceStatus"] == plan.PRICE_STATUS_UNKNOWN)
    s = R({"currentPrice": "abc", "originalPrice": "58"})
    check("currentPrice 不可解析 → 保守", s["prefilterPriceYuan"] is None
          and s["priceStatus"] == plan.PRICE_STATUS_UNKNOWN)
    s = R({"name": "没有价格字段"})
    check("完全没有价格字段 → 保守", s["prefilterPriceYuan"] is None)


def test_conditional_price_evidence_matches_original():
    """随单购条件价商品裸核价 == originalPrice（真实证据，支撑预筛规则）（P1.9 建立，P1.12 扩充）。"""
    menu, report = plan.extract_menu_with_report(plan.load_json(FIXTURE_MENU))
    conditional = [(code, info) for code, info in menu.items()
                   if info["priceStatus"] == plan.PRICE_STATUS_CONDITIONAL]
    # P1.12：数量改为**由 fixture 推导**，并与菜单里声明了随单购 discountType 的商品数对齐。
    raw_meals = plan.load_json(FIXTURE_MENU)["data"]["meals"]
    declared = [c for c, m in raw_meals.items()
                if str(m.get("discountType", "")).startswith(CONDITIONAL_DISCOUNT_PREFIX)]
    check("菜单级条件价商品 == 声明了「随单购」discountType 的商品（数据推导）",
          len(conditional) == len(declared), f"{len(conditional)} vs {len(declared)}")
    check("菜单级条件价商品数 ≥ 13（P1.9 首次验证时的数量；只增不减，减少说明字段语义变了）",
          len(conditional) >= 13, f"实际 {len(conditional)}")
    check("条件价商品全部带 withOrder 且 canWithOrder=True",
          all(info["hasWithOrder"] and info["canWithOrder"] for _, info in conditional))
    check("条件价商品的 discountType 全部以「随单购」开头",
          all(str(info["discountType"]).startswith(CONDITIONAL_DISCOUNT_PREFIX)
              for _, info in conditional))
    check("条件价商品的菜单现价 < 原价（否则字段关系异常）",
          all(info["priceYuan"] < info["originalPriceYuan"] for _, info in conditional))

    verified = 0
    for code, info in conditional:
        q = REAL_QUOTES.get(frozenset([code]))
        if not q:
            continue
        verified += 1
        cents = q["data"]["price"]
        check(f"{code} 裸核价 == originalPrice（{int(round(info['originalPriceYuan'] * 100))} 分）",
              cents == int(round(info["originalPriceYuan"] * 100)), f"官方 {cents}")
        check(f"{code} 裸核价 ≠ 菜单现价（证明菜单价确实是条件价）",
              cents != int(round(info["priceYuan"] * 100)),
              f"官方 {cents} vs 菜单 {int(round(info['priceYuan'] * 100))}")
    # P1.12：P1.9 的 13 个里，9900000890 / 9900000891 的原价证据来自**组合核价**（REAL_QUOTES 里没有单商品键），
    # 因此有独立裸核价键的是 11 条；加 P1.12 的 3 条 = 14 条。
    check("至少 14 个条件价商品有**独立**裸核价证据（P1.9 的 11 + P1.12 的 3）",
          verified >= 14, f"{verified}")

    # 对照组：非条件价商品的裸核价 == 菜单现价
    for code, label in (("9900005456", "无 discountType 的套餐"), ("507409", "促销优惠")):
        c = menu[code]
        q = REAL_QUOTES[frozenset([code])]
        check(f"对照：{label} {code} 裸核价 == 菜单现价（不是条件价）",
              q["data"]["price"] == int(round(c["priceYuan"] * 100)),
              f"官方 {q['data']['price']} vs 菜单 {int(round(c['priceYuan'] * 100))}")
        check(f"对照：{label} {code} 预筛价 == 菜单现价",
              c["prefilterPriceYuan"] == c["priceYuan"])


def test_cross_daypart_price_semantics():
    """跨时段验证：条件价语义在早餐时段同样成立，未验证取值仍然保守（P1.13）。

    取得方式：门店 3230019 全天营业，`query-meals` / `calculate-price` 支持
    `reservationDate`，于是用预约「2026-10-17 08:00」取得真实早餐菜单与核价。
    ⚠️ 这与"把同一份午餐菜单再测一遍"有本质区别 —— 已用 60006 反证参数生效：
    早餐商品 `9900003431` 不带预约参数时核价失败，带参数则成功。
    """
    E = CROSS_DAYPART_EVIDENCE

    # ① 早餐时段：所有「随单购早餐卡优惠」样本的裸核价 == originalPrice
    samples = E["breakfastConditionalSamples"]
    check("早餐时段条件价样本数 ≥ 10", len(samples) >= 10, f"{len(samples)}")
    for code, name, cur, orig, cents in samples:
        rec = REAL_QUOTES.get(frozenset([code]))
        check(f"早餐 {code} {name} 有本轮真实核价记录", rec is not None)
        if rec is None:
            continue
        check(f"早餐 {code} {name} 裸核价 == originalPrice（{int(round(orig * 100))} 分）",
              rec["data"]["price"] == int(round(orig * 100)),
              f"官方 {rec['data']['price']}")
        check(f"早餐 {code} {name} 裸核价 ≠ 菜单现价（{int(round(cur * 100))} 分）",
              rec["data"]["price"] != int(round(cur * 100)),
              f"官方 {rec['data']['price']}")
        check(f"早餐 {code} {name} 核价记录标记为早餐时段",
              rec.get("daypart") == "早餐", str(rec.get("daypart")))

    # ② 解析器对早餐卡取值的结果 == 麦金卡（语义相同，不是前缀泛化的产物）
    R = plan.resolve_price_semantics
    base = {"currentPrice": "8.1", "originalPrice": "13.5"}
    a = R({**base, "discountType": "随单购麦金卡优惠", "withOrder": {"membershipCode": "106"}})
    b = R({**base, "discountType": "随单购早餐卡优惠", "withOrder": {"cardId": "X", "cardType": 2}})
    check("★ `随单购早餐卡优惠` 与 `随单购麦金卡优惠` 判定结果一致（都用原价预筛）",
          a["prefilterPriceYuan"] == b["prefilterPriceYuan"] == 13.5
          and b["priceStatus"] == plan.PRICE_STATUS_CONDITIONAL)

    # ③ 两个白名单取值都必须带「随单购」前缀；不带前缀的零证据取值必须保持保守
    check("★ 白名单内每个取值都以「随单购」开头",
          all(str(v).startswith(CONDITIONAL_DISCOUNT_PREFIX)
              for v in plan.VERIFIED_CONDITIONAL_DISCOUNT_TYPES),
          str(plan.VERIFIED_CONDITIONAL_DISCOUNT_TYPES))
    for dt in E["unverifiedDiscountTypes"]:
        s = R({**base, "discountType": dt})
        check(f"★ 零证据取值「{dt}」→ 仍保守（不套用原价、也不用菜单价顶替）",
              s["prefilterPriceYuan"] is None
              and s["priceStatus"] == plan.PRICE_STATUS_UNKNOWN
              and s["prefilterPriceYuan"] != 13.5)

    # ④ 跨时段对照：同一商品在不同时段 discountType 不同，但语义一致
    pairs = E["sameItemDifferentDiscountType"]
    check("存在「同商品跨时段 discountType 不同」的对照样本", len(pairs) >= 1)
    for p in pairs:
        rec = REAL_QUOTES.get(frozenset([p["code"]]))
        check(f"跨时段对照 {p['code']}：两个时段的 discountType 确实不同",
              p["discountTypeLunch"] != p["discountTypeBreakfast"])
        check(f"跨时段对照 {p['code']}：两时段裸核价都 == originalPrice ¥{p['originalPriceYuan']}",
              p["quoteLunch"] == p["quoteBreakfast"] == int(round(p["originalPriceYuan"] * 100)))
        if rec is not None:
            check(f"跨时段对照 {p['code']}：早餐裸核价有独立记录可追溯",
                  rec["data"]["price"] == p["quoteBreakfast"])

    # ⑤ 预约批次必须在 QUOTES_CONTEXT 中声明，否则来源不可追溯
    check("早餐预约批次已在 QUOTES_CONTEXT 声明",
          "batch_2026-10-17_0800_reservation" in QUOTES_CONTEXT)
    check("早餐预约批次携带 reservationDate（供复现）",
          QUOTES_CONTEXT["batch_2026-10-17_0800_reservation"].get("reservationDate")
          == "2026-10-17 08:00")


def test_conditional_price_never_counts_as_budget_feasible():
    """条件价商品不得按菜单价判为预算可行；普通商品行为不变（P1.9）。"""
    menu, report = plan.extract_menu_with_report(plan.load_json(FIXTURE_MENU))
    pmap = plan.load_json(PRODUCT_MAP)
    pool = plan.build_pool(menu, pmap)
    by = {i["code"]: i for i in pool}

    check("池中 9900000890 的预筛价 = 原价 ¥58（不是菜单 ¥28）",
          by["9900000890"]["prefilterPriceYuan"] == 58.0)
    check("池中普通套餐 9900005456 的预筛价 = 菜单价 ¥34（行为不变）",
          by["9900005456"]["prefilterPriceYuan"] == 34.0)
    check("池中促销商品 521986 的预筛价 = 菜单价 ¥11（行为不变）",
          by["521986"]["prefilterPriceYuan"] == 11.0)
    conditional_in_menu = {c for c, info in menu.items()
                           if info["priceStatus"] == plan.PRICE_STATUS_CONDITIONAL}
    conditional_in_pool = {c for c in conditional_in_menu if c in by}
    check("★ 池内条件价商品一律 priceStatus=conditional 且预筛价用原价",
          all(by[c]["priceStatus"] == plan.PRICE_STATUS_CONDITIONAL
              and by[c]["prefilterPriceYuan"] == by[c]["originalPriceYuan"]
              for c in conditional_in_pool), str(sorted(conditional_in_pool)))
    # P1.12：区分「菜单级」与「池级」条件价商品 —— 二者可以不等，
    # 因为非餐品（如 521955 麦当劳联名徽章）虽带随单购 discountType，但会被 atomicNameRules 判为 exclude。
    check("菜单级条件价商品数 ≥ 池级（不变量）",
          len(conditional_in_menu) >= len(conditional_in_pool),
          f"{len(conditional_in_menu)} vs {len(conditional_in_pool)}")
    excluded_conditional = sorted(conditional_in_menu - conditional_in_pool)
    check("★ 不在池内的条件价商品必须是被 atomicNameRules 判为 exclude 的非餐品",
          all(plan.name_classify(menu[c]["name"], pmap["atomicNameRules"]) == "exclude"
              for c in excluded_conditional),
          str([(c, menu[c]["name"], plan.name_classify(menu[c]["name"], pmap["atomicNameRules"]))
               for c in excluded_conditional]))
    check("池级条件价商品数 ≥ 13（P1.9 时的池级数量）",
          len(conditional_in_pool) >= 13, f"实际 {len(conditional_in_pool)}")

    for budget in (25.0, 35.0, 40.0, 60.0):
        plans = plan.dedupe_plans(plan.search_plans(pool, budget, True, True))
        check(f"预算 {budget:.0f}：能搜到方案", len(plans) > 0, f"{len(plans)} 个")
        bad = [p for p in plans
               if (lambda t: t is not None and t > budget + plan.PRICE_TOLERANCE_YUAN)
               (plan.prefilter_sum(p))]
        check(f"预算 {budget:.0f}：没有任何方案的预筛价合计超预算",
              bad == [], str([sorted(i["code"] for i in p) for p in bad[:3]]))
        for i in (x for p in plans for x in p):
            if i["priceStatus"] == plan.PRICE_STATUS_CONDITIONAL:
                check(f"预算 {budget:.0f}：条件价商品 {i['code']} 入组合时用的是原价",
                      i["prefilterPriceYuan"] == i["originalPriceYuan"])

    # 真实案例：6102 + 9900000890（官方核价 ¥65）不得出现在 ¥35 / ¥40 的候选里
    for budget in (35.0, 40.0):
        plans = plan.dedupe_plans(plan.search_plans(pool, budget, True, True))
        codes = {tuple(sorted(i["code"] for i in p)) for p in plans}
        for bad in (("6102", "9900000890"), ("521951", "6102", "9900000890"),
                    ("521951", "6102", "9900000891")):
            check(f"预算 {budget:.0f}：条件价组合 {'+'.join(bad)} 已被预筛排除",
                  bad not in codes)
    # 修复前的可行组合数（P1.8 记录：¥35=375 / ¥40=1157）应下降
    for budget, before in ((35.0, 375), (40.0, 1157)):
        plans = plan.dedupe_plans(plan.search_plans(pool, budget, True, True))
        check(f"预算 {budget:.0f}：可行组合数由 {before} 降为 {len(plans)}（剔除了伪可行组合）",
              len(plans) < before, f"{len(plans)}")


def test_deferred_budget_check_is_conservative():
    """预筛价未知时：不本地剪枝，但必须显式标记 deferred，绝不伪装成确定价（P1.9）。"""
    base = plan.load_json(FIXTURE_MENU)

    # 把一个已确认汉堡商品换成「未验证的 discountType」且去掉原价
    raw = json.loads(json.dumps(base))
    raw["data"]["meals"]["1100"]["discountType"] = "麦金卡优惠"
    raw["data"]["meals"]["1100"].pop("originalPrice", None)
    m2, _ = plan.extract_menu_with_report(raw)
    check("注入未验证 discountType 后该商品状态为 unknown",
          m2["1100"]["priceStatus"] == plan.PRICE_STATUS_UNKNOWN)
    pool = plan.build_pool(m2, plan.load_json(PRODUCT_MAP))
    by = {i["code"]: i for i in pool}
    check("unknown 价格的商品被标记 budgetCheckDeferred",
          by["1100"]["budgetCheckDeferred"] is True
          and by["1100"]["prefilterPriceYuan"] is None)

    plans = plan.dedupe_plans(plan.search_plans(pool, 10.0, False, False))
    hit = [p for p in plans if {i["code"] for i in p} == {"1100"}]
    check("★ 未知价商品在极小预算下也**不被本地剪掉**（未知 ≠ 超预算）", len(hit) == 1)
    metrics = plan.plan_metrics(hit[0], 10.0, False, False)
    check("该组合带 budgetCheckDeferred 标记", metrics["budgetCheckDeferred"] is True)
    check("不把未知价伪装成确定价（prefilterTotalYuan 为 None）",
          metrics["prefilterTotalYuan"] is None)
    check("菜单价仍如实保留在 menuTotalYuan", metrics["menuTotalYuan"] == 26.5)

    # 缺 currentPrice → 记账，不静默丢弃
    raw2 = json.loads(json.dumps(base))
    del raw2["data"]["meals"]["1100"]["currentPrice"]
    _, rep2 = plan.extract_menu_with_report(raw2)
    check("缺 currentPrice 的条目被记入 dropped（不静默丢弃）",
          any(d["code"] == "1100" for d in rep2["dropped"]), str(rep2["dropped"]))
    check("dropped 条目带原因说明",
          all(d.get("reason") for d in rep2["dropped"]))
    check("被丢弃的条目不计入菜单", "1100" not in rep2.get("conditionalCodes", []))


def test_official_price_authoritative_over_prefilter():
    """官方核价始终是最终权威：预筛只是本地预估，不得替代核价（P1.9）。"""
    import tempfile

    # 人为把「预筛价 ¥65」但「菜单价 ¥35」的候选送进 verify，预算 40
    cand = {"candidates": [{
        "id": "P9", "styleHint": "多品类",
        "items": [
            {"code": "6102", "name": "苹果片一袋(60g)", "qty": 1,
             "menuUnitPriceYuan": 7.0, "prefilterUnitPriceYuan": 7.0},
            {"code": "9900000890", "name": "板烧鸡腿堡四件套", "qty": 1,
             "menuUnitPriceYuan": 28.0, "prefilterUnitPriceYuan": 58.0,
             "discountType": "随单购麦金卡优惠"},
        ],
        "menuTotalYuan": 35.0, "prefilterTotalYuan": 65.0,
        "satisfies": {"burger": True, "drink": True, "side": True},
    }]}
    quotes = {"quotes": {"P9": REAL_QUOTES[frozenset(["6102", "9900000890"])]}}
    with tempfile.TemporaryDirectory() as td:
        cp, qp, op = Path(td) / "c.json", Path(td) / "q.json", Path(td) / "o.json"
        cp.write_text(json.dumps(cand), encoding="utf-8")
        qp.write_text(json.dumps(quotes), encoding="utf-8")
        plan.cmd_verify(type("A", (), {"candidates": str(cp), "quotes": str(qp),
                                       "budget": 40.0, "out": str(op)})())
        out = json.loads(op.read_text(encoding="utf-8"))

    check("官方价优先：超预算的候选被判 over_budget（预筛说什么都不算数）",
          out["rejected"] and out["rejected"][0]["status"] == "over_budget",
          str(out["rejected"][0]["status"] if out["rejected"] else None))
    check("该候选不进入 verified", out["meta"]["verifiedCount"] == 0
          and out["verified"] == [])
    disc = {d["id"]: d for d in out["discrepancies"]}
    check("菜单价 ≠ 官方价 → discrepancies 如实记录",
          set(disc) == {"P9"}, str(sorted(disc)))
    check("discrepancies 的 delta = 官方 − 菜单 = ¥30.00",
          abs(disc["P9"]["deltaYuan"] - 30.0) <= 0.005, str(disc["P9"].get("deltaYuan")))
    check("discrepancies 记录里官方价与菜单价都被保留",
          disc["P9"]["officialPriceYuan"] == 65.0 and disc["P9"]["menuTotalYuan"] == 35.0)

    # 同一候选若没有核价记录 → 仍判 quote_missing（既有业务检查未被削弱）
    with tempfile.TemporaryDirectory() as td:
        cp, qp, op = Path(td) / "c.json", Path(td) / "q.json", Path(td) / "o.json"
        cp.write_text(json.dumps(cand), encoding="utf-8")
        qp.write_text(json.dumps({"quotes": {}}), encoding="utf-8")
        plan.cmd_verify(type("A", (), {"candidates": str(cp), "quotes": str(qp),
                                       "budget": 40.0, "out": str(op)})())
        out2 = json.loads(op.read_text(encoding="utf-8"))
    check("无核价记录 → 仍判 quote_missing（既有检查有效）",
          out2["rejected"][0]["status"] == "quote_missing")


# ---------------------------------------------------------------------------
# P1.10：候选分层机制 + 端到端场景
# ---------------------------------------------------------------------------

# ⚠️ 合成核价样本：**不是**官方实测，**禁止**写入 REAL_QUOTES。
#    仅用于验证「预筛价未知的候选，其最终价格只能由官方 calculate-price 决定」这一链路。
SYNTHETIC_UNVERIFIED_PRICE_QUOTE = {
    "success": True, "code": 200, "at": "SYNTHETIC-NOT-REAL",
    "data": {"price": 2200, "originalPrice": 3400, "discount": 0, "productList": [
        {"productCode": "9900005456", "productName": "麦辣鸡腿汉堡三件套",
         "quantity": 1, "originalSubtotal": 2200, "subtotal": 2200}]},
}


def _inject_unknown_price(raw: dict, codes) -> dict:
    """把指定商品的 discountType 改成**本店未验证**的取值 → 预筛价变为 None。"""
    out = json.loads(json.dumps(raw))
    for c in codes:
        if c in out["data"]["meals"]:
            out["data"]["meals"][c]["discountType"] = "早餐卡优惠"
    return out


def _run_verify(candidates: list, quotes: dict, budget: float) -> dict:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cp, qp, op = Path(td) / "c.json", Path(td) / "q.json", Path(td) / "o.json"
        cp.write_text(json.dumps({"candidates": candidates}), encoding="utf-8")
        qp.write_text(json.dumps({"quotes": quotes}), encoding="utf-8")
        plan.cmd_verify(type("A", (), {"candidates": str(cp), "quotes": str(qp),
                                       "budget": budget, "out": str(op)})())
        return json.loads(op.read_text(encoding="utf-8"))


def test_selection_prefers_credible_over_undetermined():
    """价格可信的候选必须优先占满席位；未定价候选只能补缺（P1.10 新增）。

    复现的旧缺陷：未定价候选的**菜单价不可信**，却与可信候选同池竞争，
    用"看起来便宜"的菜单价赢下 cheapest / best 席位，挤掉真正可行的候选。
    """
    raw = plan.load_json(FIXTURE_MENU)
    pmap = plan.load_json(PRODUCT_MAP)

    def sel(raw_menu, budget, maxc=6):
        menu, _ = plan.extract_menu_with_report(raw_menu)
        pool = plan.build_pool(menu, pmap)
        plans = plan.dedupe_plans(plan.search_plans(pool, budget, True, True))
        return pool, plans, plan.select_candidates(plans, budget, True, True, maxc)

    # 基线：真实 fixture 里没有价格未定商品
    _, _, base_sel = sel(raw, 35.0)
    check("基线候选中价格未定者为 0", all(not e["metrics"]["budgetCheckDeferred"]
                                          for e in base_sel))
    base_codes = [tuple(sorted(i["code"] for i in e["items"])) for e in base_sel]

    # 注入 5 个未验证 discountType 商品
    raw2 = _inject_unknown_price(raw, ["9900005456", "9900005462", "9900005464",
                                       "9900005449", "9900016073"])
    pool2, plans2, sel2 = sel(raw2, 35.0)
    check("注入生效：池中出现价格未定商品（场景有效）",
          any(i["budgetCheckDeferred"] for i in pool2),
          "未被正确注入则本场景无意义")
    check("注入后候选席位仍全部由**价格可信**候选占据",
          all(not e["metrics"]["budgetCheckDeferred"] for e in sel2),
          str([e["id"] for e in sel2 if e["metrics"]["budgetCheckDeferred"]]))
    check("★ 修复后候选与基线完全一致（未定价候选不再挤掉可信候选）",
          [tuple(sorted(i["code"] for i in e["items"])) for e in sel2] == base_codes,
          f"{[tuple(sorted(i['code'] for i in e['items'])) for e in sel2]}")

    # 不剪枝原则必须保留：未定价组合仍然存在于 plans 里
    undetermined_codes = {i["code"] for i in pool2 if i["budgetCheckDeferred"]}
    hit = [p for p in plans2 if {i["code"] for i in p} & undetermined_codes]
    check("★ 未定价商品仍**不被本地剪枝**（未知 ≠ 超预算，原则未变）", len(hit) > 0,
          f"{len(hit)} 个组合含未定价商品")

    # 极小预算 + 1 个未定价商品：可信候选为 0 → 未定价候选才补缺
    raw3 = _inject_unknown_price(raw, ["9900016057"])
    _, _, sel3 = sel(raw3, 10.0)
    check("可信候选为 0 时，未定价候选才进入席位（补缺而非抢占）",
          len(sel3) > 0 and all(e["metrics"]["budgetCheckDeferred"] for e in sel3),
          str([e["id"] for e in sel3]))
    check("补缺进来的未定价候选全部带 budgetCheckDeferred 标记",
          all(e["metrics"]["budgetCheckDeferred"] for e in sel3))


def test_candidate_composition_reported():
    """候选分层的实情必须如实输出，不得让未定价候选混进"可信推荐"（P1.10 新增）。"""
    import tempfile
    raw = plan.load_json(FIXTURE_MENU)

    def run(raw_menu, budget):
        with tempfile.TemporaryDirectory() as td:
            mp, op = Path(td) / "m.json", Path(td) / "o.json"
            mp.write_text(json.dumps(raw_menu), encoding="utf-8")
            plan.cmd_candidates(type("A", (), {
                "menu": str(mp), "map": str(PRODUCT_MAP), "budget": budget,
                "prefer_drink": True, "prefer_side": True, "max_items": 4,
                "max_candidates": 6, "store_code": "3230019", "out": str(op)})())
            return json.loads(op.read_text(encoding="utf-8"))

    out = run(raw, 35.0)
    comp = out["meta"]["candidateComposition"]
    check("candidateComposition 恒等式：credible + priceDeferred == 候选数",
          comp["credibleCount"] + comp["priceDeferredCount"] == len(out["candidates"]),
          str(comp))
    check("真实 fixture 下候选全部可信（priceDeferredCount == 0）",
          comp["priceDeferredCount"] == 0, str(comp))
    check("全部可信时不产生 priceUndeterminedNotice",
          out["meta"]["priceUndeterminedNotice"] is None)

    # 全部候选价格未定 → 必须显式说明，且给出最低已确认汉堡作为参考
    out2 = run(_inject_unknown_price(raw, ["9900016057"]), 10.0)
    comp2 = out2["meta"]["candidateComposition"]
    notice = out2["meta"]["priceUndeterminedNotice"]
    check("未定价候选全部占位时，credibleCount == 0", comp2["credibleCount"] == 0, str(comp2))
    check("此时**必须**给出 priceUndeterminedNotice（不得静默）", notice is not None)
    check("notice 说明「不等于无解」且候选仍会送官方核价",
          "不等于无解" in notice["note"] and "calculate-price" in notice["note"])
    check("notice 含最低已确认汉堡参考价",
          notice["cheapestConfirmedBurger"]["name"] == "牛肉堡单品精选")
    check("notice 逐条列出未定价候选的 id 与菜单价",
          len(notice["candidates"]) == comp2["priceDeferredCount"])


def test_end_to_end_scenarios():
    """8 个端到端场景（A–H）。真实证据优先，合成数据一律显式标注（P1.10 新增）。"""
    menu = plan.extract_menu(plan.load_json(FIXTURE_MENU))
    pmap = plan.load_json(PRODUCT_MAP)
    quotes_snapshot = len(REAL_QUOTES)

    def mk(codes, cid, satisfies=None):
        items = []
        for c in codes:
            info = menu[c]
            items.append({"code": c, "name": info["name"], "qty": 1,
                          "menuUnitPriceYuan": info["priceYuan"],
                          "prefilterUnitPriceYuan": info.get("prefilterPriceYuan"),
                          "priceStatus": info.get("priceStatus"),
                          "discountType": info.get("discountType")})
        pre = plan.prefilter_sum(items)
        return {"id": cid, "styleHint": "多品类", "items": items,
                "menuTotalYuan": round(sum(i["menuUnitPriceYuan"] for i in items), 2),
                "prefilterTotalYuan": pre,
                "satisfies": satisfies or {"burger": True, "drink": True, "side": True}}

    # --- A. 预算足够，存在经官方核价验证的可行方案（真实记录）---
    key_a = frozenset(["9900016057"])
    check("场景 A 素材存在（真实核价记录）", key_a in REAL_QUOTES)
    out_a = _run_verify([mk(["9900016057"], "A1")], {"A1": REAL_QUOTES[key_a]}, 25.0)
    check("A. 有真实核价的可行方案进入 verified",
          out_a["meta"]["verifiedCount"] == 1 and out_a["verified"][0]["id"] == "A1")
    check("A. 最终价格来自 calculate-price（priceSource 标明）",
          out_a["verified"][0]["officialPriceSource"] == "calculate-price")
    check("A. 应付 ¥23.50 与官方记录一致",
          out_a["verified"][0]["officialPriceYuan"] == 23.5,
          str(out_a["verified"][0]["officialPriceYuan"]))
    check("A. 剩余预算 = 25 − 23.5 = ¥1.50",
          out_a["verified"][0]["remainingYuan"] == 1.5)

    # --- B. 菜单价低于预算，官方核价超预算（真实记录：¥35 菜单 / ¥65 官方）---
    key_b = frozenset(["6102", "9900000890"])
    out_b = _run_verify([mk(["6102", "9900000890"], "B1")], {"B1": REAL_QUOTES[key_b]}, 35.0)
    check("B. 条件价候选被官方核价判为 over_budget",
          out_b["meta"]["verifiedCount"] == 0
          and out_b["rejected"][0]["status"] == "over_budget",
          str(out_b["rejected"]))
    check("B. 官方价 ¥65.00 被如实记录（未按菜单价 ¥35 处理）",
          out_b["rejected"][0]["officialPriceYuan"] == 65.0)

    # --- C. 预筛价未知，但官方核价可确认实际价格（合成样本，显式标注）---
    raw_c = _inject_unknown_price(plan.load_json(FIXTURE_MENU), ["9900005456"])
    menu_c, _ = plan.extract_menu_with_report(raw_c)
    items_c = [{"code": "9900005456", "name": menu_c["9900005456"]["name"], "qty": 1,
                "menuUnitPriceYuan": menu_c["9900005456"]["priceYuan"],
                "prefilterUnitPriceYuan": menu_c["9900005456"]["prefilterPriceYuan"],
                "priceStatus": menu_c["9900005456"]["priceStatus"],
                "discountType": menu_c["9900005456"]["discountType"]}]
    cand_c = {"id": "C1", "styleHint": "多品类", "items": items_c,
              "menuTotalYuan": menu_c["9900005456"]["priceYuan"],
              "prefilterTotalYuan": plan.prefilter_sum(items_c),
              "satisfies": {"burger": True, "drink": True, "side": True}}
    check("C. 该商品预筛价确实为 None（本地不可判定）",
          cand_c["prefilterTotalYuan"] is None)
    out_c = _run_verify([cand_c], {"C1": SYNTHETIC_UNVERIFIED_PRICE_QUOTE}, 25.0)
    check("C. ★ 预筛未知 ≠ 超预算：官方核价 ¥22.00 后进入 verified",
          out_c["meta"]["verifiedCount"] == 1
          and out_c["verified"][0]["officialPriceYuan"] == 22.0,
          str(out_c["verified"]))
    check("C. 最终价取自官方返回，而非菜单价 ¥34",
          out_c["verified"][0]["officialPriceYuan"] != cand_c["menuTotalYuan"])
    check("C. 合成样本未污染 REAL_QUOTES：同键真实记录仍是官方价 ¥34.00（合成值 ¥22.00）",
          frozenset(["9900005456"]) in REAL_QUOTES
          and REAL_QUOTES[frozenset(["9900005456"])]["data"]["price"] == 3400
          and SYNTHETIC_UNVERIFIED_PRICE_QUOTE["data"]["price"] == 2200)

    # --- D. 所有候选都超预算 ---
    out_d = _run_verify([mk(["6102", "9900000890"], "D1"), mk(["6102", "9900000891"], "D2")],
                        {"D1": REAL_QUOTES[frozenset(["6102", "9900000890"])],
                         "D2": REAL_QUOTES[frozenset(["6102", "9900000891"])]}, 35.0)
    check("D. 全部超预算 → verified 为空且 rejected 全是 over_budget",
          out_d["verified"] == []
          and all(r["status"] == "over_budget" for r in out_d["rejected"]))

    # --- E. 没有真实核价记录的候选 ---
    out_e = _run_verify([mk(["9900016057"], "E1")], {}, 25.0)
    check("E. 无核价记录 → quote_missing（不得当成核价成功）",
          out_e["rejected"][0]["status"] == "quote_missing"
          and out_e["meta"]["verifiedCount"] == 0)

    # --- F. 组合中包含 retired 商品 ---
    retired = {c for c, r in pmap["verifiedProducts"].items() if r.get("retired")}
    check("F. 映射表存在 retired 条目（素材有效）", len(retired) > 0, f"{len(retired)} 个")
    pool_f = plan.build_pool(menu, pmap)
    check("F. retired 商品一律不进入商品池",
          not ({i["code"] for i in pool_f} & retired), str(sorted({i["code"] for i in pool_f} & retired)))
    raw_f = json.loads(json.dumps(plan.load_json(FIXTURE_MENU)))
    for c in list(retired)[:1]:
        raw_f["data"]["meals"][c] = {"name": "注入的旧编码", "currentPrice": "1"}
    menu_f, _ = plan.extract_menu_with_report(raw_f)
    check("F. 即便菜单重新出现该编码，build_pool 仍强制排除",
          not ({i["code"] for i in plan.build_pool(menu_f, pmap)} & retired))

    # --- G. 用户要汉堡，但候选商品的汉堡属性尚未确认 ---
    unverified = [c for c in menu if c not in pmap["verifiedProducts"]]
    by_g = {i["code"]: i for i in pool_f}
    check("G. 菜单里确有「未核实」的在售商品（素材有效）",
          len(unverified) > 0, f"{len(unverified)} 个")
    check("G. ★ 未核实商品一律不得作汉堡锚点",
          all(by_g[c]["canSatisfyBurgerConstraint"] is False
              for c in unverified if c in by_g),
          str([c for c in unverified if c in by_g and by_g[c]["canSatisfyBurgerConstraint"]]))
    # 「名称含堡但未核实」清单：P1.8 后应为空。若将来再出现，仍必须被上面的断言拦住。
    name_burger_like = [c for c in unverified if "堡" in menu[c]["name"]]
    check("G. 「名称含堡但未核实」清单当前为空（P1.8 已清空；将来出现时必须继续排除）",
          name_burger_like == [], str(name_burger_like))

    # --- H. 菜单价与官方核价不一致 ---
    out_h = _run_verify([mk(["6102", "9900000890"], "H1")],
                        {"H1": REAL_QUOTES[frozenset(["6102", "9900000890"])]}, 100.0)
    disc = {d["id"]: d for d in out_h["discrepancies"]}
    check("H. 菜单价 ≠ 官方价 → 被记入 discrepancies", set(disc) == {"H1"}, str(sorted(disc)))
    check("H. deltaYuan = 官方 65.00 − 菜单 35.00 = 30.00",
          abs(disc["H1"]["deltaYuan"] - 30.0) <= 0.005, str(disc["H1"].get("deltaYuan")))
    check("H. 官方价被用作剩余预算的依据（100 − 65 = 35.00）",
          out_h["verified"][0]["remainingYuan"] == 35.0)

    check("★ 本测试全程未向 REAL_QUOTES 写入任何记录（合成数据不外溢）",
          len(REAL_QUOTES) == quotes_snapshot, f"{quotes_snapshot} → {len(REAL_QUOTES)}")


def test_verify_reports_four_amounts_without_claiming_coupon():
    """无券路径（verify）也必须能给全「原价 / 优惠 / 应付 / 菜单价」四值，
    且 discount > 0 时**绝不得**说成"用了优惠券"（P1.10 新增）。

    背景：SKILL.md 的输出模板要求四金额，但 verify 之前只输出 price，
    "原价/优惠"两格无从填写，逼生成层去猜 —— 这是实现与模板的真实不一致。
    """
    menu = plan.extract_menu(plan.load_json(FIXTURE_MENU))

    def run(codes, rec, budget, cid="V1"):
        items = [{"code": c, "name": menu[c]["name"], "qty": 1,
                  "menuUnitPriceYuan": menu[c]["priceYuan"]} for c in codes]
        cand_doc = {"candidates": [{"id": cid, "styleHint": "多品类", "items": items,
                                    "menuTotalYuan": round(sum(i["menuUnitPriceYuan"]
                                                               for i in items), 2),
                                    "satisfies": {"burger": True, "drink": True, "side": True}}]}
        return _run_verify(cand_doc["candidates"], {cid: rec}, budget)

    discounted = {k: v for k, v in REAL_QUOTES.items()
                  if (v.get("data") or {}).get("discount")
                  and all(c in menu for c in k)}
    check("存在真实 discount > 0 且编码仍在菜单里的记录（素材有效）", len(discounted) > 0,
          f"{len(discounted)} 条")
    key, rec = sorted(discounted.items(), key=lambda kv: sorted(kv[0]))[0]
    codes = sorted(key)
    out = run(codes, rec, 100.0)
    v = out["verified"][0]

    check("verify 输出四金额字段齐备",
          all(f in v for f in ("originalPriceYuan", "discountYuan",
                               "officialPriceYuan", "menuTotalYuan")),
          str(sorted(v.keys())))
    check("原价 = 官方 originalPrice/100", v["originalPriceYuan"] == rec["data"]["originalPrice"] / 100)
    check("优惠 = 官方 discount/100", v["discountYuan"] == rec["data"]["discount"] / 100)
    check("应付 = 官方 price/100（价格只来自 calculate-price）",
          v["officialPriceYuan"] == rec["data"]["price"] / 100)
    check("★ 店内促销导致的 discount > 0 不得被说成用了券（usedCoupon 恒为 false）",
          v["discountYuan"] > 0 and v["usedCoupon"] is False, str(v.get("usedCoupon")))
    check("discountSource 明确「不代表使用了优惠券」",
          "不代表使用了优惠券" in v["discountSource"])
    check("meta.discountNote 说明 discount 可能来自店内促销、不等于券",
          "店内促销" in out["meta"]["discountNote"]
          and "不等于使用了优惠券" in out["meta"]["discountNote"])

    # 缺 originalPrice / discount 的历史记录 → 必须给 None，不得据菜单价编造
    missing = [k for k, v2 in REAL_QUOTES.items()
               if "originalPrice" not in (v2.get("data") or {})
               and all(c in menu for c in k)]
    check("存在缺 originalPrice 的历史记录（素材有效）", len(missing) > 0, f"{len(missing)} 条")
    k2 = sorted(missing, key=lambda k: sorted(k))[0]
    out2 = run(sorted(k2), REAL_QUOTES[k2], 100.0, cid="V2")
    v2 = out2["verified"][0]
    check("官方未返回该字段时 originalPriceYuan / discountYuan 为 None（不推算填补）",
          v2["originalPriceYuan"] is None and v2["discountYuan"] is None,
          f"{v2['originalPriceYuan']} / {v2['discountYuan']}")
    check("此时仍如实给出官方应付价", isinstance(v2["officialPriceYuan"], float))


def test_ranking_rule_is_explicit():
    """`verify` 与 `compare` 的排序规则必须**显式声明**且与实际排序一致（P1.11 新增）。

    背景（本轮实测）：预算 ¥35 时 `verify` 的第一条是 ¥35.00（品类最全）而非 ¥14.00（最省）。
    这符合"尽量搭配"的诉求，但规则此前是**隐式**的，容易被生成层误读成价格排序。
    """
    import tempfile
    menu = plan.extract_menu(plan.load_json(FIXTURE_MENU))

    # 构造两个候选：便宜但简朴 vs 稍贵但品类全（仅用于验证排序逻辑，非真实报价）
    CHEAP, RICH = "9900008747", "9900016057"

    def mk(code, cid, drink, side, price_cents):
        return ({"id": cid, "styleHint": "多品类",
                 "items": [{"code": code, "name": menu[code]["name"], "qty": 1,
                            "menuUnitPriceYuan": menu[code]["priceYuan"]}],
                 "menuTotalYuan": menu[code]["priceYuan"],
                 "satisfies": {"burger": True, "drink": drink, "side": side}},
                {"success": True, "code": 200, "at": "2026-10-16T11:56:08+08:00",
                 "data": {"price": price_cents, "originalPrice": price_cents, "discount": 0}})

    cheap_c, cheap_q = mk(CHEAP, "V1", False, False, 1400)
    rich_c, rich_q = mk(RICH, "V2", True, True, 2350)

    with tempfile.TemporaryDirectory() as td:
        cp, qp, op = Path(td) / "c.json", Path(td) / "q.json", Path(td) / "o.json"
        cp.write_text(json.dumps({"candidates": [cheap_c, rich_c]}), encoding="utf-8")
        qp.write_text(json.dumps({"quotes": {"V1": cheap_q, "V2": rich_q}}), encoding="utf-8")
        plan.cmd_verify(type("A", (), {"candidates": str(cp), "quotes": str(qp),
                                       "budget": 35.0, "out": str(op)})())
        out = json.loads(op.read_text(encoding="utf-8"))
    check("verify 输出显式声明 rankingRule", "rankingRule" in out["meta"])
    check("verify 的 rankingRule 明确「不是价格排序」",
          "不是价格排序" in out["meta"]["rankingRule"])
    check("verify 实际按丰富度优先：品类全的排在便宜的前面",
          [v["id"] for v in out["verified"]] == ["V2", "V1"],
          str([v["id"] for v in out["verified"]]))
    check("verify 的首条确实不是最便宜的（规则与行为一致）",
          out["verified"][0]["officialPriceYuan"] > out["verified"][-1]["officialPriceYuan"])

    with tempfile.TemporaryDirectory() as td:
        cp, qp, op = Path(td) / "c.json", Path(td) / "q.json", Path(td) / "o.json"
        cp.write_text(json.dumps({"candidates": [rich_c, cheap_c]}), encoding="utf-8")
        qp.write_text(json.dumps({"quotes": {"V1": {"none": cheap_q}, "V2": {"none": rich_q}}}),
                      encoding="utf-8")
        plan.cmd_compare(type("A", (), {"candidates": str(cp), "quotes": str(qp),
                                        "budget": 35.0, "out": str(op), "max_probes": 20})())
        out2 = json.loads(op.read_text(encoding="utf-8"))
    check("compare 输出显式声明 rankingRule", "rankingRule" in out2["meta"])
    check("compare 的 rankingRule 说明按官方应付价升序",
          "升序" in out2["meta"]["rankingRule"])
    check("compare 实际按价格升序：便宜的在前",
          [v["id"] for v in out2["verified"]] == ["V1", "V2"],
          str([v["id"] for v in out2["verified"]]))


def test_p1_12_angus_series_and_consistency():
    """P1.12 新增的 7 个安格斯商品：结构 / 证据 / 映射→硬约束→核价 的一致性。

    覆盖要求的一致性检查：A 锚点、B 未确认不得满足硬约束、C 名称不算证据、
    E 组合商品不被拆分、F 映射更新不绕过官方核价。
    """
    import tempfile
    menu = plan.extract_menu(plan.load_json(FIXTURE_MENU))
    pmap = plan.load_json(PRODUCT_MAP)
    v = pmap["verifiedProducts"]
    pool = plan.build_pool(menu, pmap)
    by = {i["code"]: i for i in pool}
    NEW = ["511781", "511782", "9900005428", "9900005430",
           "9900000881", "9900000882", "9900006957"]

    # A. 已确认汉堡可作锚点 + 证据完整性
    for c in NEW:
        r = v.get(c)
        check(f"P1.12 {c} 已入表且 confidence=confirmed_detail",
              r is not None and r["confidence"] == "confirmed_detail")
        check(f"P1.12 {c} 的 evidence 完整（tool / storeCode / at / raw）",
              bool(r) and r["evidence"].get("tool") == "query-meal-detail"
              and r["evidence"].get("storeCode") == "3230019"
              and str(r["evidence"].get("at", "")).startswith("2026-10-16T12:03")
              and bool(r["evidence"].get("raw")))
        check(f"P1.12 {c} 默认组成含汉堡、可作锚点、不必多选",
              bool(r) and r["containsBurgerByDefault"] is True
              and not r.get("requiresUserSelection")
              and by[c]["canSatisfyBurgerConstraint"] is True)

    # 原子商品：rounds 为空 → 商品名即组成
    for c in ("511781", "511782"):
        check(f"P1.12 {c} 是原子商品（kind=single, roundsLen=0, 默认组成为空）",
              v[c]["kind"] == "single" and v[c]["roundsLen"] == 0
              and v[c]["defaultComposition"] == [])

    # ★ 关键判定：名称含「随心选」，但第 1 轮两个选项**全是**安格斯厚牛堡
    r6957 = v["9900006957"]
    check("★ 9900006957 第 1 轮默认项是 511782 芝士安格斯厚牛堡",
          r6957["defaultComposition"][0]["choiceCode"] == "511782")
    check("★ 9900006957 的 evidence 记录了「两个选项都是安格斯厚牛堡」这一判定依据",
          "两个都是安格斯厚牛堡" in r6957["evidence"]["raw"])
    check("★ 9900006957 未被标为多选商品（四区均为单选）",
          not r6957.get("requiresUserSelection"))

    # E. 组合商品不得被拆分：套餐子项不因被套餐引用而进入商品池
    check("★ 套餐内子项 515106（麦乐鸡4块）不是顶层商品，也不进入商品池",
          "515106" not in menu and "515106" not in by)
    for c in ("9900005428", "9900005430"):
        check(f"P1.12 {c} compositionSize == 默认组成件数 == 3（按整份参与搜索）",
              by[c]["compositionSize"] == len(v[c]["defaultComposition"]) == 3)
    for c in ("9900000881", "9900000882", "9900006957"):
        check(f"P1.12 {c} compositionSize == 默认组成件数 == 4",
              by[c]["compositionSize"] == len(v[c]["defaultComposition"]) == 4)

    # C. 名称含「堡」不是证据 —— 名称推断商品仍不得作锚点
    inferred_burger = [i for i in pool if not i["verified"] and "堡" in i["name"]]
    check("★ 名称含「堡」但未核实的商品一律不得作锚点（名称不算证据）",
          all(not i["canSatisfyBurgerConstraint"] for i in inferred_burger),
          str([i["code"] for i in inferred_burger]))

    # F. 映射更新不绕过官方核价：映射表不含价格；没有核价记录仍判 quote_missing
    raw = PRODUCT_MAP.read_text(encoding="utf-8")
    check("映射表整体仍不含任何价格字段（映射不是价格来源）",
          all(k not in raw for k in ("priceYuan", "currentPrice", "originalPrice", '"price"')))
    cand = {"candidates": [{"id": "Z1", "styleHint": "多品类",
             "items": [{"code": "9900005428", "name": menu["9900005428"]["name"],
                        "qty": 1, "menuUnitPriceYuan": menu["9900005428"]["priceYuan"]}],
             "menuTotalYuan": menu["9900005428"]["priceYuan"],
             "satisfies": {"burger": True, "drink": True, "side": True}}]}
    with tempfile.TemporaryDirectory() as td:
        cp, qp, op = Path(td) / "c.json", Path(td) / "q.json", Path(td) / "o.json"
        cp.write_text(json.dumps(cand), encoding="utf-8")
        qp.write_text(json.dumps({"quotes": {}}), encoding="utf-8")
        plan.cmd_verify(type("A", (), {"candidates": str(cp), "quotes": str(qp),
                                       "budget": 50.0, "out": str(op)})())
        out = json.loads(op.read_text(encoding="utf-8"))
    check("★ 新入表商品没有核价记录时仍判 quote_missing（映射不代替核价）",
          out["rejected"][0]["status"] == "quote_missing"
          and out["meta"]["verifiedCount"] == 0)


# ---------------------------------------------------------------------- main
def test_p1_14_hard_constraints_drink_and_side():
    """P1.14：饮料/小食「必须有」需要真正的**硬约束**入口，不能被静默降级成软偏好。

    用仓库中的**真实菜单快照**驱动内核（不调用 MCP），验证的是内核硬约束语义。
    """
    menu = plan.extract_menu(plan.load_json(FIXTURE_MENU))
    pmap = plan.load_json(PRODUCT_MAP)
    pool = plan.build_pool(menu, pmap)

    # ① 复现修复前的缺陷：只给软偏好 --prefer-drink，内核仍可能产出不含饮料的候选
    soft = plan.select_candidates(
        plan.dedupe_plans(plan.search_plans(pool, 25.0, True, False)),
        25.0, True, False, 6)
    lacking = [c["id"] for c in soft if not c["metrics"]["categories"].get("drink")]
    check("缺陷可复现：软偏好 --prefer-drink 仍会产出不含饮料的候选",
          len(lacking) > 0, f"不含饮料的候选={lacking}")

    # ② 硬约束：要求饮料后，所有候选都必须含饮料
    hard = plan.select_candidates(
        plan.dedupe_plans(plan.search_plans(pool, 25.0, True, False,
                                            plan.DEFAULT_MAX_ITEMS, True, False)),
        25.0, True, False, 6)
    check("--require-drink 能产出候选", len(hard) > 0, f"{len(hard)} 个")
    check("★ 硬约束 --require-drink 下**所有**候选都含饮料",
          all(c["metrics"]["categories"].get("drink") for c in hard),
          str([c["id"] for c in hard
               if not c["metrics"]["categories"].get("drink")]))
    check("硬约束不破坏原有的汉堡硬约束",
          all(c["metrics"]["categories"].get("burger") for c in hard))

    # ③ 硬约束叠加：汉堡 + 饮料 + 小食
    both = plan.select_candidates(
        plan.dedupe_plans(plan.search_plans(pool, 35.0, True, True,
                                            plan.DEFAULT_MAX_ITEMS, True, True)),
        35.0, True, True, 6)
    bad = [c["id"] for c in both
           if not (c["metrics"]["categories"].get("drink")
                   and c["metrics"]["categories"].get("side"))]
    check("★ 叠加硬约束（饮料+小食）下所有候选都满足",
          not bad, f"不满足的候选={bad}")

    # ④ 保守性：硬约束无法满足时不产出方案
    impossible = plan.search_plans(pool, 5.0, True, True,
                                   plan.DEFAULT_MAX_ITEMS, True, True)
    check("★ 硬约束无解时不产出方案（不擅自放宽硬约束）",
          not impossible, f"plan 数={len(impossible)}")

    # ⑤ 元信息必须如实申报硬约束（防止又被当成普通偏好）
    src = (ROOT / "scripts" / "plan.py").read_text(encoding="utf-8")
    check("硬约束已写入 meta.hardConstraints",
          "mustContainDrink" in src and "mustContainSide" in src)


def test_p1_15_hard_constraint_chain_and_exceptions():
    """P1.15：硬约束从 NL 到推荐的完整链路 + 异常路径（内核层，基于真实菜单快照）。

    重要前提（与 SKILL.md 一致）：本仓库**没有自然语言解析代码**，
    NL→槽位的映射由 SKILL.md 规定、由 agent 在执行时落实。
    因此本测试把「SKILL.md 解析后的正确槽位」当作内核输入，检验内核是否守住契约：
      - 软偏好（尽量有）绝不被偷偷升级为硬约束（必须有）；
      - 硬约束（必须有）缺失即整组丢弃，不擅自放宽；
      - 核价失败 / 超预算 / 价格不一致时诚实处置，绝不冒充「已核价」或伪造价格。
    fixture 是 2026-10-16 11:55 门店 3230019 的真实菜单快照（HISTORICAL_REAL）。
    """
    menu = plan.extract_menu(plan.load_json(FIXTURE_MENU))
    pmap = plan.load_json(PRODUCT_MAP)
    pool = plan.build_pool(menu, pmap)

    def sel(budget, prefer_drink, prefer_side,
            require_drink=False, require_side=False, maxc=6):
        plans = plan.dedupe_plans(plan.search_plans(
            pool, budget, prefer_drink, prefer_side,
            plan.DEFAULT_MAX_ITEMS, require_drink, require_side))
        return plan.select_candidates(plans, budget, prefer_drink, prefer_side, maxc)

    # ---- A. 25 元，必须有汉堡和饮料（两者都是硬约束）----
    A = sel(25.0, True, False, require_drink=True, require_side=False)
    check("A. ¥25 必须有汉堡+饮料：能产出候选", len(A) > 0, f"{len(A)} 个")
    check("A. ★ 所有候选都满足「含饮料」硬约束",
          all(c["metrics"]["categories"].get("drink") for c in A),
          str([c["id"] for c in A if not c["metrics"]["categories"].get("drink")]))
    check("A. 所有候选都满足「含汉堡」硬约束（硬约束未因加饮料而失效）",
          all(c["metrics"]["categories"].get("burger") for c in A))

    # ---- B. 25 元，必须有汉堡，尽量有饮料（软偏好，绝不能是硬约束）----
    B_soft = sel(25.0, True, False, require_drink=False, require_side=False)
    drinkless = [c["id"] for c in B_soft
                 if not c["metrics"]["categories"].get("drink")]
    check("B. ★ 软偏好「尽量有饮料」下内核允许出现不含饮料的候选"
          "（证明没有被偷偷升级为硬约束）",
          len(drinkless) > 0, f"不含饮料候选={drinkless}")
    B_hard = sel(25.0, True, False, require_drink=True, require_side=False)
    check("B. 对照：同预算 require_drink=True 时所有候选都含饮料",
          all(c["metrics"]["categories"].get("drink") for c in B_hard))
    check("B. 对照：硬约束比软偏好更严格，候选数不会更多",
          len(B_hard) <= len(B_soft), f"hard={len(B_hard)} soft={len(B_soft)}")

    # ---- C. 35 元，必须有汉堡和饮料，尽量丰富（丰富度是软偏好/默认排序）----
    C = sel(35.0, True, True, require_drink=True, require_side=False)
    check("C. ¥35 必须有汉堡+饮料：所有候选都含饮料",
          all(c["metrics"]["categories"].get("drink") for c in C))
    check("C. 丰富度偏好不改变硬约束（仍全部含饮料）",
          all(c["metrics"]["categories"].get("drink") for c in C))

    # ---- D. 35 元，必须有汉堡和小食（硬约束）----
    D = sel(35.0, False, True, require_drink=False, require_side=True)
    check("D. ¥35 必须有汉堡+小食：所有候选都含小食",
          all(c["metrics"]["categories"].get("side") for c in D),
          str([c["id"] for c in D if not c["metrics"]["categories"].get("side")]))

    # ---- E. 5 元，必须有汉堡和饮料（不可行 → 诚实无解，不擅自放宽）----
    E_plans = plan.search_plans(pool, 5.0, True, False,
                                plan.DEFAULT_MAX_ITEMS, True, False)
    check("E. ★ ¥5 必须有汉堡+饮料：没有任何可行组合（不擅自放宽硬约束）",
          not E_plans, f"plan 数={len(E_plans)}")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mp = Path(td) / "m.json"; op = Path(td) / "o.json"
        mp.write_text(json.dumps(plan.load_json(FIXTURE_MENU)), encoding="utf-8")
        plan.cmd_candidates(type("A", (), {
            "menu": str(mp), "map": str(PRODUCT_MAP), "budget": 5.0,
            "prefer_drink": True, "prefer_side": False,
            "max_items": 4, "max_candidates": 6, "store_code": "3230019",
            "out": str(op), "require_drink": True, "require_side": False})())
        out_e = json.loads(op.read_text(encoding="utf-8"))
    check("E. 无解时内核必输出 noSolutionAdvice（带最低汉堡价与免责声明）",
          out_e["noSolutionAdvice"] is not None
          and out_e["noSolutionAdvice"]["cheapestConfirmedBurger"] is not None
          and bool(out_e["noSolutionAdvice"]["disclaimer"]))
    gap = out_e["noSolutionAdvice"]["shortfallYuan"]
    check("E. 无解说明如实给出最低汉堡与预算的真实差额（正数，不伪造「差一点点」）",
          isinstance(gap, (int, float)) and gap > 0, f"shortfallYuan={gap}")

    # ---- F. 缺预算：内核边界应拒绝非正预算（SKILL.md 层要求先追问，不得编造）----
    for bad_budget in (0.0, -5.0):
        raised = False
        try:
            with tempfile.TemporaryDirectory() as td:
                mp = Path(td) / "m.json"; op = Path(td) / "o.json"
                mp.write_text(json.dumps(plan.load_json(FIXTURE_MENU)), encoding="utf-8")
                plan.cmd_candidates(type("A", (), {
                    "menu": str(mp), "map": str(PRODUCT_MAP), "budget": bad_budget,
                    "prefer_drink": False, "prefer_side": False,
                    "max_items": 4, "max_candidates": 6, "store_code": "3230019",
                    "out": str(op)})())
        except SystemExit:
            raised = True
        check(f"F. 预算={bad_budget}（非正）被内核拒绝（防止无依据编造预算）", raised)

    # ---- 异常 1：calculate-price 返回 60006 失败 → quote_failed，不得冒充已核价 ----
    cand_fail = {"candidates": [{"id": "X1", "items": [
        {"code": "9900016057", "name": "牛肉堡单品精选", "qty": 1,
         "menuUnitPriceYuan": 14.0}],
        "menuTotalYuan": 14.0, "satisfies": {"burger": True}}]}
    quotes_fail = {"quotes": {"X1": {"success": False, "code": 60006,
        "message": "商品已售罄或不可售，换一个商品或检查orderType/beType"}}}
    out_fail = _run_verify(cand_fail["candidates"], quotes_fail["quotes"], 25.0)
    check("异常. 60006 失败被识别为 quote_failed（不是 quote_missing）",
          out_fail["rejected"] and out_fail["rejected"][0]["status"] == "quote_failed")
    check("异常. 失败核价方案不得出现在 verified（不冒充已核价）",
          out_fail["meta"]["verifiedCount"] == 0 and len(out_fail["verified"]) == 0)

    # ---- 异常 2：超预算候选 → over_budget，且不冒充已核价 ----
    cand_ob = {"candidates": [{"id": "Y1", "items": [
        {"code": "9900015183", "name": "精选单人餐三件套", "qty": 1,
         "menuUnitPriceYuan": 35.0}],
        "menuTotalYuan": 35.0,
        "satisfies": {"burger": True, "drink": True, "side": True}}]}
    quotes_ob = {"quotes": {"Y1": REAL_QUOTES[frozenset(["9900015183"])]}}
    out_ob = _run_verify(cand_ob["candidates"], quotes_ob["quotes"], 25.0)
    check("异常. 超预算被拒绝且状态为 over_budget",
          out_ob["rejected"] and out_ob["rejected"][0]["status"] == "over_budget")
    check("异常. 超预算方案不得出现在 verified", out_ob["meta"]["verifiedCount"] == 0)

    # ---- 异常 3：核价价与菜单价不一致 → 记录 discrepancy，仍以官方价为准 ----
    key = frozenset(["9900005456"])
    check("异常. 素材存在（真实核价记录 9900005456=¥34.00）", key in REAL_QUOTES)
    cand_d = {"candidates": [{"id": "Z1", "items": [
        {"code": "9900005456", "name": "示例套餐", "qty": 1,
         "menuUnitPriceYuan": 54.5}],
        "menuTotalYuan": 54.5,
        "satisfies": {"burger": True, "drink": True, "side": True}}]}
    quotes_d = {"quotes": {"Z1": REAL_QUOTES[key]}}
    out_d = _run_verify(cand_d["candidates"], quotes_d["quotes"], 100.0)
    check("异常. 菜单价与官方价不符时记录 discrepancy（不静默覆盖）",
          len(out_d["discrepancies"]) > 0)
    check("异常. 仍以官方核价作为应付价（不把菜单价当成交价）",
          out_d["verified"]
          and out_d["verified"][0]["officialPriceYuan"]
          == round(REAL_QUOTES[key]["data"]["price"] / 100, 2))

    # ---- 最终回答诚实性：verify 如实照抄官方四金额，usedCoupon 恒 false ----
    key_a = frozenset(["9900016057"])
    cand_h = {"candidates": [{"id": "H1", "items": [
        {"code": "9900016057", "name": "牛肉堡单品精选", "qty": 1,
         "menuUnitPriceYuan": 14.0}],
        "menuTotalYuan": 14.0, "satisfies": {"burger": True}}]}
    quotes_h = {"quotes": {"H1": REAL_QUOTES[key_a]}}
    out_h = _run_verify(cand_h["candidates"], quotes_h["quotes"], 25.0)
    v = out_h["verified"][0]
    rec = REAL_QUOTES[key_a]["data"]
    check("诚实. originalPriceYuan 直接照抄官方（未推算/未用菜单价顶替）",
          v["originalPriceYuan"] == (round(rec["originalPrice"] / 100, 2)
              if isinstance(rec.get("originalPrice"), (int, float)) else None))
    check("诚实. discountYuan 直接照抄官方，缺失为 None（未推算）",
          v["discountYuan"] == (round(rec["discount"] / 100, 2)
              if isinstance(rec.get("discount"), (int, float)) else None))
    check("诚实. usedCoupon 恒为 false（无券路径：折扣来自店内促销而非券）",
          v["usedCoupon"] is False)


def main() -> int:
    test_product_map_has_no_price()
    test_classification_safety()
    test_search_constraints()
    test_quote_parsing()
    test_quote_provenance()
    test_retired_skus_are_excluded_everywhere()
    test_menu_fixture_provenance()
    test_p1_8_new_burgers()
    test_first_real_discount_evidence()
    test_price_semantics_resolver()
    test_conditional_price_evidence_matches_original()
    test_conditional_price_never_counts_as_budget_feasible()
    test_deferred_budget_check_is_conservative()
    test_official_price_authoritative_over_prefilter()
    test_code_rotation_evidence()
    test_end_to_end_with_real_quotes()
    test_reject_paths()
    test_no_solution_advice()
    test_selection_prefers_credible_over_undetermined()
    test_candidate_composition_reported()
    test_end_to_end_scenarios()
    test_verify_reports_four_amounts_without_claiming_coupon()
    test_ranking_rule_is_explicit()
    test_p1_12_angus_series_and_consistency()
    test_p1_14_hard_constraints_drink_and_side()
    test_p1_15_hard_constraint_chain_and_exceptions()

    failed = [r for r in RESULTS if not r[1]]
    for label, ok, detail in RESULTS:
        print(f"{'[通过]' if ok else '[失败]'} {label}" + (f"  ({detail})" if detail else ""))
    print(f"\n测试结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
