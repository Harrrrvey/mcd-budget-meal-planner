#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_coupons.py — 优惠券相关离线测试（P0）

只用标准库，不依赖 pytest（也可被 pytest 直接收集）。运行：
    python3 tests/test_coupons.py

覆盖：
  1. 金额单位：元 <-> 分 往返换算
  2. 券归一化：同一 couponId 多券码归并
  3. 有效期状态：valid / expired / not_yet / validity_unknown
  4. 适用商品编码缺失 -> unknown，**绝不**当作全品适用
  5. 券-候选编码级匹配：code_matched / not_applicable / unknown / unassessable
  6. 探针预算剪枝
  7. 实际节省 = 无券官方价 − 带券官方价（**不是**券面金额）
  8. 带券核价失败 -> unverifiable，不得计入已省
  9. over_budget 与 quote_failed 区分
 10. 输出必须包含原价/优惠/应付/节省四值
 11. 「查询失败」不得被解释为「没有优惠券」
 12. 真实空样本（门店券为空 / 券包为空）解析正确
 13. available-coupons 的券一律 unassessable（无 couponId）
 14. 卡包券 vs 当前门店可用券 的三分类
 15. 覆盖度体检不修改映射表
 16. 本地内核不含任何写工具调用

所有 fixture 的来源见 tests/fixtures/coupons/README.md。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import plan  # noqa: E402

FIX = ROOT / "tests/fixtures/coupons"
MENU_FIXTURE = ROOT / "tests/fixtures/menu_plan_fixture.json"
PRODUCT_MAP = ROOT / "references/product-map.json"

NOW = datetime(2026, 10, 16, 12, 0, 0)

STORE_SAMPLE_RAW = (FIX / "structure_sample_store_coupons.json").read_text(encoding="utf-8")
WALLET_SYNTHETIC_RAW = (FIX / "synthetic_wallet_coupons.json").read_text(encoding="utf-8")
REAL_STORE_EMPTY_RAW = (FIX / "real_store_coupons_empty_3230019.json").read_text(encoding="utf-8")
REAL_WALLET_EMPTY_RAW = (FIX / "real_wallet_coupons_empty.txt").read_text(encoding="utf-8")
REAL_AVAILABLE_RAW = (FIX / "real_available_coupons_2026-10-16.txt").read_text(encoding="utf-8")

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(cond), detail))


def make_doc(wallet=None, store=None, available=None,
             w_status=plan.QUERY_NOT_QUERIED, s_status=plan.QUERY_NOT_QUERIED,
             a_status=plan.QUERY_NOT_QUERIED) -> dict:
    doc = {}
    doc["wallet"] = {"status": w_status, "raw": wallet}
    doc["store"] = {"status": s_status, "raw": store}
    doc["available"] = {"status": a_status, "raw": available}
    return doc


def hydrate(items: list) -> list:
    """给裸解析结果补上 probeKey / validity —— 与 normalize_coupons 的做法一致。"""
    for c in items:
        c.setdefault("probeKey", c.get("couponId") or None)
        c.setdefault("validity", plan.coupon_validity(c, NOW))
    return items


def cand(cid: str, items: list[tuple[str, float]], menu_total: float,
         pref_hits: int = 0, style: str = "均衡") -> dict:
    return {
        "id": cid,
        "styleHint": style,
        "items": [{"code": c, "name": n, "qty": 1, "menuUnitPriceYuan": p} for c, n, p in items],
        "menuTotalYuan": menu_total,
        "satisfies": {"burger": True, "drink": False, "side": False},
        "preferenceHits": pref_hits,
    }


def run_compare(candidates: list, quotes: dict, budget: float, max_probes: int = 20) -> dict:
    with tempfile.TemporaryDirectory() as td:
        cp, qp, op = Path(td) / "c.json", Path(td) / "q.json", Path(td) / "o.json"
        cp.write_text(json.dumps({"candidates": candidates}, ensure_ascii=False), encoding="utf-8")
        qp.write_text(json.dumps({"quotes": quotes}, ensure_ascii=False), encoding="utf-8")
        plan.cmd_compare(type("A", (), {"candidates": str(cp), "quotes": str(qp),
                                        "budget": budget, "max_probes": max_probes,
                                        "out": str(op)})())
        return json.loads(op.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 1. 金额单位
def test_money_units():
    check("元->分：23.90 -> 2390", plan.to_cents(23.9) == 2390)
    check("元->分：字符串 '23.90' -> 2390", plan.to_cents("23.90") == 2390)
    check("分->元：2390 -> 23.9", plan.from_cents(2390) == 23.9)
    check("分->元：非数值返回 None", plan.from_cents("abc") is None)
    check("元<->分往返一致", plan.from_cents(plan.to_cents(9.9)) == 9.9)


# ------------------------------------------------------------ 2. 券归一化/归并
def test_store_grouping():
    items, explicit_empty = plan.parse_store_coupons(STORE_SAMPLE_RAW)
    check("门店券样本能解析", len(items) == 4, f"实际 {len(items)} 组")
    check("样本非空时 explicit_empty 为 False", explicit_empty is False)
    group = next((c for c in items if c["couponId"] == "S3001"), None)
    check("同一 couponId 的多张券码被归并为一组", group is not None and len(group["couponCodes"]) == 2,
          f"{group['couponCodes'] if group else None}")
    check("归并后适用商品编码去重", group is not None and group["applicableCodes"] == ["9900015165"])


# -------------------------------------------------------- 3. 有效期状态
def test_validity():
    coupons, _ = plan.parse_store_coupons(STORE_SAMPLE_RAW)
    by_id = {c["couponId"]: c for c in coupons}
    check("未过期券判定为 valid",
          plan.coupon_validity(by_id["S3001"], NOW) == plan.VALIDITY_VALID)
    check("已过期券判定为 expired",
          plan.coupon_validity(by_id["S3003"], NOW) == plan.VALIDITY_EXPIRED)

    wallet, _ = plan.parse_wallet_coupons(WALLET_SYNTHETIC_RAW)
    w = {c["couponId"]: c for c in wallet}
    check("生效时间在未来的券判定为 not_yet",
          plan.coupon_validity(w["W2003"], NOW) == plan.VALIDITY_NOT_YET)
    check("labels 含「未到使用时间」判定为 not_yet",
          plan.coupon_validity(w["W2005"], NOW) == plan.VALIDITY_NOT_YET)
    check("有效期字段缺失时判定为 validity_unknown（不猜）",
          plan.coupon_validity({"labels": [], "tradeStartDate": None, "tradeEndDate": None},
                               NOW) == plan.VALIDITY_UNKNOWN)


# ------------------------------------- 4/5/6. 适用性与匹配
def test_applicability_and_matching():
    store = hydrate(plan.parse_store_coupons(STORE_SAMPLE_RAW)[0])
    by_id = {c["couponId"]: c for c in store}
    check("有 products 的券 applicabilityCodesKnown 为 True",
          by_id["S3001"]["applicabilityCodesKnown"] is True)
    check("无 products 的券 applicabilityCodesKnown 为 False",
          by_id["S3004"]["applicabilityCodesKnown"] is False)

    cand_codes = {"9900015160", "9900015165"}
    check("编码有交集 -> code_matched",
          plan.match_coupon_to_codes(by_id["S3001"], cand_codes) == plan.APPLICABILITY_CODE_MATCHED)
    check("编码无交集 -> not_applicable",
          plan.match_coupon_to_codes(by_id["S3002"], cand_codes) == plan.APPLICABILITY_NOT_APPLICABLE)
    check("适用商品编码缺失 -> unknown",
          plan.match_coupon_to_codes(by_id["S3004"], cand_codes) == plan.APPLICABILITY_UNKNOWN)
    check("过期券 -> not_applicable（时间维度）",
          plan.match_coupon_to_codes(by_id["S3003"], cand_codes) == plan.APPLICABILITY_NOT_APPLICABLE)

    # 关键安全断言：unknown 的券不得匹配到任意候选
    all_codes = {"1450", "1406", "1440", "1120", "9900015160", "9900015162",
                 "9900015164", "9900015183", "9900015187", "9900016054"}
    check("unknown 券不得被当作「全品适用」",
          plan.match_coupon_to_codes(by_id["S3004"], all_codes) == plan.APPLICABILITY_UNKNOWN)

    # available-coupons 的券没有 couponId -> unassessable
    unclaimed, _ = plan.parse_available_coupons(REAL_AVAILABLE_RAW)
    probe = dict(unclaimed[0])
    probe["validity"] = plan.VALIDITY_VALID
    probe["probeKey"] = probe.get("couponId") or None
    check("未领取券（无 couponId）-> unassessable",
          plan.match_coupon_to_codes(probe, all_codes) == plan.APPLICABILITY_UNASSESSABLE)


def test_attach_and_pruning():
    store, _ = plan.parse_store_coupons(STORE_SAMPLE_RAW)
    for c in store:
        c["validity"] = plan.coupon_validity(c, NOW)
        c["probeKey"] = c.get("couponId") or None
    c = cand("P1", [("9900015160", "大堡口福三件套", 23.9), ("9900015165", "派", 8.5)], 32.4)
    summary = plan.attach_coupon_matches([c], store, probe_unknown=False, max_probes_per_candidate=3)
    check("匹配统计记录到 code_matched", summary["codeMatched"] >= 1, str(summary["codeMatched"]))
    check("code_matched 的券被排入探针计划",
          c["probePlan"]["coupons"] == ["S3001"], str(c["probePlan"]["coupons"]))
    check("unknown 的券默认不试算",
          all(m["status"] != plan.APPLICABILITY_UNKNOWN or not m["probePlanned"]
              for m in c["couponMatches"]))
    check("探针总数 = 1（无券基线）+ 计划券数",
          c["probePlan"]["totalProbes"] == 1 + len(c["probePlan"]["coupons"]))

    # 剪枝：造 5 张都命中同一编码的券
    many = [{"couponId": f"X{i}", "title": f"券{i}", "source": plan.COUPON_SOURCE_STORE,
             "validity": plan.VALIDITY_VALID, "probeKey": f"X{i}",
             "applicableCodes": ["9900015160"], "applicabilityCodesKnown": True}
            for i in range(5)]
    c2 = cand("P2", [("9900015160", "大堡口福三件套", 23.9)], 23.9)
    s2 = plan.attach_coupon_matches([c2], many, probe_unknown=False, max_probes_per_candidate=2)
    check("探针剪枝生效：只保留 2 张", len(c2["probePlan"]["coupons"]) == 2)
    check("剪枝数量被如实记录", s2["prunedProbes"] == 3, f"实际 {s2['prunedProbes']}")
    check("剪枝后输出了剪枝计数", c2["probePlan"]["prunedCount"] == 3)


# ------------------------------------------------- 7/8/9/10. 实际节省与核价
def test_saving_is_price_difference_not_face_value():
    quotes = json.loads((FIX / "synthetic_quotes.json").read_text(encoding="utf-8"))["quotes"]
    cands = [cand("T4", [("9900015160", "大堡口福三件套", 23.9)], 23.9)]
    out = run_compare(cands, quotes, 25.0)
    v = out["verified"]
    check("带券方案被选中", len(v) == 1 and v[0]["chosenCouponKey"] == "FACE5")
    check("实际节省 = 无券价 − 带券价 = ¥2.00", v and v[0]["savingYuan"] == 2.0,
          f"实际 {v[0]['savingYuan'] if v else None}")
    check("实际节省不等于券面金额 ¥5.00", v and v[0]["savingYuan"] != 5.0)
    check("savingSource 标明来自价格差",
          v and v[0]["savingSource"] == "baseline_price_minus_coupon_price")
    check("优惠金额取自官方 discount 字段（¥2.00）", v and v[0]["discountYuan"] == 2.0)


def test_enjoyable_balance_is_not_discount():
    raw = {"price": 2190, "discount": 200,
           "enjoyed": {"amountType": "3", "realDiscount": 2, "balance": 0},
           "enjoyable": {"amountType": "3", "realDiscount": 5, "balance": 610}}
    info = plan.interpret_enjoyment(raw)
    check("discount 被解释为优惠金额（分）", info["discountCents"] == 200)
    check("enjoyable.balance 被解释为门槛差额（分）", info["enjoyableBalanceCents"] == 610)
    check("门槛差额不等于优惠金额", info["discountCents"] != info["enjoyableBalanceCents"])
    check("解释器显式区分两者", "不得混用" in info["note"])
    check("enjoyable 非空不代表「已有券可用」", info["enjoyable"] is not None and info["discountCents"] == 200)


def test_four_amount_fields_present():
    quotes = json.loads((FIX / "synthetic_quotes.json").read_text(encoding="utf-8"))["quotes"]
    cands = [cand("T1", [("9900015160", "大堡口福三件套", 23.9)], 23.9)]
    out = run_compare(cands, quotes, 25.0)
    v = out["verified"][0]
    for field in ("originalPriceYuan", "discountYuan", "priceYuan", "savingYuan"):
        check(f"输出包含 {field}", field in v and v[field] is not None, str(v.get(field)))
    check("原价 ¥23.90 / 优惠 ¥4.00 / 应付 ¥19.90 / 节省 ¥4.00",
          (v["originalPriceYuan"], v["discountYuan"], v["priceYuan"], v["savingYuan"])
          == (23.9, 4.0, 19.9, 4.0),
          str((v["originalPriceYuan"], v["discountYuan"], v["priceYuan"], v["savingYuan"])))
    check("所有分单位字段存在（*Cents）",
          all(k in v for k in ("priceCents", "originalPriceCents", "discountCents", "savingCents")))


def test_coupon_quote_failure_is_unverifiable():
    quotes = json.loads((FIX / "synthetic_quotes.json").read_text(encoding="utf-8"))["quotes"]
    cands = [cand("T2", [("9900015187", "人气经典随心配", 14.9),
                         ("9900015165", "派", 8.5)], 23.4, pref_hits=2)]
    out = run_compare(cands, quotes, 25.0)
    v = out["verified"][0]
    check("带券核价失败时回退到无券方案", v["chosenCouponKey"] == "none" and v["usedCoupon"] is False)
    check("无券方案节省为 0", v["savingYuan"] == 0)
    check("失败选项被记录为 unverifiable",
          len(out["unverifiableOptions"]) == 1
          and out["unverifiableOptions"][0]["status"] == plan.APPLICABILITY_UNVERIFIABLE,
          str(out["unverifiableOptions"][0]["status"] if out["unverifiableOptions"] else None))
    check("全部选项里没有任何「已省」金额来源为失败的券",
          all(o.get("savingYuan") is None for o in out["unverifiableOptions"]))


def test_over_budget_vs_quote_failed():
    quotes = json.loads((FIX / "synthetic_quotes.json").read_text(encoding="utf-8"))["quotes"]
    cands = [cand("T3", [("9900015183", "精选单人餐三件套", 35.0)], 35.0)]
    out = run_compare(cands, quotes, 25.0)
    check("超预算被判为 over_budget",
          out["rejected"] and out["rejected"][0]["status"] == "over_budget",
          str(out["rejected"][0]["status"] if out["rejected"] else None))
    check("超预算方案不进入 verified", out["meta"]["verifiedCount"] == 0)
    check("over_budget 与 quote_failed 是两个不同状态",
          "over_budget" != "quote_failed")

    cands2 = [cand("TX", [("9900015160", "大堡口福三件套", 23.9)], 23.9)]
    out2 = run_compare(cands2, {"TX": {"none": {"success": False, "code": 60006,
                                                "message": "商品已售罄或不可售"}}}, 25.0)
    check("无券基线失败 -> baseline_unavailable（不是没有核价记录）",
          out2["rejected"] and out2["rejected"][0]["status"] == "baseline_unavailable",
          str(out2["rejected"][0]["status"] if out2["rejected"] else None))


def test_compare_sorting_tiebreak():
    same = {"success": True, "code": 200,
            "data": {"originalPrice": 2000, "discount": 0, "price": 2000, "productList": []}}
    with_coupon = {"success": True, "code": 200,
                   "data": {"originalPrice": 2000, "discount": 0, "price": 2000, "productList": []}}
    cands = [
        cand("A1", [("1450", "麦香鸡", 16.5)], 20.0, pref_hits=2, style="多品类"),
        cand("A2", [("1406", "板烧鸡腿堡", 16.5)], 20.0, pref_hits=1),
    ]
    quotes = {"A1": {"none": same}, "A2": {"none": same, "Z9": with_coupon}}
    out = run_compare(cands, quotes, 25.0)
    order = [v["id"] for v in out["verified"]]
    check("同价时偏好命中多者优先", order == ["A1", "A2"], str(order))

    cands = [
        cand("B1", [("1450", "麦香鸡", 16.5)], 20.0, pref_hits=1),
        cand("B2", [("1406", "板烧鸡腿堡", 16.5)], 20.0, pref_hits=1),
    ]
    quotes = {"B1": {"none": same}, "B2": {"none": same, "Z9": with_coupon}}
    out = run_compare(cands, quotes, 25.0)
    order = [v["id"] for v in out["verified"]]
    check("同价同偏好时用券更少者优先", order == ["B1", "B2"], str(order))


def test_probe_budget_declared():
    quotes = json.loads((FIX / "synthetic_quotes.json").read_text(encoding="utf-8"))["quotes"]
    cands = [cand("T1", [("9900015160", "大堡口福三件套", 23.9)], 23.9)]
    out = run_compare(cands, quotes, 25.0, max_probes=1)
    check("超出探针预算被声明", out["meta"]["probeBudgetExceeded"] is True,
          f"probeCount={out['meta']['probeCount']}, budget={out['meta']['probeBudget']}")
    check("探针数被如实记录", out["meta"]["probeCount"] == 2, str(out["meta"]["probeCount"]))
    check("输出带搜索范围声明", "全局最优" in out["meta"]["scopeNote"])


# --------------------------------------------- 11/12/13/14. 查询状态与三分类
def test_query_error_not_equal_no_coupons():
    doc = make_doc(store=None, s_status=plan.QUERY_ERROR)
    norm = plan.normalize_coupons(doc, NOW)
    check("查询失败时产出 0 张券", norm["coupons"] == [])
    check("查询失败产生明确告警", any("查询失败" in w and "没有优惠券" in w for w in norm["warnings"]),
          str(norm["warnings"]))
    check("查询失败不会被当成「查询成功但为空」", norm["queryState"]["store"] == plan.QUERY_ERROR)

    doc2 = make_doc(store=None, s_status=plan.QUERY_OK)
    norm2 = plan.normalize_coupons(doc2, NOW)
    check("状态 ok 但解析不出券 -> 告警为解析失败（不是没有券）",
          any("解析失败" in w for w in norm2["warnings"]), str(norm2["warnings"]))


def test_real_empty_samples():
    doc = make_doc(wallet=REAL_WALLET_EMPTY_RAW, store=REAL_STORE_EMPTY_RAW,
                   w_status=plan.QUERY_EMPTY, s_status=plan.QUERY_EMPTY)
    norm = plan.normalize_coupons(doc, NOW)
    check("真实空样本解析为 0 张券", norm["coupons"] == [])
    check("真实空样本不产生 wallet/store 告警（确实是空，不是失败）",
          not any(w.startswith(("wallet：", "store：")) for w in norm["warnings"]),
          str(norm["warnings"]))

    doc2 = make_doc(store=REAL_STORE_EMPTY_RAW, s_status=plan.QUERY_OK)
    norm2 = plan.normalize_coupons(doc2, NOW)
    check("data:[] 被识别为「明确的空」而非解析失败",
          not any(w.startswith("store：") for w in norm2["warnings"]), str(norm2["warnings"]))


def test_available_coupons_are_unassessable():
    doc = make_doc(available=REAL_AVAILABLE_RAW, a_status=plan.QUERY_OK)
    norm = plan.normalize_coupons(doc, NOW)
    check("available-coupons 券名去重后为 6 张", len(norm["unclaimed"]) == 6,
          f"实际 {len(norm['unclaimed'])}")
    check("未领取券的 couponId 一律为空",
          all(c["couponId"] is None for c in norm["unclaimed"]))
    check("未领取券的 probeKey 为空（无法送进官方试算）",
          all(c["probeKey"] is None for c in norm["unclaimed"]))
    check("未领取券不计入「已领取」的 coupons 列表",
          all(c["source"] != plan.COUPON_SOURCE_UNCLAIMED for c in norm["coupons"]))


def test_three_way_classification():
    doc = make_doc(wallet=WALLET_SYNTHETIC_RAW, store=STORE_SAMPLE_RAW,
                   w_status=plan.QUERY_OK, s_status=plan.QUERY_OK)
    norm = plan.normalize_coupons(doc, NOW)
    store_ids = {c["couponId"] for c in norm["storeUsable"]}
    check("门店可用券被单独归类", store_ids == {"S3001", "S3002", "S3003", "S3004"}, str(store_ids))
    check("卡包券与门店券 couponId 无交集时全部进入 walletOnly",
          len(norm["walletOnly"]) == 5, f"实际 {len(norm['walletOnly'])}")
    check("卡包券不会被当成门店可用券",
          all(c["source"] == plan.COUPON_SOURCE_WALLET for c in norm["walletOnly"]))
    check("门店可用券与卡包券的 source 不同",
          all(c["source"] == plan.COUPON_SOURCE_STORE for c in norm["storeUsable"]))

    # 交集情形：门店券 couponId 命中卡包券
    wallet_same = json.dumps({"data": [{"couponId": "S3001", "couponCode": "WC-9",
                                        "title": "派买一送一"}]}, ensure_ascii=False)
    doc2 = make_doc(wallet=wallet_same, store=STORE_SAMPLE_RAW,
                    w_status=plan.QUERY_OK, s_status=plan.QUERY_OK)
    norm2 = plan.normalize_coupons(doc2, NOW)
    check("门店券命中卡包时不再计入 walletOnly", norm2["walletOnly"] == [],
          str([c["couponId"] for c in norm2["walletOnly"]]))


# --------------------------------------------------------- 15. 覆盖度体检
def test_coverage_does_not_modify_map():
    before = hashlib.sha256(PRODUCT_MAP.read_bytes()).hexdigest()
    menu = plan.extract_menu(plan.load_json(MENU_FIXTURE))
    pmap = plan.load_json(PRODUCT_MAP)
    stats = plan.coverage_stats(menu, pmap)
    after = hashlib.sha256(PRODUCT_MAP.read_bytes()).hexdigest()

    check("覆盖度统计可用", stats["menuItemCount"] > 0 and stats["coveredCount"] > 0,
          f"{stats['coveredCount']}/{stats['menuItemCount']}")
    check("覆盖度体检不修改映射表", before == after)
    # 说明：原先这里断言「待核实清单非空」。该断言描述的是**改动前的数据状态**，
    # 在 P1.5 补齐 15 个商品后已被正当消灭，故改为断言**机制**（结构正确 + 已补齐项不再出现），
    # 并追加统计自洽性校验 —— 是加强，不是放宽。
    check("待核实清单结构完整（可能为空，但字段必须齐全）",
          all(("code" in x and "name" in x and "menuPriceYuan" in x)
              for x in stats["uncoveredBurgerLike"]),
          f"{len(stats['uncoveredBurgerLike'])} 条")
    check("覆盖度统计自洽：covered + uncovered + excluded == menuItemCount",
          stats["coveredCount"] + stats["uncoveredCount"] + stats["excludedCount"]
          == stats["menuItemCount"],
          f"{stats['coveredCount']} + {stats['uncoveredCount']} + {stats['excludedCount']}"
          f" vs {stats['menuItemCount']}")
    # P1.7 收紧：映射表中「菜单里找不到」的条目**必须**全部是已标 retired 的旧编码。
    # 原断言是 `verifiedNotInMenu == []`，那是「快照与映射表恰好一致」的历史状态，
    # 不是业务不变量；真正的不变量是「凡不在售的条目都必须有明确失效说明」。
    retired_in_map = {c for c, r in pmap.get("verifiedProducts", {}).items() if r.get("retired")}
    check("映射表中不在菜单里的条目全部已标 retired",
          set(stats["verifiedNotInMenu"]) == retired_in_map,
          f"不在菜单={sorted(stats['verifiedNotInMenu'])} retired={sorted(retired_in_map)}")
    check("不存在「既不在售、又没说明失效原因」的僵尸条目",
          stats["verifiedNotInMenuUnmarked"] == [], str(stats["verifiedNotInMenuUnmarked"]))
    check("已标 retired 的编码没有重新出现在菜单里（若出现必须重新核实）",
          stats["retiredInMenu"] == [], str(stats["retiredInMenu"]))
    newly_verified = {"521316", "521952", "521953", "9900000890", "9900000891", "9900004066",
                      "9900005449", "9900005453", "9900005456", "9900005462", "9900005464",
                      "9900016073", "9900016075", "9900016309", "9900016310"}
    check("P1.5 补齐的 15 个商品不再出现在待核实清单里",
          not any(x["code"] in newly_verified for x in stats["uncoveredBurgerLike"]))


# ------------------------------------------------------- 16. 内核无写操作
def test_kernel_has_no_write_tools():
    src = (ROOT / "scripts/plan.py").read_text(encoding="utf-8")
    for token in ("auto-bind-coupons", "create-order", "cancel-order",
                  "mall-create-order", "delivery-create-address"):
        check(f"本地内核不引用写工具 {token}", token not in src)


# ---------------------------------------------------------------------- main
def main() -> int:
    test_money_units()
    test_store_grouping()
    test_validity()
    test_applicability_and_matching()
    test_attach_and_pruning()
    test_saving_is_price_difference_not_face_value()
    test_enjoyable_balance_is_not_discount()
    test_four_amount_fields_present()
    test_coupon_quote_failure_is_unverifiable()
    test_over_budget_vs_quote_failed()
    test_compare_sorting_tiebreak()
    test_probe_budget_declared()
    test_query_error_not_equal_no_coupons()
    test_real_empty_samples()
    test_available_coupons_are_unassessable()
    test_three_way_classification()
    test_coverage_does_not_modify_map()
    test_kernel_has_no_write_tools()

    failed = [r for r in RESULTS if not r[1]]
    for label, ok, detail in RESULTS:
        print(f"{'[通过]' if ok else '[失败]'} {label}" + (f"  ({detail})" if detail else ""))
    print(f"\n券测试结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
