#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plan.py — 麦麦预算点餐师 的本地内核（组合搜索 + 核价校验）

设计原则（与 SKILL.md 一致）：
  1. 纯本地、离线、无副作用。不发起任何网络请求，不下单、不支付、不领券。
  2. 绝不硬编码价格。所有价格来自运行时传入的实时菜单文件（query-meals 的真实返回）。
  3. 只有 references/product-map.json 中 confidence = confirmed_detail 且
     containsBurgerByDefault = true 的商品，才能满足「必须含汉堡」这个硬性条件。
  4. 名称推断出来的饮料/小食只能满足「可选偏好」，永远不能用于硬性条件。

子命令：
  candidates  菜单 + 映射 + 约束  ->  候选组合（含菜单预估价，未核价）
  verify      候选组合 + calculate-price 真实返回  ->  核价结论
  selftest    内置最小自检（用 tests/fixtures 下的真实数据）

用法示例：
  python3 scripts/plan.py candidates --menu tests/fixtures/menu_real.json \
      --map references/product-map.json --budget 25 --prefer-drink --max-candidates 6
  python3 scripts/plan.py verify --candidates /tmp/cand.json \
      --quotes /tmp/quotes.json --budget 25
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime
from pathlib import Path

CENTS_PER_YUAN = 100
DEFAULT_MAX_ITEMS = 4          # 单个方案最多几件商品（防止堆砌）
DEFAULT_MAX_CANDIDATES = 6     # 送去官方核价的候选数量上限
PRICE_TOLERANCE_YUAN = 0.005   # 浮点比较容差
MAX_PLAN_GUARD = 400000        # 搜索安全阀，防止极端参数下内存爆掉

# --------------------------------------------------------------------------
# 菜单价格语义（P1.9）
# --------------------------------------------------------------------------
# 背景：菜单的 currentPrice 有时是**条件价**（需随单购麦金卡才成立），
# 直接拿它做预算预筛会把 ¥58 的商品当成 ¥28，从而选出根本买不起的组合。
# 字段语义**不靠命名猜测**，而是由以下证据确定（详见 references/menu-price-semantics.md）：
#   ① 官方工具定义原文（query-meals 的字段说明）：
#      withOrder = "随单购商品，当商品享受随单购优惠价时，用户必须要选择随单购才可享受优惠，
#                   **否则将以原价购买**"
#   ② 实测（门店 3230019，`orderType=1`、`beType=1`，共 26 条裸核价）：
#      · 午餐时段（2026-10-16 11:25 与 12:06）：16 个「随单购麦金卡优惠」商品逐一裸核价，
#        calculate-price 的 price **全部**等于 originalPrice（无一例外）；
#      · 早餐时段（2026-10-16 13:15–13:16，用 `reservationDate=2026-10-17 08:00` 取得）：
#        10 个「随单购早餐卡优惠」商品逐一裸核价，price **同样全部**等于 originalPrice。
#      → 「随单购* 的条件价 = 原价」这一语义**跨时段成立**。
#      对照组：4 个「促销优惠」商品与 2 个无 discountType 商品，price 均等于 currentPrice。
PRICE_STATUS_MENU = "menu_price_is_effective"
PRICE_STATUS_PROMO_MENU = "promo_price_is_effective"
PRICE_STATUS_CONDITIONAL = "conditional_price_replaced_by_original"
PRICE_STATUS_UNKNOWN = "unknown_price_semantics"

PRICE_STATUS_LABELS = {
    PRICE_STATUS_MENU: "菜单现价即实际价（无优惠字段声明）",
    PRICE_STATUS_PROMO_MENU: "促销价即实际价（促销已自动生效，无需额外条件）",
    PRICE_STATUS_CONDITIONAL: "菜单现价是条件价；预筛改用原价（已实测原价 == 官方裸核价）",
    PRICE_STATUS_UNKNOWN: "无法证明无条件价 → 不做本地预算判断，一律交官方核价",
}

# 「促销优惠」的语义已被实测（521986 / 507409）。其余未验证取值一律进 unknown 分支（保守）。
DISCOUNT_TYPE_VALUE_SEMANTICS = {
    "促销优惠": PRICE_STATUS_PROMO_MENU,
}
# 「随单购*」表示需要额外购买会员卡才成立的条件价。
# ⚠️ 这里**只精确匹配已实测过的取值**，不做前缀泛化 —— 但**名单本身可扩展**：
#    每拿到一个新取值下的真实裸核价证据，就可以把它加进来。
#
#    已实测（门店 3230019，两个时段共 26 条裸核价，全部 price == originalPrice）：
#      · 随单购麦金卡优惠 —— 午餐时段 16 条
#      · 随单购早餐卡优惠 —— 早餐时段 10 条
#
# ⚠️ 仍**故意排除**不带「随单购」前缀的 `早餐卡优惠` / `麦金卡优惠`：
#    这两个取值在所有已取菜单里**从未出现过**，零证据 → 必须走 unknown 保守分支。
#    注意这正是「不做前缀泛化」的意义所在：若把规则写成"含『卡优惠』即视为条件价"，
#    就会把这两个未验证取值一并放进来 —— 那是猜测，不是证据。
VERIFIED_CONDITIONAL_DISCOUNT_TYPES = {"随单购麦金卡优惠", "随单购早餐卡优惠"}


def resolve_price_semantics(info: dict) -> dict:
    """判定一个菜单条目的「预筛价」。**只返回结论，不猜**。

    返回：
      menuPriceYuan      —— 菜单展示价（currentPrice），含义不变，仅用于展示/差异对比
      originalPriceYuan  —— 原价（划线价），解析失败为 None
      prefilterPriceYuan —— 用于本地预算预筛的价格；**None 表示不得做本地预算判断**
      priceStatus        —— 见 PRICE_STATUS_*
      priceReason        —— 人可读的判定依据（含证据来源）
    """
    cur = to_yuan(info.get("currentPrice", info.get("price")))
    orig = to_yuan(info.get("originalPrice"))
    dt = info.get("discountType")
    dt = str(dt).strip() if dt is not None else None
    if dt == "" or dt == "null":
        dt = None

    common = {
        "menuPriceYuan": cur,
        "originalPriceYuan": orig,
        "discountType": dt,
        "canWithOrder": bool(info.get("canWithOrder")),
        "hasWithOrder": isinstance(info.get("withOrder"), dict),
    }

    if cur is None:
        return {**common, "prefilterPriceYuan": None, "priceStatus": PRICE_STATUS_UNKNOWN,
                "priceReason": "菜单缺少可解析的 currentPrice；不得把未知价当成确定价。"}

    if dt is None:
        return {**common, "prefilterPriceYuan": cur, "priceStatus": PRICE_STATUS_MENU,
                "priceReason": "无 discountType 声明 → 菜单现价视为该商品的销售价（已实测一例："
                               "9900005456 裸核价 ¥34 == currentPrice）。"}

    if dt in DISCOUNT_TYPE_VALUE_SEMANTICS:
        return {**common, "prefilterPriceYuan": cur,
                "priceStatus": DISCOUNT_TYPE_VALUE_SEMANTICS[dt],
                "priceReason": f"discountType「{dt}」的语义已实测：促销已自动生效，"
                               f"calculate-price 的 subtotal 等于 currentPrice。"}

    if dt in VERIFIED_CONDITIONAL_DISCOUNT_TYPES:
        if orig is not None and orig > 0:
            return {**common, "prefilterPriceYuan": orig, "priceStatus": PRICE_STATUS_CONDITIONAL,
                    "priceReason": f"discountType「{dt}」是条件价（需随单购会员卡）。"
                                   f"官方定义明确「否则将以原价购买」；实测 26 个同类商品"
                                   f"逐一裸核价均等于 originalPrice"
                                   f"（午餐 `随单购麦金卡优惠` 16 条 + 早餐 `随单购早餐卡优惠` 10 条，"
                                   f"均为门店 3230019）→ 预筛改用原价。"}
        return {**common, "prefilterPriceYuan": None, "priceStatus": PRICE_STATUS_UNKNOWN,
                "priceReason": f"discountType「{dt}」是条件价，但缺少可用的 originalPrice，"
                               f"无法确定无条件价 → 不做本地预算判断。"}

    # 兜底：取值域里还有「早餐卡优惠」「麦金卡优惠」「随单购早餐卡优惠」等**未实测**类型。
    # 它们可能同样需要额外购卡才成立，也可能不是 —— 没有证据 → **一律按未知保守处理**，
    # 既不套用原价，也不用菜单价充当无条件价。
    return {**common, "prefilterPriceYuan": None, "priceStatus": PRICE_STATUS_UNKNOWN,
            "priceReason": f"discountType「{dt}」的语义**未经实测**（官方取值域仍含不带"
                           f"「随单购」前缀的「早餐卡优惠」「麦金卡优惠」，二者与已验证的"
                           f"「随单购*」不是一回事，零样本 → 不可套用同一结论），"
                           f"无法证明无条件价 → 不做本地预算判断，交官方核价。"}




# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------

def fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"[错误] {msg}", file=sys.stderr)
    raise SystemExit(2)


def to_yuan(value) -> float | None:
    """把菜单里的价格（通常是字符串，单位元）转成 float。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).strip().replace("¥", "").replace("元", "").replace(",", "")
    if not text:
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def fmt_yuan(value: float) -> str:
    return f"¥{value:.2f}"


def largest_json_object(text: str) -> dict | None:
    """从任意文本里抽出最大的合法 JSON 对象。

    MCP 工具返回的文本常常是「说明文字 + 原始 JSON」拼在一起，
    这里用 raw_decode 逐个尝试 '{' 起点，取解析长度最大的那个对象。
    """
    decoder = json.JSONDecoder()
    best: dict | None = None
    best_len = -1
    cursor = 0
    limit = len(text)
    while cursor < limit:
        start = text.find("{", cursor)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(obj, dict) and (end - start) > best_len:
            best, best_len = obj, end - start
        cursor = start + 1
    return best


def load_json(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        fail(f"文件不存在：{p}")
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        fail(f"文件为空：{p}")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = largest_json_object(text)
        if obj is None:
            fail(f"无法从 {p} 中解析出 JSON 对象。")
    if not isinstance(obj, dict):
        fail(f"{p} 的顶层不是 JSON 对象。")
    return obj


def dump_json(obj, out: str | None) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"[已写入] {out}", file=sys.stderr)
    else:
        print(text)


# --------------------------------------------------------------------------
# 菜单解析
# --------------------------------------------------------------------------

def extract_menu_with_report(raw: dict) -> tuple[dict[str, dict], dict]:
    """把 query-meals 的返回（或精简形式）统一成 {code: {name, priceYuan, ...}}。

    支持三种输入：
      A. query-meals 原始返回：{"success":true,"data":{"meals":{code:{name,currentPrice,...}}}}
      B. 只有 meals 映射：{"meals":{...}}
      C. 精简形式：{"items":[{"code":..,"name":..,"currentPrice":..}]}

    P1.9 起额外带上价格语义字段（priceStatus / prefilterPriceYuan / originalPriceYuan /
    discountType），并把**无法使用的条目记账**而不是静默丢弃。

    返回 (menu, report)：
      menu   —— {code: {...}}，字段名 `priceYuan` 仍表示**菜单展示价**（含义未变）
      report —— {"dropped": [...], "priceStatusCounts": {...}, "conditionalCodes": [...]}
    """
    node = raw
    if isinstance(node.get("data"), dict):
        node = node["data"]

    dropped: list[dict] = []
    out: dict[str, dict] = {}

    def absorb(code, name, info) -> None:
        name = str(name or "").strip()
        if not code or not name:
            dropped.append({"code": str(code) if code else None, "name": name or None,
                            "reason": "缺少 code 或 name"})
            return
        sem = resolve_price_semantics(info)
        if sem["menuPriceYuan"] is None:
            dropped.append({"code": str(code), "name": name,
                            "reason": "currentPrice 缺失或无法解析为数字",
                            "rawCurrentPrice": info.get("currentPrice")})
            return
        out[str(code)] = {"name": name, "priceYuan": sem["menuPriceYuan"], **sem}

    meals = node.get("meals") if isinstance(node, dict) else None
    if isinstance(meals, dict) and meals:
        for code, info in meals.items():
            if not isinstance(info, dict):
                dropped.append({"code": str(code), "name": None, "reason": "条目不是对象"})
                continue
            absorb(code, info.get("name", ""), info)
        if out:
            return out, _price_report(out, dropped)

    items = raw.get("items") if isinstance(raw.get("items"), list) else None
    if items is None and isinstance(node, dict) and isinstance(node.get("items"), list):
        items = node["items"]
    if items:
        for it in items:
            if not isinstance(it, dict):
                dropped.append({"code": None, "name": None, "reason": "条目不是对象"})
                continue
            absorb(it.get("code", it.get("productCode")),
                   it.get("name", it.get("productName", "")), it)
        if out:
            return out, _price_report(out, dropped)

    fail("菜单文件中找不到可用商品。期望 data.meals / meals / items 之一。")
    return {}, {}  # pragma: no cover


def _price_report(menu: dict, dropped: list) -> dict:
    """汇总价格语义分布，便于调用方如实说明「哪些商品被特殊处理了」。"""
    counts: dict[str, int] = {}
    conditional = []
    unknown = []
    for code, info in menu.items():
        st = info.get("priceStatus")
        counts[st] = counts.get(st, 0) + 1
        if st == PRICE_STATUS_CONDITIONAL:
            conditional.append({
                "code": code, "name": info["name"],
                "menuPriceYuan": info["priceYuan"],
                "prefilterPriceYuan": info["prefilterPriceYuan"],
                "originalPriceYuan": info.get("originalPriceYuan"),
                "discountType": info.get("discountType"),
            })
        elif st == PRICE_STATUS_UNKNOWN:
            unknown.append({"code": code, "name": info["name"],
                            "discountType": info.get("discountType"),
                            "menuPriceYuan": info["priceYuan"]})
    return {
        "dropped": dropped,
        "priceStatusCounts": counts,
        "conditionalCodes": [c["code"] for c in conditional],
        "conditionalItems": sorted(conditional, key=lambda x: x["code"]),
        "unknownPriceItems": sorted(unknown, key=lambda x: x["code"]),
    }


def extract_menu(raw: dict) -> dict[str, dict]:
    """兼容旧签名：只返回菜单本身。需要「被丢弃条目」报告时用 extract_menu_with_report。"""
    return extract_menu_with_report(raw)[0]



# --------------------------------------------------------------------------
# 商品分类
# --------------------------------------------------------------------------

def name_classify(name: str, atomic_rules: dict) -> str:
    """按 references/product-map.json 的 atomicNameRules 做兜底分类。

    支持两种匹配方式（由规则里的 match 字段决定）：
      contains（默认）：名称包含关键词
      suffix          ：名称以关键词结尾
    suffix 是为「酱」这类词准备的——用 contains 会误伤「蘸酱炸鸡」等真实餐品。
    """
    for rule in atomic_rules.get("rules", []):
        mode = rule.get("match", "contains")
        for kw in rule.get("keywords", []):
            if not kw:
                continue
            if mode == "suffix":
                if name.endswith(kw):
                    return rule.get("classification", "unknown")
            elif kw in name:
                return rule.get("classification", "unknown")
    return "unknown"


def build_pool(menu: dict[str, dict], product_map: dict) -> list[dict]:
    """把实时菜单 + 映射表 合成为可搜索的商品池。"""
    verified = product_map.get("verifiedProducts", {})
    atomic_rules = product_map.get("atomicNameRules", {})
    pool: list[dict] = []

    for code, info in menu.items():
        name = info["name"]
        price = info["priceYuan"]
        # P1.9：预筛价与展示价分开。prefilterPriceYuan 为 None 表示「无条件价无法确定」，
        # 此时**不得**用它做本地预算判断，一律留给官方 calculate-price。
        prefilter = info.get("prefilterPriceYuan")
        price_fields = {
            "prefilterPriceYuan": prefilter,
            "originalPriceYuan": info.get("originalPriceYuan"),
            "priceStatus": info.get("priceStatus", PRICE_STATUS_UNKNOWN),
            "discountType": info.get("discountType"),
            "budgetCheckDeferred": prefilter is None,
        }
        rec = verified.get(code)

        if rec:
            # 已确认失效 / 已轮换的旧编码（retired）：一律不得进入商品池。
            # 即使菜单里因为缓存或数据回滚而重现该编码，也不得把它当作可售候选推荐。
            # 判定依据见 references/product-map.json 的 codeRotation 表。
            if rec.get("retired"):
                continue
            composition = rec.get("defaultComposition") or []
            pool.append({
                "code": code,
                "name": rec.get("nameFromDetail") or name,
                "nameFromMenu": name,
                "priceYuan": price,
                **price_fields,
                "verified": True,
                "confidence": rec.get("confidence", "confirmed_detail"),
                "source": "references/product-map.json",
                "kind": rec.get("kind", "combo"),
                "categories": dict(rec.get("categories", {})),
                # 套餐按默认组成折算件数；单品恒为 1
                "compositionSize": max(1, len(composition)),
                # 硬性条件只认已确认组成的商品。
                # requiresUserSelection=true（需要用户在多选项里做选择）的商品一律排除：
                # 默认组成虽含汉堡，但用户完全可以选出不含汉堡的组合 —— 不得替用户假定。
                "canSatisfyBurgerConstraint": (bool(rec.get("containsBurgerByDefault"))
                                               and not bool(rec.get("requiresUserSelection"))),
                "burgerSwitchable": bool(rec.get("burgerSwitchable")),
                "requiresUserSelection": bool(rec.get("requiresUserSelection")),
            })
            continue

        cls = name_classify(name, atomic_rules)
        if cls == "exclude":
            continue
        pool.append({
            "code": code,
            "name": name,
            "nameFromMenu": name,
            "priceYuan": price,
            **price_fields,
            "verified": False,
            "confidence": "name_inferred_single",
            "source": "references/product-map.json#atomicNameRules",
            "kind": "atomic_name_inferred",
            "categories": {
                "burger": cls == "burger_name_inferred",
                "drink": cls == "drink",
                "side": cls == "side",
            },
            "compositionSize": 1,
            # 名称推断永远不得满足硬性条件
            "canSatisfyBurgerConstraint": False,
            "burgerSwitchable": False,
            "requiresUserSelection": False,
        })
    return pool


# --------------------------------------------------------------------------
# 组合搜索
# --------------------------------------------------------------------------

def summarise(items: list[dict]) -> dict:
    cats = {"burger": False, "drink": False, "side": False}
    for it in items:
        for k in cats:
            cats[k] = cats[k] or bool(it["categories"].get(k))
    return cats


def prefilter_sum(items: list[dict]) -> float | None:
    """把一组商品的**预筛价**相加。任一项预筛价未知 → 返回 None（表示无法做本地预算判断）。"""
    total = 0.0
    for it in items:
        v = it.get("prefilterPriceYuan")
        if v is None:
            return None
        total += v
    return round(total, 2)


def plan_metrics(items: list[dict], budget: float, prefer_drink: bool, prefer_side: bool) -> dict:
    cats = summarise(items)
    total = round(sum(i["priceYuan"] for i in items), 2)
    pre_total = prefilter_sum(items)
    remaining = round(budget - total, 2)
    pref_hits = (1 if (prefer_drink and cats["drink"]) else 0) + \
                (1 if (prefer_side and cats["side"]) else 0)
    pref_total = (1 if prefer_drink else 0) + (1 if prefer_side else 0)
    # 套餐按默认组成折算「有效件数」：大堡口福三件套算 3 件，不是一个
    effective_items = sum(int(i.get("compositionSize", 1)) for i in items)
    # 排序优先级（产品自定义规则，非客观营养结论）：
    #   ① 硬性条件（由候选池保证） ② 命中的可选偏好数 ③ 有效件数 ④ 预算利用率（花得更满更好）
    # 预算利用率以**预筛价**为准（条件价不能当无条件价用）；预筛价未知时退回菜单价仅用于排序。
    basis = pre_total if pre_total is not None else total
    spend_ratio = (basis / budget) if budget else 0.0
    return {
        "categories": cats,
        "categoryCount": sum(1 for v in cats.values() if v),
        "itemCount": len(items),
        "effectiveItemCount": effective_items,
        # 本版本每个 code 只允许出现一次，故 distinct == itemCount
        "distinctItemCount": len({i["code"] for i in items}),
        # menuTotalYuan 仍是**菜单展示价**合计：用于展示与「菜单价 ≠ 官方价」的差异对比，
        # 绝不作为预算可行性的依据。
        "menuTotalYuan": total,
        "prefilterTotalYuan": pre_total,
        "budgetCheckDeferred": pre_total is None,
        "prefilterExceedsBudget": (pre_total is not None and pre_total > budget + PRICE_TOLERANCE_YUAN),
        "remainingYuan": remaining,
        "preferenceHits": pref_hits,
        "preferenceTotal": pref_total,
        "score": pref_hits * 10000
                 + effective_items * 100
                 + int(max(0.0, min(1.0, spend_ratio)) * 99),
    }


def search_plans(pool: list[dict], budget: float, prefer_drink: bool, prefer_side: bool,
                 max_items: int = DEFAULT_MAX_ITEMS, require_drink: bool = False,
                 require_side: bool = False) -> list[dict]:
    """回溯式搜索：每个方案 = 1 个「已确认含汉堡」锚点 + 若干可选的饮料/小食。

    P1.14 新增 **`require_drink` / `require_side` 硬约束**。
    此前内核只有「必须含汉堡」一条硬约束，饮料/小食**只支持 `--prefer-*` 软偏好**
    （仅影响排序，不保证出现）。实际后果（本轮实测复现）：用户说
    「必须有汉堡和饮料」，预算 ¥25 时内核产出的候选 C6 =「麦香鸡 + 派」，
    `satisfies = ['burger','side']` —— **不含饮料**，硬约束被静默降级成软偏好。

    修复方式保持最小：在整个方案层面做**过滤**（而不是放宽），
    因此不会引入新的"看起来更丰富"的假方案，也**不改变原有汉堡锚点逻辑**。

    P1.9 起**预算剪枝改用 prefilterPriceYuan（预筛价）而不是菜单展示价**：
      - 条件价商品（随单购）的预筛价是原价，不再被当成「半价」选进来；
      - 预筛价为 None（无条件价无法确定）的商品**不做本地预算剪枝**，仍会进入候选，
        但会带上 budgetCheckDeferred 标记 —— 预算合规性一律以官方 calculate-price 为准。

    其它剪枝与约束：
      - 同一商品编码在方案中只出现一次（不靠重复同一商品制造「丰富」）；
      - 一个方案内最多 1 个饮料项（避免出现「两杯饮料」这种凑数组合）；
      - 方案件数上限 max_items。
    """
    tol = PRICE_TOLERANCE_YUAN

    def within(price) -> bool:
        """预筛价未知(None)时**不剪枝** —— 未知不等于超预算。"""
        return price is None or price <= budget + tol

    anchors = [i for i in pool
               if i["canSatisfyBurgerConstraint"] and within(i["prefilterPriceYuan"])]
    # 补足项只允许「原子单品」（饮料/小食）。
    # 刻意排除其它套餐/组合型商品，避免出现「套餐＋套餐」这种堆砌式子（那只是数字上更丰富）。
    fillers = [i for i in pool
               if i["kind"] == "atomic_name_inferred"
               and within(i["prefilterPriceYuan"])
               and (i["categories"].get("drink") or i["categories"].get("side"))]
    # 排序按预筛价（未知排最后），让便宜的先被枚举到
    fillers.sort(key=lambda x: (x["prefilterPriceYuan"] if x["prefilterPriceYuan"] is not None
                                else float("inf"), x["code"]))

    def _satisfies_hard(cats: dict) -> bool:
        """硬约束（可选）：用户明确说「必须有饮料/小食」时，整个方案必须满足。

        注意这是**过滤**语义：不满足就整个方案丢掉，绝不用偏好加权来"凑"。
        """
        return (not require_drink or cats["drink"]) and (not require_side or cats["side"])

    plans: list[list[dict]] = []
    for anchor in sorted(anchors, key=lambda x: (x["prefilterPriceYuan"]
                                                 if x["prefilterPriceYuan"] is not None
                                                 else float("inf"), x["code"])):
        # 单锚点方案同样要过硬约束（锚点自带饮料时才会被保留）
        if _satisfies_hard(summarise([anchor])):
            plans.append([anchor])
        a_pre = anchor["prefilterPriceYuan"]
        usable = []
        for f in fillers:
            f_pre = f["prefilterPriceYuan"]
            # 只有两边都已知时才能安全地按预算裁掉；未知的一律保留
            if a_pre is not None and f_pre is not None and a_pre + f_pre > budget + tol:
                continue
            usable.append(f)
        # 锚点若已含饮料，就不再追加饮料
        anchor_has_drink = bool(anchor["categories"].get("drink"))
        drink_pool = [] if anchor_has_drink else [f for f in usable if f["categories"].get("drink")]
        other_pool = [f for f in usable if not f["categories"].get("drink")]

        for extra_count in range(1, max_items):
            added = False
            for drink_take in range(0, min(1, extra_count) + 1):
                other_take = extra_count - drink_take
                if other_take > len(other_pool):
                    continue
                for dc in itertools.combinations(drink_pool, drink_take):
                    for oc in itertools.combinations(other_pool, other_take):
                        parts = [anchor, *dc, *oc]
                        # P1.14：硬约束在这里整体把关 —— 不满足就丢弃该组合，
                        # 不因为"件数多/评分高"而被保留下来当作可行方案。
                        if not _satisfies_hard(summarise(parts)):
                            continue
                        total = prefilter_sum(parts)
                        # 预筛价未知 → 不判断，交给官方核价
                        if total is not None and total > budget + tol:
                            continue
                        plans.append(parts)
                        added = True
            if not added:
                break
        if len(plans) > MAX_PLAN_GUARD:
            break
    return plans


def dedupe_plans(plans: list[list[dict]]) -> list[list[dict]]:
    """去掉完全相同的商品集合。"""
    seen: set[tuple[str, ...]] = set()
    out: list[list[dict]] = []
    for p in plans:
        key = tuple(sorted(i["code"] for i in p))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def select_candidates(plans: list[list[dict]], budget: float, prefer_drink: bool,
                      prefer_side: bool, max_candidates: int) -> list[dict]:
    """从全部可行组合里挑出少量、彼此有区分度的候选，供后续官方核价。

    区分度策略：以「锚点商品」为分组维度轮转取样。同一锚点下只保留
    分类数/有效件数/偏好命中不同的方案，避免核价队列里塞满近乎相同的组合。

    P1.10 起把候选分成两组，**可信组优先占满席位**：
      credible —— 预筛价可判定（prefilterPriceYuan 非 None）
      deferred —— 预筛价无法判定（None，如未验证的 discountType 或缺原价的条件价）

    原因（2026-10-16 实测）：未定候选的**菜单价不可信**，若与可信候选同池竞争，
    它会用"看起来便宜"的菜单价赢下 cheapest / best 席位，把真正可行的候选挤掉。
    复现：把 9900005456 的 discountType 改成未验证取值 → 预算 ¥35 时候选里出现
    菜单价 ¥54.5（已超预算）的未定候选，并挤掉了原本可行的候选；
    预算 ¥10 时 6 个席位被未定候选全部占满，连 noSolutionAdvice 也随之失效。
    未定组只在**剩余席位**里补齐，且仍带 budgetCheckDeferred 标记，绝不当成确定价。
    """
    scored = []
    for p in plans:
        m = plan_metrics(p, budget, prefer_drink, prefer_side)
        scored.append({"items": p, "metrics": m, "anchorCode": p[0]["code"]})
    if not scored:
        return []

    selected: list[dict] = []
    seen: set[tuple] = set()

    def _basis(entry: dict) -> float:
        """排序/比较用的价格基准：优先预筛价（条件价已修正），未知才退回菜单价。"""
        pre = entry["metrics"]["prefilterTotalYuan"]
        return pre if pre is not None else entry["metrics"]["menuTotalYuan"]

    def pick(entry: dict | None) -> bool:
        if entry is None or len(selected) >= max_candidates:
            return False
        m = entry["metrics"]
        sig = (entry["anchorCode"], m["categoryCount"], m["effectiveItemCount"], m["preferenceHits"])
        if sig in seen:
            return False
        seen.add(sig)
        selected.append(entry)
        return True

    def run_group(group: list[dict]) -> None:
        """在给定分组内跑一遍「三类代表 + 锚点轮转 + 兜底」的选择流程。"""
        if not group or len(selected) >= max_candidates:
            return
        group.sort(key=lambda x: (-x["metrics"]["score"], _basis(x), x["anchorCode"]))

        cheapest = min(group, key=lambda x: (_basis(x), -x["metrics"]["score"]))
        richest = max(group, key=lambda x: (x["metrics"]["effectiveItemCount"],
                                           x["metrics"]["categoryCount"],
                                           x["metrics"]["preferenceHits"],
                                           -_basis(x)))
        best = group[0]

        by_anchor: dict[str, list[dict]] = {}
        for entry in group:
            by_anchor.setdefault(entry["anchorCode"], []).append(entry)
        group_order = sorted(by_anchor,
                             key=lambda a: -max(e["metrics"]["score"] for e in by_anchor[a]))

        # 三类代表先占位：最优、最丰富、最省钱
        for entry in (best, richest, cheapest):
            pick(entry)

        # 再按锚点轮转补充，保证方案之间商品不同
        cursor = {a: 0 for a in group_order}
        progress = True
        while len(selected) < max_candidates and progress:
            progress = False
            for a in group_order:
                if len(selected) >= max_candidates:
                    break
                entries = by_anchor[a]
                while cursor[a] < len(entries):
                    candidate = entries[cursor[a]]
                    cursor[a] += 1
                    if pick(candidate):
                        progress = True
                        break

        # 兜底：组内仍不足则放宽去重。**绝不跨组** —— 未定价候选不得借兜底挤掉可信候选。
        for entry in group:
            if len(selected) >= max_candidates:
                break
            if entry not in selected:
                selected.append(entry)

    credible = [e for e in scored if not e["metrics"]["budgetCheckDeferred"]]
    deferred = [e for e in scored if e["metrics"]["budgetCheckDeferred"]]
    run_group(credible)
    run_group(deferred)

    for idx, entry in enumerate(selected, start=1):
        entry["id"] = f"C{idx}"
    assign_styles(selected)
    return selected


def assign_styles(selected: list[dict]) -> None:
    """给候选打上「产品自定义」的风格标签。这是排序规则，不是客观营养结论。"""
    if not selected:
        return
    min_price = min(e["metrics"]["menuTotalYuan"] for e in selected)
    max_eff = max(e["metrics"]["effectiveItemCount"] for e in selected)
    max_cats = max(e["metrics"]["categoryCount"] for e in selected)
    for e in selected:
        m = e["metrics"]
        if m["categoryCount"] == max_cats and m["categoryCount"] >= 3 and m["effectiveItemCount"] == max_eff:
            e["styleHint"] = "多品类且丰富"
        elif m["categoryCount"] == max_cats and m["categoryCount"] >= 3:
            e["styleHint"] = "多品类"
        elif m["menuTotalYuan"] == min_price:
            e["styleHint"] = "省钱"
        elif m["effectiveItemCount"] == max_eff:
            e["styleHint"] = "丰富"
        else:
            e["styleHint"] = "均衡"


# --------------------------------------------------------------------------
# 金额单位约定（写死在代码里，避免单位混乱）
# --------------------------------------------------------------------------
#   - 官方接口返回的金额字段（price / originalPrice / discount / originalSubtotal /
#     subtotal / productPrice / enjoyable.balance）**一律以「分」为单位**，内部保存为 int。
#   - query-meals 菜单里的 currentPrice 是**元**；转成分请用 to_cents()。
#   - 展示层统一用 from_cents() 还原为元。
#   - 旧的 priceYuan 字段只为兼容既有组合搜索逻辑而保留，**不参与**任何券金额运算。
#   - 券的「实际节省」= 同一组合的无券官方价 − 带券官方价（同为单位分后相减），
#     任何时候都不得用券面金额代替。

def to_cents(value) -> int | None:
    """元 -> 分（四舍五入到整数分）。无法解析返回 None。"""
    yuan = to_yuan(value)
    if yuan is None:
        return None
    return int(round(yuan * CENTS_PER_YUAN))


def from_cents(cents) -> float | None:
    """分 -> 元（保留两位小数）。非数值返回 None。"""
    if isinstance(cents, bool) or not isinstance(cents, (int, float)):
        return None
    return round(float(cents) / CENTS_PER_YUAN, 2)


# --------------------------------------------------------------------------
# 优惠券：来源 / 查询状态 / 有效期 / 适用性置信状态
# --------------------------------------------------------------------------

COUPON_SOURCE_WALLET = "wallet"        # 用户卡包（query-my-coupons）
COUPON_SOURCE_STORE = "store"          # 当前门店当前渠道可用（query-store-coupons）
COUPON_SOURCE_UNCLAIMED = "unclaimed"  # 已上架、尚未领取（available-coupons）

QUERY_OK = "ok"
QUERY_EMPTY = "empty"
QUERY_ERROR = "error"
QUERY_NOT_QUERIED = "not_queried"

VALIDITY_VALID = "valid"
VALIDITY_EXPIRED = "expired"
VALIDITY_NOT_YET = "not_yet"
VALIDITY_UNKNOWN = "validity_unknown"

# 适用性置信状态。含义见 references/coupon-schema.md。
APPLICABILITY_CODE_MATCHED = "code_matched"      # 券的适用商品编码与候选有交集（仍不等于「确定可省」）
APPLICABILITY_UNKNOWN = "unknown"                # 官方未给出适用商品编码 —— 严禁当成「全品适用」
APPLICABILITY_NOT_APPLICABLE = "not_applicable"  # 编码无交集，或时间上不可用
APPLICABILITY_UNVERIFIABLE = "unverifiable"      # 交给官方试算但失败，无法证明其效果
APPLICABILITY_UNASSESSABLE = "unassessable"      # 无 couponId/couponCode，压根无法送进官方试算

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                 "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y.%m.%d")


def parse_time(text):
    """尽量宽松地解析官方返回里的时间字符串；解析不出返回 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _naive(dt):
    """把可能带时区的时间压成 naive，便于与 now 比较。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def coupon_validity(coupon: dict, now: datetime) -> str:
    """判定券在 now 时点的时间可用性。信息不足时返回 validity_unknown（不猜）。"""
    labels = [str(x) for x in (coupon.get("labels") or [])]
    if any(("未到使用时间" in x) or ("未开始" in x) for x in labels):
        return VALIDITY_NOT_YET
    start = _naive(parse_time(coupon.get("tradeStartDate")))
    end = _naive(parse_time(coupon.get("tradeEndDate")))
    if start is None and end is None:
        return VALIDITY_UNKNOWN
    if end is not None and now > end:
        return VALIDITY_EXPIRED
    if start is not None and now < start:
        return VALIDITY_NOT_YET
    return VALIDITY_VALID


def _coupon_title(item: dict) -> str:
    for key in ("title", "couponName", "name", "activityName"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return "（无名称）"


def _coupon_applicable_codes(item: dict):
    """从 products[].productCode 提取适用商品编码。返回 (codes, known)。"""
    products = item.get("products")
    if not isinstance(products, list) or not products:
        return [], False
    codes = []
    for prod in products:
        if isinstance(prod, dict):
            code = prod.get("productCode")
            if code is not None and str(code).strip():
                codes.append(str(code).strip())
    if not codes:
        return [], False
    return sorted(set(codes)), True


def _as_coupon_list(node):
    """把各种可能的返回外形统一成 list[dict]。"""
    if isinstance(node, list):
        return [x for x in node if isinstance(x, dict)]
    if isinstance(node, dict):
        for key in ("data", "coupons", "list", "items", "couponList"):
            val = node.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


def _json_from_text(text):
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return largest_json_object(stripped)


def _mentions_none(text) -> bool:
    if not isinstance(text, str):
        return False
    return ("暂无" in text) or ("没有可用" in text) or ("无可领取" in text)


def parse_store_coupons(raw):
    """解析 query-store-coupons 返回 -> (coupons, explicit_empty)。

    同一 couponId 可对应多个券码，这里按 couponId 归并成一组。
    """
    node = _json_from_text(raw)
    items = _as_coupon_list(node)
    if not items:
        return [], _mentions_none(raw) or (isinstance(node, dict) and node.get("data") == [])
    grouped: dict[str, dict] = {}
    for item in items:
        cid = str(item.get("couponId") or "").strip()
        code = item.get("couponCode")
        codes = []
        if isinstance(code, str) and code.strip():
            codes.append(code.strip())
        elif isinstance(code, list):
            codes.extend(str(c).strip() for c in code if str(c).strip())
        title = _coupon_title(item)
        key = cid or f"__no_id__::{title}"
        rec = grouped.get(key)
        if rec is None:
            applicable, known = _coupon_applicable_codes(item)
            rec = {
                "couponId": cid or None,
                "couponCodes": [],
                "title": title,
                "tradeStartDate": item.get("tradeStartDate") or item.get("beginTime"),
                "tradeEndDate": item.get("tradeDateTime") or item.get("tradeEndDate") or item.get("endTime"),
                "labels": _labels_of(item),
                "applicableCodes": applicable,
                "applicabilityCodesKnown": known,
                "raw": item,
            }
            grouped[key] = rec
        for c in codes:
            if c not in rec["couponCodes"]:
                rec["couponCodes"].append(c)
    return list(grouped.values()), False


def _labels_of(item: dict):
    labels = []
    instructions = item.get("instructions")
    if isinstance(instructions, dict):
        for lb in (instructions.get("labels") or []):
            if isinstance(lb, dict):
                text = lb.get("text") or lb.get("label") or lb.get("title")
                if isinstance(text, str) and text.strip():
                    labels.append(text.strip())
            elif isinstance(lb, str) and lb.strip():
                labels.append(lb.strip())
    direct = item.get("labels")
    if isinstance(direct, list):
        for lb in direct:
            if isinstance(lb, dict):
                text = lb.get("text") or lb.get("label")
                if isinstance(text, str) and text.strip():
                    labels.append(text.strip())
            elif isinstance(lb, str) and lb.strip():
                labels.append(lb.strip())
    return labels


def parse_wallet_coupons(raw):
    """解析 query-my-coupons 返回 -> (coupons, explicit_empty)。结构未知，按容错方式处理。"""
    node = _json_from_text(raw)
    items = _as_coupon_list(node)
    if not items:
        return [], _mentions_none(raw) or (isinstance(node, dict) and node.get("data") in ([], None))
    out = []
    for item in items:
        cid = item.get("couponId") or item.get("id")
        code = item.get("couponCode") or item.get("code")
        codes = []
        if isinstance(code, str) and code.strip():
            codes.append(code.strip())
        elif isinstance(code, list):
            codes.extend(str(c).strip() for c in code if str(c).strip())
        applicable, known = _coupon_applicable_codes(item)
        out.append({
            "couponId": str(cid).strip() if cid is not None and str(cid).strip() else None,
            "couponCodes": codes,
            "title": _coupon_title(item),
            "tradeStartDate": item.get("tradeStartDate") or item.get("beginTime"),
            "tradeEndDate": item.get("tradeEndDate") or item.get("tradeDateTime") or item.get("endTime"),
            "labels": _labels_of(item),
            "applicableCodes": applicable,
            "applicabilityCodesKnown": known,
            "raw": item,
        })
    return out, False


def parse_available_coupons(raw):
    """解析 available-coupons 返回（markdown 文本）-> (coupons, explicit_empty)。

    注意：该接口只给券名/图片/状态，**不含 couponId**，
    因此这里产出的券只能是「未领取」候选，适用性恒为 unassessable。
    """
    if not isinstance(raw, str):
        node = _json_from_text(raw)
        items = _as_coupon_list(node)
        if items:
            out = []
            for item in items:
                out.append({
                    "couponId": None,
                    "couponCodes": [],
                    "title": _coupon_title(item),
                    "tradeStartDate": None,
                    "tradeEndDate": None,
                    "labels": [],
                    "applicableCodes": [],
                    "applicabilityCodesKnown": False,
                    "raw": item,
                })
            return out, False
        return [], _mentions_none(raw)
    if _mentions_none(raw):
        return [], True
    out = []
    titles = []
    for line in raw.splitlines():
        text = line.strip().lstrip("-").strip()
        for prefix in ("优惠券标题：", "优惠券标题:"):
            if text.startswith(prefix):
                titles.append(text[len(prefix):].strip())
    seen = set()
    for title in titles:
        if not title or title in seen:
            continue
        seen.add(title)
        out.append({
            "couponId": None,
            "couponCodes": [],
            "title": title,
            "tradeStartDate": None,
            "tradeEndDate": None,
            "labels": [],
            "applicableCodes": [],
            "applicabilityCodesKnown": False,
            "raw": None,
        })
    return out, False


def load_coupon_input(path) -> dict:
    """读取券商盘点输入。

    格式（由 Agent 写入，显式携带查询状态，避免把「查询失败」当成「没有券」）：
      {"wallet":{"status":"ok|empty|error|not_queried","raw":...},
       "store":{...}, "available":{...}}
    """
    doc = load_json(path)
    out = {}
    for key in ("wallet", "store", "available"):
        node = doc.get(key)
        if isinstance(node, dict) and "status" in node:
            out[key] = {"status": str(node.get("status")), "raw": node.get("raw")}
        elif node is None:
            out[key] = {"status": QUERY_NOT_QUERIED, "raw": None}
        else:
            out[key] = {"status": QUERY_OK, "raw": node}
    return out


def normalize_coupons(doc: dict, now: datetime) -> dict:
    """把三个来源的返回归一化成统一结构。

    结构上的三分（不是靠字段打标签）：
      - coupons[]   ：**已领取**的券（来源为 wallet 或 store）
      - walletOnly[]：已领取但不在当前门店可用清单里的券
      - storeUsable[]：官方按「当前门店 + 当前渠道」判定可用的券
      - unclaimed[] ：**未领取**的券，只来自 available-coupons，恒为 unassessable，
                      且**不会**混进 coupons[]。
    """
    claimed_sources = (
        ("wallet", COUPON_SOURCE_WALLET, parse_wallet_coupons),
        ("store", COUPON_SOURCE_STORE, parse_store_coupons),
    )
    coupons, warnings, query_state, unclaimed = [], [], {}, []

    for key, source, parser in claimed_sources:
        block = doc.get(key) or {}
        status = str(block.get("status") or QUERY_NOT_QUERIED)
        query_state[key] = status
        raw = block.get("raw")
        if status == QUERY_NOT_QUERIED:
            warnings.append(f"{key}：本轮未查询，不能据此判断用户有无优惠券。")
            continue
        if status == QUERY_ERROR:
            warnings.append(f"{key}：查询失败。**不得**把查询失败解释为「没有优惠券」。")
            continue
        items, explicit_empty = parser(raw)
        if not items:
            if explicit_empty or status == QUERY_EMPTY:
                continue
            warnings.append(f"{key}：状态为 {status} 但未能从返回中解析出任何券 —— "
                            f"这是解析失败，不等于「没有券」。")
            continue
        for item in items:
            rec = dict(item)
            rec["source"] = source
            rec["validity"] = coupon_validity(rec, now)
            rec["probeKey"] = rec.get("couponId") or None
            rec["sourceQueryStatus"] = status
            coupons.append(rec)

    block = doc.get("available") or {}
    status = str(block.get("status") or QUERY_NOT_QUERIED)
    query_state["available"] = status
    if status == QUERY_NOT_QUERIED:
        warnings.append("available：本轮未查询，不能据此判断用户有无可领取的券。")
    elif status == QUERY_ERROR:
        warnings.append("available：查询失败。**不得**把查询失败解释为「没有可领取的券」。")
    else:
        items, explicit_empty = parse_available_coupons(block.get("raw"))
        if not items and not (explicit_empty or status == QUERY_EMPTY):
            warnings.append(f"available：状态为 {status} 但未能从返回中解析出任何券 —— "
                            f"这是解析失败，不等于「没有可领取的券」。")
        else:
            for item in items:
                rec = dict(item)
                rec["source"] = COUPON_SOURCE_UNCLAIMED
                rec["validity"] = coupon_validity(rec, now)
                rec["probeKey"] = None
                rec["sourceQueryStatus"] = status
                rec["applicability"] = APPLICABILITY_UNASSESSABLE
                unclaimed.append(rec)

    store_ids = {c["couponId"] for c in coupons
                 if c["source"] == COUPON_SOURCE_STORE and c["couponId"]}
    wallet_only = [c for c in coupons
                   if c["source"] == COUPON_SOURCE_WALLET and c["couponId"] not in store_ids]

    return {
        "coupons": coupons,
        "queryState": query_state,
        "warnings": warnings,
        "storeUsable": [c for c in coupons if c["source"] == COUPON_SOURCE_STORE],
        "walletOnly": wallet_only,
        "unclaimed": unclaimed,
    }


def match_coupon_to_codes(coupon: dict, candidate_codes: set) -> str:
    """判定某张券对某个候选组合的适用性状态（不调用任何远端接口）。"""
    if coupon.get("validity") in (VALIDITY_EXPIRED, VALIDITY_NOT_YET):
        return APPLICABILITY_NOT_APPLICABLE
    if not coupon.get("probeKey"):
        return APPLICABILITY_UNASSESSABLE
    if not coupon.get("applicabilityCodesKnown"):
        return APPLICABILITY_UNKNOWN
    if set(coupon.get("applicableCodes") or []) & set(candidate_codes):
        return APPLICABILITY_CODE_MATCHED
    return APPLICABILITY_NOT_APPLICABLE


def load_normalized_coupons(path) -> dict:
    """读取 `coupons` 子命令产出的归一化券文件。缺失字段按最保守方式补齐。"""
    doc = load_json(path)
    coupons = doc.get("coupons")
    if not isinstance(coupons, list):
        fail(f"{path} 中找不到 coupons 列表，请先用 `plan.py coupons` 生成归一化结果。")
    for c in coupons:
        c.setdefault("probeKey", c.get("couponId") or None)
        c.setdefault("validity", VALIDITY_UNKNOWN)
        c.setdefault("applicableCodes", [])
        c.setdefault("applicabilityCodesKnown", bool(c.get("applicableCodes")))
        c.setdefault("title", "（无名称）")
    return doc


def attach_coupon_matches(candidates: list, coupons: list, probe_unknown: bool = False,
                          max_probes_per_candidate: int = 3) -> dict:
    """给每个候选附上券匹配结果，并按探针预算剪枝。返回统计摘要。"""
    summary = {"candidates": len(candidates), "codeMatched": 0, "unknown": 0,
               "notApplicable": 0, "unassessable": 0, "unusableByTime": 0,
               "plannedProbes": 0, "prunedProbes": 0}
    for cand in candidates:
        codes = {i["code"] for i in cand.get("items", [])}
        matches = []
        probe_able = []
        for coupon in coupons:
            status = match_coupon_to_codes(coupon, codes)
            validity = coupon.get("validity")
            if validity in (VALIDITY_EXPIRED, VALIDITY_NOT_YET):
                summary["unusableByTime"] += 1
                continue
            if status == APPLICABILITY_CODE_MATCHED:
                summary["codeMatched"] += 1
            elif status == APPLICABILITY_UNKNOWN:
                summary["unknown"] += 1
            elif status == APPLICABILITY_NOT_APPLICABLE:
                summary["notApplicable"] += 1
            elif status == APPLICABILITY_UNASSESSABLE:
                summary["unassessable"] += 1
            overlaps = sorted(set(coupon.get("applicableCodes") or []) & codes)
            matches.append({
                "couponId": coupon.get("couponId"),
                "title": coupon.get("title"),
                "source": coupon.get("source"),
                "status": status,
                "validity": validity,
                "matchedCodes": overlaps,
            })
            if status == APPLICABILITY_CODE_MATCHED:
                probe_able.append((-len(overlaps), str(coupon.get("couponId") or ""), coupon))
            elif status == APPLICABILITY_UNKNOWN and probe_unknown:
                probe_able.append((0, str(coupon.get("couponId") or ""), coupon))
        probe_able.sort(key=lambda x: (x[0], x[1]))
        planned = [c for _, _, c in probe_able[:max_probes_per_candidate]]
        summary["plannedProbes"] += len(planned)
        summary["prunedProbes"] += max(0, len(probe_able) - len(planned))
        planned_keys = {c.get("couponId") for c in planned}
        for m in matches:
            m["probePlanned"] = m["status"] == APPLICABILITY_CODE_MATCHED and m["couponId"] in planned_keys
            if m["status"] == APPLICABILITY_UNKNOWN and probe_unknown:
                m["probePlanned"] = m["couponId"] in planned_keys
        cand["couponMatches"] = matches
        cand["probePlan"] = {
            "baseline": True,
            "coupons": [c.get("couponId") for c in planned],
            "totalProbes": 1 + len(planned),
            "prunedCount": max(0, len(probe_able) - len(planned)),
        }
    return summary


def interpret_enjoyment(node: dict) -> dict:
    """区分「已享受优惠」与「可享受门槛差额」。

    `enjoyable.balance` 是「还差多少才够门槛」，**不是**优惠金额，严禁与 discount 混用。
    """
    enjoyed = node.get("enjoyed") if isinstance(node.get("enjoyed"), dict) else None
    enjoyable = node.get("enjoyable") if isinstance(node.get("enjoyable"), dict) else None
    discount = node.get("discount")
    return {
        "discountCents": discount if isinstance(discount, (int, float)) else None,
        "enjoyed": enjoyed,
        "enjoyable": enjoyable,
        "enjoyableBalanceCents": (enjoyable or {}).get("balance"),
        "note": "enjoyed=已享受的优惠；enjoyable.balance=距门槛还差的金额。二者不得混用。",
    }


def coverage_stats(menu: dict, product_map: dict) -> dict:
    """覆盖度统计：映射表覆盖了多少菜单商品；未覆盖里有多少「疑似含汉堡」。

    恒等式：coveredCount + uncoveredCount + excludedCount == menuItemCount
    （excludedCount = 被 atomicNameRules 判为 exclude 的调味酱/周边等，不参与组合）
    """
    verified_products = product_map.get("verifiedProducts", {})
    verified = set(verified_products)
    retired = {c for c, rec in verified_products.items() if rec.get("retired")}
    atomic_rules = product_map.get("atomicNameRules", {})
    covered, uncovered_burger_like, uncovered_other = [], [], []
    excluded = 0
    for code, info in menu.items():
        if code in verified:
            covered.append(code)
            continue
        cls = name_classify(info["name"], atomic_rules)
        if cls == "exclude":
            excluded += 1
            continue
        if cls == "burger_name_inferred":
            uncovered_burger_like.append({"code": code, "name": info["name"],
                                          "menuPriceYuan": info["priceYuan"]})
        else:
            uncovered_other.append({"code": code, "name": info["name"]})
    missing_from_menu = sorted(c for c in verified if c not in menu)
    return {
        "menuItemCount": len(menu),
        "verifiedProductCount": len(verified),
        "coveredCount": len(covered),
        "uncoveredCount": len(uncovered_burger_like) + len(uncovered_other),
        "excludedCount": excluded,
        "uncoveredBurgerLike": sorted(uncovered_burger_like, key=lambda x: x["code"]),
        "uncoveredOtherCount": len(uncovered_other),
        "verifiedNotInMenu": missing_from_menu,
        # 菜单里找不到、但又**没有**标 retired 的条目 —— 正常必须为空。
        # 非空说明映射表里存在「既不在售、又没说明失效原因」的僵尸条目。
        "verifiedNotInMenuUnmarked": sorted(
            c for c in missing_from_menu if not verified_products[c].get("retired")
        ),
        # 已确认失效/已轮换的旧编码。它们已被 build_pool 强制排除；
        # retiredInMenu 正常应为空 —— 若非空，说明该编码又被菜单重新启用，需要重新核实。
        "retiredProductCount": len(retired),
        "retiredInMenu": sorted(c for c in retired if c in menu),
        # P1.9：价格语义分布（菜单现价里有条件价的商品数量）
        "conditionalPriceCount": sum(1 for i in menu.values()
                                     if i.get("priceStatus") == PRICE_STATUS_CONDITIONAL),
        "priceStatusCounts": _count_price_status(menu),
    }


def _count_price_status(menu: dict) -> dict:
    counts: dict[str, int] = {}
    for info in menu.values():
        st = str(info.get("priceStatus"))
        counts[st] = counts.get(st, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def cmd_candidates(args) -> None:
    menu_raw = load_json(args.menu)
    product_map = load_json(args.map)
    menu, price_report = extract_menu_with_report(menu_raw)

    budget = float(args.budget)
    if budget <= 0:
        fail("预算必须大于 0。")

    pool = build_pool(menu, product_map)
    if not pool:
        fail("商品池为空，请检查菜单与映射表。")

    # 用 getattr 取默认值：老调用方（含测试里的简化 args 桩）不含这两个参数时，
    # 行为与修复前**完全一致**（不做任何额外过滤）。
    require_drink = bool(getattr(args, "require_drink", False))
    require_side = bool(getattr(args, "require_side", False))
    plans = dedupe_plans(search_plans(pool, budget, args.prefer_drink, args.prefer_side,
                                      args.max_items, require_drink, require_side))
    selected = select_candidates(plans, budget, args.prefer_drink, args.prefer_side,
                                 args.max_candidates)

    burger_pool = [i for i in pool if i["canSatisfyBurgerConstraint"]]

    def _basis(item) -> float:
        """用于「最低门槛」说明的价格：优先用预筛价，未知才退回菜单价。"""
        return item["prefilterPriceYuan"] if item["prefilterPriceYuan"] is not None \
            else item["priceYuan"]

    cheapest_burger = min(burger_pool, key=_basis) if burger_pool else None

    # 条件价商品的处理必须**显式说明**，不得静默丢弃
    conditional_in_pool = [i for i in pool if i["priceStatus"] == PRICE_STATUS_CONDITIONAL]
    excluded_conditional, deferred_conditional = [], []
    for i in conditional_in_pool:
        pre = i["prefilterPriceYuan"]
        row = {
            "code": i["code"], "name": i["name"],
            "menuPriceYuan": i["priceYuan"], "prefilterPriceYuan": pre,
            "originalPriceYuan": i.get("originalPriceYuan"),
            "discountType": i.get("discountType"),
        }
        if pre is None:
            deferred_conditional.append({
                **row, "reason": "条件价且无可用原价 → 不做本地预算判断，交官方核价。"})
        elif pre > budget + PRICE_TOLERANCE_YUAN:
            excluded_conditional.append({
                **row,
                "reason": (f"菜单现价 {i['priceYuan']} 是条件价（{i.get('discountType')}）；"
                           f"无条件价按原价 {pre} 计入预筛 → 超出预算 {round(budget, 2)}，"
                           f"本地预筛已排除。**未**按菜单现价当作预算可行。")})

    unknown_price_items = [i for i in pool if i["budgetCheckDeferred"]
                           and i["priceStatus"] != PRICE_STATUS_CONDITIONAL]

    # P1.10：候选分层的实情必须如实输出 —— 未定价候选不得混进"可信推荐"里当默认结果。
    credible_candidates = [e for e in selected if not e["metrics"]["budgetCheckDeferred"]]
    deferred_candidates = [e for e in selected if e["metrics"]["budgetCheckDeferred"]]
    price_undetermined_notice = None
    if deferred_candidates and not credible_candidates:
        price_undetermined_notice = {
            "reason": ("本次**没有任何可在本地判定价格的候选**：下列候选含价格语义未经实测的商品"
                       "（discountType 未验证或缺少原价），本地无法先行判断是否超预算。"),
            "candidates": [{"id": e["id"], "menuTotalYuan": e["metrics"]["menuTotalYuan"],
                            "items": [i["name"] for i in e["items"]]}
                           for e in deferred_candidates],
            "cheapestConfirmedBurger": (
                {"code": cheapest_burger["code"], "name": cheapest_burger["name"],
                 "menuPriceYuan": cheapest_burger["priceYuan"],
                 "prefilterPriceYuan": cheapest_burger["prefilterPriceYuan"]}
                if cheapest_burger else None
            ),
            "note": ("这**不等于无解** —— 只是本地无法先行判断。候选仍会送官方 calculate-price，"
                     "核价后若超预算会如实判为 over_budget，不得当作可行方案输出。"),
        }

    out = {
        "meta": {
            "storeCode": args.store_code,
            "channel": {"orderType": 1, "beType": 1, "note": "到店自取，不传 beCode"},
            "budgetYuan": round(budget, 2),
            "hardConstraints": {"mustContainBurger": True,
                                "mustContainDrink": require_drink,
                                "mustContainSide": require_side},
            "optionalPreferences": {"preferDrink": bool(args.prefer_drink),
                                    "preferSide": bool(args.prefer_side)},
            "menuItemCount": len(menu),
            "poolItemCount": len(pool),
            "feasiblePlanCount": len(plans),
            "priceStatusCounts": price_report.get("priceStatusCounts", {}),
            "note": ("以下价格均为菜单预估（未核价）。必须经 calculate-price 核价后才能作为推荐结果。"
                     "预算预筛用的是 prefilterPriceYuan（条件价商品已改用原价）；"
                     "预筛价为 null 的商品不做本地预算判断，一律交官方核价。"),
            "menuPriceAnomalies": price_report.get("dropped", []),
            "conditionalPriceNotice": {
                "menuPriceIsConditionalCount": len(conditional_in_pool),
                "excludedByPrefilter": excluded_conditional,
                "deferredToOfficialQuote": deferred_conditional + [
                    {"code": i["code"], "name": i["name"], "menuPriceYuan": i["priceYuan"],
                     "discountType": i.get("discountType"),
                     "reason": "无法证明无条件价 → 不做本地预算判断，交官方核价。"}
                    for i in unknown_price_items],
                "note": ("菜单里部分商品的 currentPrice 是**条件价**（如「随单购麦金卡优惠」），"
                         "并非无条件成交价。本 Skill 一律以 prefilterPriceYuan 做预算预筛，"
                         "最终价格只能来自官方 calculate-price。"),
            },
            "candidateComposition": {
                "credibleCount": len(credible_candidates),
                "priceDeferredCount": len(deferred_candidates),
                "note": ("credible = 预筛价可判定（含条件价已改用原价者）；"
                         "priceDeferred = 预筛价无法判定，其预算合规性只能由官方 calculate-price 决定。"
                         "候选选择**优先占满可信席位**，未定价候选只在剩余席位补齐 —— "
                         "避免不可信的菜单价把真正可行的候选挤掉。"),
            },
            "priceUndeterminedNotice": price_undetermined_notice,
        },
        "candidates": [
            {
                "id": e["id"],
                "styleHint": e.get("styleHint"),
                "items": [
                    {"code": i["code"], "name": i["name"], "qty": 1,
                     "menuUnitPriceYuan": i["priceYuan"],
                     "prefilterUnitPriceYuan": i["prefilterPriceYuan"],
                     "originalUnitPriceYuan": i.get("originalPriceYuan"),
                     "priceStatus": i.get("priceStatus"),
                     "discountType": i.get("discountType"),
                     "confidence": i["confidence"]}
                    for i in e["items"]
                ],
                "menuTotalYuan": e["metrics"]["menuTotalYuan"],
                "prefilterTotalYuan": e["metrics"]["prefilterTotalYuan"],
                "budgetCheckDeferred": e["metrics"]["budgetCheckDeferred"],
                "remainingYuan": e["metrics"]["remainingYuan"],
                "effectiveItemCount": e["metrics"]["effectiveItemCount"],
                "satisfies": e["metrics"]["categories"],
                "preferenceHits": e["metrics"]["preferenceHits"],
                "score": e["metrics"]["score"],
            }
            for e in selected
        ],
        "noSolutionAdvice": None if selected else {
            "reason": "在当前门店、当前渠道、且已确认组成的商品集合中，没有找到满足约束的方案。",
            "cheapestConfirmedBurger": (
                {"code": cheapest_burger["code"], "name": cheapest_burger["name"],
                 "menuPriceYuan": cheapest_burger["priceYuan"],
                 "prefilterPriceYuan": cheapest_burger["prefilterPriceYuan"]}
                if cheapest_burger else None
            ),
            "shortfallYuan": (round(_basis(cheapest_burger) - budget, 2)
                              if cheapest_burger and _basis(cheapest_burger) > budget else None),
            "disclaimer": "部分商品组成尚未核实，这并不代表整个菜单已被证明无解。",
        },
    }

    coupons_path = getattr(args, "coupons", None)
    if coupons_path:
        probe_unknown = bool(getattr(args, "probe_unknown", False))
        max_per = int(getattr(args, "max_coupons_per_candidate", 3))
        norm = load_normalized_coupons(coupons_path)
        summary = attach_coupon_matches(out["candidates"], norm.get("coupons", []),
                                        probe_unknown, max_per)
        out["meta"]["couponContext"] = {
            **summary,
            "couponSourcePath": str(coupons_path),
            "probeUnknownEnabled": probe_unknown,
            "maxCouponsPerCandidate": max_per,
            "note": ("每个候选的探针数 = 1（无券基线）+ 计划试算的券数。"
                     "适用商品编码缺失的券标 unknown，默认不试算，且**不得**当作全品适用；"
                     "券价仍然只能以 calculate-price 为准。"),
        }

    dump_json(out, args.out)


def _parse_quote(raw) -> dict | None:
    """把一条 calculate-price 返回标准化。

    返回 None 表示「压根没有这条核价记录」（quote_missing）。
    返回 dict 表示「确实调用过并拿到了返回」——里面可能 success=False，
    那种情况属于核价失败（quote_failed），必须与 quote_missing 区分开。
    """
    if raw is None:
        return None
    node = raw
    if isinstance(raw, str):
        try:
            node = json.loads(raw)
        except json.JSONDecodeError:
            node = largest_json_object(raw)
    if not isinstance(node, dict):
        return None

    outer_success = node.get("success")
    outer_code = node.get("code")
    outer_message = node.get("message")
    inner = node.get("data") if isinstance(node.get("data"), dict) else None

    if inner is None:
        # 失败响应（如 {"success":false,"code":60006,"message":...}）没有 data。
        # 只要带 success / code，就认定「这是一次真实调用返回」而不是缺失。
        if outer_success is None and outer_code is None and "price" not in node:
            return None
        inner = dict(node)

    result = dict(inner)
    result["success"] = outer_success if outer_success is not None else result.get("success", True)
    if outer_code is not None:
        result.setdefault("code", outer_code)
    if outer_message is not None:
        result.setdefault("message", outer_message)
    return result


def cmd_verify(args) -> None:
    cand_doc = load_json(args.candidates)
    quotes_doc = load_json(args.quotes)

    quotes = quotes_doc.get("quotes") if isinstance(quotes_doc.get("quotes"), dict) else quotes_doc
    candidates = cand_doc.get("candidates", [])
    budget = float(args.budget)

    verified, rejected, discrepancies = [], [], []

    for cand in candidates:
        cid = cand["id"]
        node = _parse_quote(quotes.get(cid))
        if node is None:
            rejected.append({"id": cid, "status": "quote_missing",
                             "reason": "没有找到该候选对应的 calculate-price 返回，不得视为已核价。"})
            continue
        if node.get("success") is False:
            rejected.append({"id": cid, "status": "quote_failed",
                             "reason": f"calculate-price 返回失败（code={node.get('code')}, message={node.get('message')}）。该方案不得作为已核价方案输出。"})
            continue
        price_cents = node.get("price")
        if not isinstance(price_cents, (int, float)):
            rejected.append({"id": cid, "status": "quote_failed",
                             "reason": "calculate-price 返回中缺少 price 字段。"})
            continue

        official = round(price_cents / CENTS_PER_YUAN, 2)
        menu_total = cand.get("menuTotalYuan")
        match = (menu_total is not None and abs(official - menu_total) <= 0.005)

        if not match:
            discrepancies.append({
                "id": cid,
                "menuTotalYuan": menu_total,
                "officialPriceYuan": official,
                "deltaYuan": (round(official - menu_total, 2) if menu_total is not None else None),
                "note": "菜单价与官方核价不一致。以官方核价为准，并如实报告差异，不得自行修正。",
            })

        entry = {
            "id": cid,
            "styleHint": cand.get("styleHint"),
            "items": cand.get("items", []),
            "menuTotalYuan": menu_total,
            "officialPriceYuan": official,
            "officialPriceSource": "calculate-price",
            # P1.10：把官方的 originalPrice / discount 也如实带出。
            # 理由：SKILL.md 的输出模板要求「原价 / 优惠金额 / 应付金额 / 实际节省」四件套，
            # 而 verify 是无券路径 —— 之前只有 price，模板里的"原价/优惠"两个格子无从填写，
            # 逼着生成层去猜。官方返回里本来就有这两个字段，直接照抄即可，缺失时如实给 None。
            "originalPriceYuan": (round(node["originalPrice"] / CENTS_PER_YUAN, 2)
                                  if isinstance(node.get("originalPrice"), (int, float)) else None),
            "discountYuan": (round(node["discount"] / CENTS_PER_YUAN, 2)
                             if isinstance(node.get("discount"), (int, float)) else None),
            # 本子命令是**无券路径**：即使 discount > 0，也绝不能说成"用了优惠券"。
            "usedCoupon": False,
            "discountSource": ("calculate-price 的 discount 字段；可能来自**店内促销自动生效**，"
                               "**不代表使用了优惠券**（本路径未传任何 couponId/couponCode）。"),
            "remainingYuan": round(budget - official, 2),
            "withinBudget": official <= budget + 0.005,
            "priceMatchesMenu": match,
            "satisfies": cand.get("satisfies", {}),
        }

        if entry["withinBudget"]:
            verified.append(entry)
        else:
            rejected.append({"id": cid, "status": "over_budget",
                             "reason": f"官方核价 {fmt_yuan(official)} 超出预算 {fmt_yuan(budget)}。",
                             "officialPriceYuan": official})

    out = {
        "meta": {
            "budgetYuan": round(budget, 2),
            "verifiedCount": len(verified),
            "rejectedCount": len(rejected),
            "rule": "只有同时满足「核价成功」「不超过预算」的候选，才可标记为已验证可行。",
            "discountNote": ("discountYuan 直接来自官方 calculate-price 的 discount 字段。它可能来自"
                             "**店内促销自动生效**，**不等于使用了优惠券**；本子命令是无券路径，"
                             "usedCoupon 恒为 false。originalPriceYuan / discountYuan 缺失时为 null —— "
                             "表示该次官方返回未包含该字段，**不得据菜单价推算填补**。"),
            # P1.11：把排序规则从"隐式"变"显式"。
            # 实测发现：预算 ¥35 时 verified 的第一条是 ¥35.00（品类最全）而非 ¥14.00（最省），
            # 这本身符合「尽量搭配」的诉求，但规则若不写出来，生成层会误以为它按价格排序。
            "rankingRule": ("verified 默认按「丰富度优先」排列：品类覆盖数降序 → 有效件数降序 → "
                            "剩余预算降序。⚠️ 这**不是价格排序** —— 第一条不一定是最便宜的。"
                            "用户要「最省」时，应按 officialPriceYuan 升序自行取最小；"
                            "需要「原价/优惠/应付/实际节省」四件套与价格升序时用 compare 子命令。"),
        },
        "verified": sorted(verified, key=lambda x: (-(
            (1 if x["satisfies"].get("drink") else 0) + (1 if x["satisfies"].get("side") else 0)
        ), -len(x["items"]), -x["remainingYuan"])),
        "rejected": rejected,
        "discrepancies": discrepancies,
    }
    dump_json(out, args.out)


def _resolve_now(text):
    dt = parse_time(text) if text else None
    return _naive(dt) or datetime.now()


def _coupon_public_view(coupon: dict) -> dict:
    view = {k: v for k, v in coupon.items() if k != "raw"}
    raw = coupon.get("raw")
    view["rawKeys"] = sorted(raw.keys()) if isinstance(raw, dict) else None
    return view


def cmd_coupons(args) -> None:
    """归一化三个来源的券，并显式区分「卡包券 / 当前门店可用券 / 未领取券」。"""
    doc = load_coupon_input(args.input)
    now = _resolve_now(getattr(args, "now", None))
    norm = normalize_coupons(doc, now)
    coupons = norm["coupons"]
    out = {
        "meta": {
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "queryState": norm["queryState"],
            "walletCouponCount": sum(1 for c in coupons if c["source"] == COUPON_SOURCE_WALLET),
            "storeUsableCount": len(norm["storeUsable"]),
            "walletOnlyCount": len(norm["walletOnly"]),
            "unclaimedCount": len(norm["unclaimed"]),
            "note": ("store = 官方按「当前门店+当前渠道」返回的可用券（唯一可据以判断本店可用的来源）；"
                     "wallet = 用户卡包，官方明确说明**不校验门店与渠道**；"
                     "unclaimed = available-coupons 的券名，**不含 couponId**，适用性恒为 unassessable，"
                     "不得直接当作可用于当前订单的优惠券。"),
        },
        "coupons": [_coupon_public_view(c) for c in coupons],
        "storeUsable": [_coupon_public_view(c) for c in norm["storeUsable"]],
        "walletOnly": [_coupon_public_view(c) for c in norm["walletOnly"]],
        "unclaimed": [
            {"title": c["title"], "couponId": None, "probeKey": None,
             "validity": c.get("validity"), "applicability": APPLICABILITY_UNASSESSABLE,
             "reason": ("available-coupons 未返回 couponId / 优惠金额 / 门槛 / 适用商品，"
                        "无法判断其能否降低本次订单金额；**不得**直接当作可用于当前订单的优惠券。")}
            for c in norm["unclaimed"]
        ],
        "warnings": norm["warnings"],
    }
    dump_json(out, args.out)


def _quote_price_cents(node) -> int | None:
    if node is None or node.get("success") is False:
        return None
    cents = node.get("price")
    if isinstance(cents, bool) or not isinstance(cents, (int, float)):
        return None
    return int(cents)


def _quote_field_cents(node, field) -> int | None:
    if node is None:
        return None
    val = node.get(field)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return int(val)


def cmd_compare(args) -> None:
    """对同一组合比较「不带券」与「带券」的官方核价，实际节省只能取两者之差。"""
    cand_doc = load_json(args.candidates)
    quotes_doc = load_json(args.quotes)
    quotes = quotes_doc.get("quotes") if isinstance(quotes_doc.get("quotes"), dict) else quotes_doc
    candidates = cand_doc.get("candidates", [])
    budget = float(args.budget)
    budget_cents = to_cents(budget)
    max_probes = int(getattr(args, "max_probes", 20))

    verified, rejected, unverifiable, discrepancies = [], [], [], []
    probe_count = 0

    for cand in candidates:
        cid = cand["id"]
        raw_entry = quotes.get(cid)
        if isinstance(raw_entry, dict):
            option_map = dict(raw_entry)
        elif raw_entry is None:
            option_map = {}
        else:
            option_map = {"none": raw_entry}

        base_node = _parse_quote(option_map.get("none"))
        base_cents = _quote_price_cents(base_node)

        options = []
        for key, raw in option_map.items():
            if key == "none":
                continue
            probe_count += 1
            node = _parse_quote(raw)
            cents = _quote_price_cents(node)
            status = "verified" if cents is not None else (
                "quote_failed" if node is not None else "quote_missing")
            option = {
                "couponKey": key,
                "status": status,
                "originalPriceCents": _quote_field_cents(node, "originalPrice"),
                "discountCents": _quote_field_cents(node, "discount"),
                "priceCents": cents,
                "originalPriceYuan": from_cents(_quote_field_cents(node, "originalPrice")),
                "discountYuan": from_cents(_quote_field_cents(node, "discount")),
                "priceYuan": from_cents(cents),
                "savingCents": None,
                "savingYuan": None,
                "savingSource": None,
            }
            if cents is not None and base_cents is not None:
                option["savingCents"] = base_cents - cents
                option["savingYuan"] = from_cents(base_cents - cents)
                option["savingSource"] = "baseline_price_minus_coupon_price"
            elif cents is not None and base_cents is None:
                option["savingSource"] = "baseline_unavailable"
            else:
                option["status"] = APPLICABILITY_UNVERIFIABLE
            options.append(option)
            if option["status"] != "verified":
                unverifiable.append({"id": cid, **{k: v for k, v in option.items()},
                                     "reason": "带券核价未成功，无法证明该券的实际效果；不得声称省了钱。"})

        if base_cents is None:
            rejected.append({
                "id": cid,
                "status": "baseline_unavailable",
                "reason": "没有可用的「不带券」核价基线，无法计算实际节省，因此不参与比较。",
            })
            continue
        probe_count += 1

        baseline = {
            "couponKey": "none", "status": "verified",
            "originalPriceCents": _quote_field_cents(base_node, "originalPrice"),
            "discountCents": _quote_field_cents(base_node, "discount"),
            "priceCents": base_cents,
            "originalPriceYuan": from_cents(_quote_field_cents(base_node, "originalPrice")),
            "discountYuan": from_cents(_quote_field_cents(base_node, "discount")),
            "priceYuan": from_cents(base_cents),
            "savingCents": 0, "savingYuan": 0.0, "savingSource": "baseline",
        }
        usable = [o for o in options if o["status"] == "verified"]
        pool = usable + [baseline]
        best = min(pool, key=lambda o: (o["priceCents"], 0 if o["couponKey"] == "none" else 1))

        menu_total = cand.get("menuTotalYuan")
        if menu_total is not None and abs(from_cents(base_cents) - menu_total) > 0.005:
            discrepancies.append({
                "id": cid, "menuTotalYuan": menu_total,
                "officialBaselineYuan": from_cents(base_cents),
                "deltaYuan": round(from_cents(base_cents) - menu_total, 2),
                "note": "菜单价与无券官方核价不一致。以官方核价为准，并如实报告差异。",
            })

        entry = {
            "id": cid,
            "styleHint": cand.get("styleHint"),
            "items": cand.get("items", []),
            "satisfies": cand.get("satisfies", {}),
            "preferenceHits": cand.get("preferenceHits"),
            "menuTotalYuan": menu_total,
            "chosenCouponKey": best["couponKey"],
            "usedCoupon": best["couponKey"] != "none",
            "originalPriceCents": best["originalPriceCents"],
            "discountCents": best["discountCents"],
            "priceCents": best["priceCents"],
            "savingCents": best["savingCents"],
            "originalPriceYuan": best["originalPriceYuan"],
            "discountYuan": best["discountYuan"],
            "priceYuan": best["priceYuan"],
            "savingYuan": best["savingYuan"],
            "savingSource": best["savingSource"],
            "priceSource": "calculate-price",
            "allOptions": pool,
        }
        if best["priceCents"] > budget_cents:
            rejected.append({
                "id": cid, "status": "over_budget",
                "officialPriceYuan": best["priceYuan"],
                "reason": f"官方核价 {fmt_yuan(best['priceYuan'])} 超出预算 {fmt_yuan(budget)}。",
            })
            continue
        entry["withinBudget"] = True
        entry["remainingYuan"] = round(budget - best["priceYuan"], 2)
        verified.append(entry)

    verified.sort(key=lambda x: (x["priceCents"], -(x.get("preferenceHits") or 0),
                                 1 if x["usedCoupon"] else 0))

    out = {
        "meta": {
            "budgetYuan": round(budget, 2),
            "verifiedCount": len(verified),
            "rejectedCount": len(rejected),
            "unverifiableCount": len(unverifiable),
            "probeCount": probe_count,
            "probeBudget": max_probes,
            "probeBudgetExceeded": probe_count > max_probes,
            "unitConvention": ("所有 *Cents 字段单位为分，*Yuan 字段单位为元，二者同源；"
                               "savingCents = 同组合无券官方价 − 带券官方价，**不是**券面金额。"),
            "rule": ("只有「无券基线核价成功」且「带券核价成功」的选项才可比较；"
                     "带券核价失败一律标 unverifiable，不得计入已省金额。"),
            "scopeNote": "结果仅为当前已搜索并核价范围内的最低价，不等同于全局最优。",
            "rankingRule": ("verified 按**官方应付价升序**排列（同价时：偏好命中多者优先 → "
                            "不用券者优先）。与 verify 子命令的「丰富度优先」不同，"
                            "这里的排序基准就是价格。"),
        },
        "verified": verified,
        "rejected": rejected,
        "unverifiableOptions": unverifiable,
        "discrepancies": discrepancies,
    }
    dump_json(out, args.out)


def cmd_coverage(args) -> None:
    """只读的映射表覆盖度体检：不修改映射表，只报告缺口与待核实清单。"""
    menu = extract_menu(load_json(args.menu))
    product_map = load_json(args.map)
    stats = coverage_stats(menu, product_map)
    out = {
        "meta": {
            "menuItemCount": stats["menuItemCount"],
            "verifiedProductCount": stats["verifiedProductCount"],
            "coveredCount": stats["coveredCount"],
            "coverageRatio": (round(stats["coveredCount"] / stats["menuItemCount"], 4)
                              if stats["menuItemCount"] else None),
            "uncoveredCount": stats["uncoveredCount"],
            "excludedCount": stats["excludedCount"],
            "retiredProductCount": stats["retiredProductCount"],
            "conditionalPriceCount": stats.get("conditionalPriceCount"),
            "priceStatusCounts": stats.get("priceStatusCounts"),
            "identity": "coveredCount + uncoveredCount + excludedCount == menuItemCount",
            "note": ("未覆盖商品一律视为「组成未知」，不得用于满足硬性条件。"
                     "已标记 retired 的旧编码被 build_pool 强制排除，绝不进入推荐。"
                     "conditionalPriceCount 是菜单现价为**条件价**的商品数（预筛已改用原价）。"
                     "本命令只报告缺口，**不会**修改映射表。"),
        },
        "priorityToVerify": stats["uncoveredBurgerLike"],
        "priorityCount": len(stats["uncoveredBurgerLike"]),
        "uncoveredOtherCount": stats["uncoveredOtherCount"],
        "verifiedNotInMenu": stats["verifiedNotInMenu"],
        "verifiedNotInMenuUnmarked": stats["verifiedNotInMenuUnmarked"],
        "verifiedNotInMenuNote": ("映射表里有、当前菜单里找不到的条目。正常应**全部**是已标 retired 的旧编码；"
                                  "verifiedNotInMenuUnmarked 必须为空 —— 非空说明存在未说明原因的僵尸条目。"),
        "retiredInMenu": stats["retiredInMenu"],
        "retiredInMenuNote": ("正常应为空数组。非空说明已确认失效的编码又出现在菜单中，"
                              "必须重新核实其当前状态，不得直接沿用旧的 retired 结论。"),
        "howToVerify": ("对每个 code 调用 query-meal-detail(storeCode, orderType=1, beType=1, code)，"
                        "依据 data.rounds 的默认子项判断是否含汉堡；有真实返回后才可写回映射表并更新 evidence.at。"
                        "注意菜单随时段切换，若返回 60001「未匹配到商品」说明该商品当前时段不在售，"
                        "应换到处于常规时段的门店或稍后重试，不要据此判定商品不存在。"),
    }
    dump_json(out, args.out)


def cmd_selftest(args) -> None:
    here = Path(__file__).resolve().parent
    root = here.parent
    menu = load_json(root / "tests/fixtures/menu_plan_fixture.json")
    product_map = load_json(root / "references/product-map.json")
    pool = build_pool(extract_menu(menu), product_map)

    checks = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        checks.append((label, bool(cond), detail))

    check("菜单能解析出商品", len(pool) > 0, f"商品池 {len(pool)} 条")

    # 回归断言：「酱」只按后缀排除，不能误伤「蘸酱XX」类真实餐品
    for code, name in [("9900016077", "蘸酱炸鸡"), ("9900016078", "蘸酱麦麦脆汁鸡")]:
        item = next((i for i in pool if i["code"] == code), None)
        check(f"{name} 未被「酱」规则误排除", item is not None and item["categories"]["side"])
    for code, name in [("521797", "北美招牌牧场酱"), ("521799", "日式鲜磨山葵酱")]:
        item = next((i for i in pool if i["code"] == code), None)
        check(f"{name} 被正确排除", item is None)

    verified_burgers = [i for i in pool if i["canSatisfyBurgerConstraint"]]
    check("存在可满足硬性条件的已确认汉堡商品", len(verified_burgers) > 0,
          f"{len(verified_burgers)} 条：" + ", ".join(f"{i['code']}/{i['name']}" for i in verified_burgers[:5]))

    # 关键安全性断言：名称推断的商品一律不得满足硬性条件
    inferred_ok = all(not i["canSatisfyBurgerConstraint"] for i in pool if not i["verified"])
    check("名称推断商品不会满足硬性条件", inferred_ok)

    # 已知反例必须被正确排除
    for code, name in [("9900011126", "酥脆无双套餐"), ("9900011123", "“辣”么快乐套餐"),
                       ("9900004835", "麦乐鸡套餐"), ("9900015568", "精选超值随心配")]:
        item = next((i for i in pool if i["code"] == code), None)
        check(f"反例 {name} 未被当作含汉堡", item is not None and not item["canSatisfyBurgerConstraint"])

    # 真实存在的含汉堡套餐必须可用（用**当前在售**编码；旧编码 9900015160 / 9900015183 /
    # 9900015187 已在 2026-10-16 确认轮换并从商品池强制排除，见 product-map.json 的 codeRotation）
    for code, name in [("9900015602", "大堡口福三件套"), ("9900004064", "精选单人餐三件套"),
                       ("9900013304", "人气经典随心配")]:
        item = next((i for i in pool if i["code"] == code), None)
        check(f"{name} 可用于满足硬性条件", item is not None and item["canSatisfyBurgerConstraint"])

    # 已失效/已轮换的旧编码必须彻底不出现在商品池里
    retired_skus = {c for c, rec in product_map.get("verifiedProducts", {}).items()
                    if rec.get("retired")}
    retired_in_pool = [i["code"] for i in pool if i["code"] in retired_skus]
    menu_codes = set(extract_menu(menu))
    check("映射表标注了已失效/已轮换的编码", len(retired_skus) > 0, f"{len(retired_skus)} 个")
    check("已失效/已轮换的旧编码不进入商品池", not retired_in_pool, str(retired_in_pool))
    check("菜单快照里已无这些失效编码（快照已刷新）",
          not (retired_skus & menu_codes), str(sorted(retired_skus & menu_codes)))

    # 预算约束
    plans = dedupe_plans(search_plans(pool, 25.0, True, True))
    over = [p for p in plans if sum(i["priceYuan"] for i in p) > 25.0 + 0.005]
    check("25 元搜索不产生超预算组合", not over, f"共 {len(plans)} 个可行组合")
    check("25 元能搜到可行组合", len(plans) > 0)

    dup = [p for p in plans if len({i["code"] for i in p}) != len(p)]
    check("组合内不重复同一商品编码", not dup)

    # 无解场景
    plans10 = dedupe_plans(search_plans(pool, 10.0, False, False))
    check("预算 10 元时无可行组合（预期无解）", len(plans10) == 0, f"实际 {len(plans10)} 个")

    # 核价解析：必须区分「核价失败」和「压根没核价」
    q_ok = _parse_quote({"success": True, "data": {"price": 2390}})
    q_fail = _parse_quote({"success": False, "code": 60006,
                           "message": "商品已售罄或不可售，换一个商品或检查orderType/beType"})
    q_none = _parse_quote(None)
    check("正常核价返回被正确解析", q_ok is not None and q_ok.get("price") == 2390)
    check("失败返回被识别为真实调用（不是缺失）",
          q_fail is not None and q_fail.get("success") is False and q_fail.get("code") == 60006,
          "回归：曾把失败响应误判为 quote_missing")
    check("没有核价记录时返回 None", q_none is None)

    # --- P1.9 条件价预筛（字段语义只认已验证取值，其余保守）---
    sem_cond = resolve_price_semantics({"currentPrice": "28", "originalPrice": "58",
                                        "discountType": "随单购麦金卡优惠"})
    check("条件价商品：预筛改用原价",
          sem_cond["prefilterPriceYuan"] == 58.0
          and sem_cond["priceStatus"] == PRICE_STATUS_CONDITIONAL,
          str(sem_cond["prefilterPriceYuan"]))
    check("条件价商品：菜单现价仍被保留（供差异对比）", sem_cond["menuPriceYuan"] == 28.0)
    sem_plain = resolve_price_semantics({"currentPrice": "34", "originalPrice": "47.5"})
    check("无 discountType：预筛行为与修复前一致（仍用菜单现价）",
          sem_plain["prefilterPriceYuan"] == 34.0
          and sem_plain["priceStatus"] == PRICE_STATUS_MENU)
    sem_unknown = resolve_price_semantics({"currentPrice": "28", "originalPrice": "58",
                                          "discountType": "麦金卡优惠"})
    check("未实测的 discountType：不得套用原价，也不得当成无条件价",
          sem_unknown["prefilterPriceYuan"] is None
          and sem_unknown["priceStatus"] == PRICE_STATUS_UNKNOWN)
    sem_missing = resolve_price_semantics({"currentPrice": "28",
                                           "discountType": "随单购麦金卡优惠"})
    check("条件价但缺 originalPrice：保守（不做本地预算判断）",
          sem_missing["prefilterPriceYuan"] is None)
    plans25 = dedupe_plans(search_plans(pool, 25.0, True, True))
    over = [p for p in plans25
            if (lambda t: t is not None and t > 25.0 + PRICE_TOLERANCE_YUAN)(prefilter_sum(p))]
    check("25 元：没有任何方案的预筛价合计超预算", not over)

    failed = [c for c in checks if not c[1]]
    for label, ok, detail in checks:
        print(f"{'[通过]' if ok else '[失败]'} {label}" + (f"  ({detail})" if detail else ""))
    print(f"\n自检结果：{len(checks) - len(failed)}/{len(checks)} 通过")
    if failed:
        raise SystemExit(1)


# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="plan.py",
        description="麦麦预算点餐师 本地内核：组合搜索与核价校验（离线、无副作用）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("candidates", help="从实时菜单搜索候选组合")
    p1.add_argument("--menu", required=True, help="query-meals 的真实返回（或精简 items 形式）")
    p1.add_argument("--map", required=True, help="references/product-map.json")
    p1.add_argument("--budget", required=True, type=float, help="预算，单位元")
    p1.add_argument("--prefer-drink", action="store_true", help="偏好饮料")
    p1.add_argument("--prefer-side", action="store_true", help="偏好小食")
    p1.add_argument("--require-drink", action="store_true",
                    help="**硬约束**：方案必须含饮料（区分于 --prefer-drink 软偏好）")
    p1.add_argument("--require-side", action="store_true",
                    help="**硬约束**：方案必须含小食（区分于 --prefer-side 软偏好）")
    p1.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS, help="单方案最多件数")
    p1.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES,
                    help="送去官方核价的候选数量上限")
    p1.add_argument("--store-code", default=None, help="仅用于在输出里标注门店")
    p1.add_argument("--coupons", default=None,
                    help="（可选）plan.py coupons 产出的归一化券文件；给出后为每个候选附券匹配")
    p1.add_argument("--probe-unknown", action="store_true",
                    help="（默认关闭）允许对「适用商品编码缺失」的券也安排试算")
    p1.add_argument("--max-coupons-per-candidate", type=int, default=3,
                    help="单个候选最多安排试算几张券（探针剪枝）")
    p1.add_argument("--out", default=None, help="输出 JSON 路径（缺省则打印到 stdout）")
    p1.set_defaults(func=cmd_candidates)

    p2 = sub.add_parser("verify", help="用 calculate-price 真实返回校验候选")
    p2.add_argument("--candidates", required=True)
    p2.add_argument("--quotes", required=True, help='形如 {"C1": <calculate-price 返回>, ...}')
    p2.add_argument("--budget", required=True, type=float)
    p2.add_argument("--out", default=None)
    p2.set_defaults(func=cmd_verify)

    p3 = sub.add_parser("selftest", help="内置最小自检")
    p3.set_defaults(func=cmd_selftest)

    p4 = sub.add_parser("coupons", help="归一化优惠券（只读，不领券、不下单）")
    p4.add_argument("--input", required=True,
                    help='形如 {"wallet":{"status":"ok|empty|error|not_queried","raw":...},"store":{...},"available":{...}}')
    p4.add_argument("--now", default=None, help="判定有效期用的当前时间（缺省取本机时间）")
    p4.add_argument("--out", default=None)
    p4.set_defaults(func=cmd_coupons)

    p5 = sub.add_parser("compare", help="比较同组合的「无券/带券」官方核价，算实际节省")
    p5.add_argument("--candidates", required=True)
    p5.add_argument("--quotes", required=True,
                    help='形如 {"quotes": {"C1": {"none": <无券返回>, "<couponId>": <带券返回>}}}')
    p5.add_argument("--budget", required=True, type=float)
    p5.add_argument("--max-probes", type=int, default=20, help="探针预算上限（超出则声明未穷举）")
    p5.add_argument("--out", default=None)
    p5.set_defaults(func=cmd_compare)

    p6 = sub.add_parser("coverage", help="映射表覆盖度体检（只读，不修改映射表）")
    p6.add_argument("--menu", required=True, help="query-meals 的真实返回")
    p6.add_argument("--map", required=True, help="references/product-map.json")
    p6.add_argument("--out", default=None)
    p6.set_defaults(func=cmd_coverage)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
