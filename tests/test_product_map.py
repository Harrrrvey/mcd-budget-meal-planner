#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_product_map.py — 商品映射专项测试（P1.5）

只用标准库，不依赖 pytest（也可被 pytest 直接收集）。运行：
    python3 tests/test_product_map.py

覆盖：
  1. 已确认默认组成含汉堡的商品能进入硬约束候选池
  2. 已确认不含汉堡的套餐不会进入候选池
  3. 组成未知的商品不会被误判为满足汉堡条件
  4. 可选替换项不会被错误地当作默认组成
  5. 需要用户选择的套餐不会被误判为默认含汉堡
  6. 原有已确认映射没有被意外改变
  7. 新增映射不会导致候选生成异常
  8. 新增条目的证据完整性（tool / storeCode / at / raw）
  9. 映射表仍然不含任何价格字段

所有结论都基于 references/product-map.json 与 tests/fixtures/menu_plan_fixture.json。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import plan  # noqa: E402

PRODUCT_MAP = ROOT / "references/product-map.json"
MENU_FIXTURE = ROOT / "tests/fixtures/menu_plan_fixture.json"

# 2026-10-16 第四批新增的 15 个商品（真实 query-meal-detail 返回）
NEW_CODES = ["521316", "521952", "521953", "9900000890", "9900000891", "9900004066",
             "9900005449", "9900005453", "9900005456", "9900005462", "9900005464",
             "9900016073", "9900016075", "9900016309", "9900016310"]

# 2026-10-16 第六批（P1.8）新增的 11 个商品：从 coverage.priorityToVerify 清单核实并转正
P1_8_CODES = ["1100", "4648", "9900003586", "9900005466", "9900005413",
              "9900005411", "9900000888", "9900000886", "9900000893",
              "9900010110", "9900016058"]

# 改动前 16 个已确认条目的关键事实（用于回归：原有映射不得被意外改变）
ORIGINAL_SNAPSHOT = {
    "1450": (True, 0), "1406": (True, 0), "1440": (True, 0), "1120": (True, 0),
    "9900015162": (True, 1), "9900015164": (True, 1), "9900015160": (True, 3),
    "9900015183": (True, 3), "9900011128": (True, 4), "9900016054": (True, 5),
    "9900015187": (True, 2), "9900015188": (False, 2), "9900016076": (False, 3),
    "9900011126": (False, 3), "9900011123": (False, 3), "9900004835": (False, 3),
}

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(cond), detail))


def load():
    pmap = plan.load_json(PRODUCT_MAP)
    menu = plan.extract_menu(plan.load_json(MENU_FIXTURE))
    pool = plan.build_pool(menu, pmap)
    return pmap, menu, pool


# --------------------------------------------------- 1/2/3. 硬约束候选池
def test_hard_constraint_pool():
    _, menu, pool = load()
    by_code = {i["code"]: i for i in pool}

    # 1. 已确认默认含汉堡 → 可作锚点
    for code, name in [("521316", "高达吉士双牛堡"), ("521952", "龙焰鸡腿堡"),
                       ("521953", "龙焰芝士棒鸡腿堡"), ("9900000890", "板烧鸡腿堡四件套"),
                       ("9900000891", "麦辣鸡腿堡四件套"), ("9900005449", "吉士汉堡包套餐"),
                       ("9900005453", "麦香鸡套餐"), ("9900005456", "麦辣鸡腿汉堡三件套"),
                       ("9900005462", "板烧鸡腿堡三件套"), ("9900005464", "双层吉士汉堡套餐"),
                       ("9900016073", "龙焰鸡腿堡三件套"), ("9900016075", "龙焰芝士棒鸡腿堡三件套"),
                       ("9900016309", "龙焰鸡腿堡四件套"), ("9900016310", "龙焰芝士棒鸡腿堡四件套")]:
        item = by_code.get(code)
        check(f"「{name}」可进入硬约束候选池",
              item is not None and item["canSatisfyBurgerConstraint"] is True,
              f"实际 {item['canSatisfyBurgerConstraint'] if item else '不在池中'}")

    # 2. 已确认不含汉堡 → 不得作锚点
    #    注：9900015188「精选超值随心配」已在 P1.7 确认编码轮换为 9900015568，
    #    旧编码被强制排除，因此这里改用当前在售的 9900015568。
    for code, name in [("9900011126", "酥脆无双套餐"), ("9900011123", "“辣”么快乐套餐"),
                       ("9900004835", "麦乐鸡套餐"), ("9900015568", "精选超值随心配"),
                       ("9900016076", "“我就喜欢”鸡薯双全盒")]:
        item = by_code.get(code)
        check(f"不含汉堡的「{name}」不可作锚点",
              item is not None and item["canSatisfyBurgerConstraint"] is False)

    # 3. 组成未知 / 名称推断 → 不得作锚点
    inferred = [i for i in pool if not i["verified"]]
    check("名称推断商品一律不可作锚点",
          all(i["canSatisfyBurgerConstraint"] is False for i in inferred),
          f"{len(inferred)} 条名称推断商品")

    anchors = [i for i in pool if i["canSatisfyBurgerConstraint"]]
    pmap = plan.load_json(PRODUCT_MAP)
    expected_anchors = {c for c, r in pmap["verifiedProducts"].items()
                        if r.get("containsBurgerByDefault")
                        and not r.get("requiresUserSelection")
                        and not r.get("retired")
                        and c in menu}
    check("锚点集合 == 「已确认含汉堡 且 非多选 且 未失效 且 在售」的商品集合（代码与数据一致）",
          {i["code"] for i in anchors} == expected_anchors,
          f"池={len(anchors)} 期望={len(expected_anchors)}")
    # P1.12：数量改为**下限哨兵** —— 写死会随每次核实/菜单演进过期；
    # 低于下限说明有条目被误删或被误判为非锚点，必须查。
    check("锚点数量 ≥ 43（P1.7 的 25 + P1.8 的 11 + P1.12 的 7）",
          len(anchors) >= 43, f"实际 {len(anchors)}")


# ------------------------------------------------------- 4. 可选项 != 默认项
def test_optional_choices_are_not_default():
    pmap, _, _ = load()
    v = pmap["verifiedProducts"]

    rec = v["9900005456"]  # 麦辣鸡腿汉堡三件套：第 2 轮小食有 7 个可选
    codes = [c["choiceCode"] for c in rec["defaultComposition"]]
    check("默认组成只收录 isDefault=1 的项（不含可选替换项）",
          codes == ["1440", "4810", "3050"], str(codes))
    check("可选的「麦辣鸡翅2块(1700)」未被当作默认组成", "1700" not in codes)
    check("可选的「大薯条(4820)」未被当作默认组成", "4820" not in codes)

    rec = v["9900000890"]  # 板烧鸡腿堡四件套
    codes = [c["choiceCode"] for c in rec["defaultComposition"]]
    check("四件套默认组成含 4 项且顺序正确",
          codes == ["1406", "4810", "515106", "3050"], str(codes))
    check("可选的「马来咖喱肉骨鸡3块(521881)」未被当作默认组成", "521881" not in codes)

    # 每个条目的 defaultComposition 长度应与 roundsLen 一致
    for code, rec in v.items():
        if not rec.get("roundsVerified"):
            continue
        check(f"{code} 默认组成条数 == roundsLen",
              len(rec.get("defaultComposition") or []) == rec.get("roundsLen"),
              f"{len(rec.get('defaultComposition') or [])} vs {rec.get('roundsLen')}")


# --------------------------------------------- 5. 需要用户选择的套餐
def test_requires_user_selection():
    pmap, _, pool = load()
    rec = pmap["verifiedProducts"]["9900004066"]
    by_code = {i["code"]: i for i in pool}
    item = by_code["9900004066"]

    check("堡堡随心拼被标记 requiresUserSelection", rec.get("requiresUserSelection") is True)
    check("其默认组成确实含汉堡（如实记录，未被篡改）",
          rec.get("containsBurgerByDefault") is True)
    check("其 burgerSwitchable 为 True（第 1 轮含非汉堡选项）",
          rec.get("burgerSwitchable") is True)
    check("⚠️ 但因需用户选择，不得进入硬约束候选池",
          item["canSatisfyBurgerConstraint"] is False,
          f"实际 {item['canSatisfyBurgerConstraint']}")
    check("多选轮次的已选份数被如实记录",
          [c.get("selectedQuantity") for c in rec["defaultComposition"]] == [2, 3, 2])

    rounds = {c["choiceCode"] for c in rec["defaultComposition"]}
    check("非汉堡选项「酥酥多笋卷(521816)」不属于默认组成", "521816" not in rounds)

    # 全表：requiresUserSelection=true 的条目一律不可作锚点
    for code, rec in pmap["verifiedProducts"].items():
        if rec.get("requiresUserSelection"):
            item = by_code.get(code)
            check(f"{code} 因 requiresUserSelection 被排除在锚点外",
                  item is None or item["canSatisfyBurgerConstraint"] is False)


# --------------------------------------------- 6. 原有映射未被意外改变
def test_original_entries_unchanged():
    pmap, _, _ = load()
    v = pmap["verifiedProducts"]
    check("原有 16 个条目全部仍在", all(c in v for c in ORIGINAL_SNAPSHOT),
          str([c for c in ORIGINAL_SNAPSHOT if c not in v]))
    for code, (burger, rounds_len) in ORIGINAL_SNAPSHOT.items():
        rec = v.get(code)
        check(f"原有 {code} 的 containsBurgerByDefault 未变",
              rec is not None and rec["containsBurgerByDefault"] is burger)
        check(f"原有 {code} 的 roundsLen 未变",
              rec is not None and rec["roundsLen"] == rounds_len)

    # 原有条目的名称与默认组成首项抽样校验
    spot = {"9900015183": ("精选单人餐三件套", "1406"),
            "9900015188": ("精选超值随心配", "521881"),
            "9900004835": ("麦乐鸡套餐", "1401"),
            "9900016054": ("汉堡开心乐园餐", "1000")}
    for code, (name, first_choice) in spot.items():
        rec = v[code]
        check(f"原有 {code} 名称未变", rec["nameFromMenu"] == name, rec["nameFromMenu"])
        check(f"原有 {code} 默认组成首项未变",
              rec["defaultComposition"][0]["choiceCode"] == first_choice)


# ---------------------------------------- 7. 新增映射不破坏候选生成
def test_new_entries_do_not_break_pipeline():
    _, _, pool = load()
    by_code = {i["code"]: i for i in pool}
    for code in NEW_CODES:
        check(f"新增商品 {code} 出现在商品池中", code in by_code)

    import tempfile as _tf
    for budget in (25.0, 40.0, 60.0, 100.0):
        with _tf.TemporaryDirectory() as td:
            op = Path(td) / "o.json"
            try:
                plan.cmd_candidates(type("A", (), {
                    "menu": str(MENU_FIXTURE), "map": str(PRODUCT_MAP), "budget": budget,
                    "prefer_drink": True, "prefer_side": True, "max_items": 4,
                    "max_candidates": 6, "store_code": "3230019", "out": str(op)})())
                out = json.loads(op.read_text(encoding="utf-8"))
                ok = True
                detail = f"{len(out['candidates'])} 个候选"
            except SystemExit as exc:  # plan.fail() 走的是 SystemExit
                ok, out, detail = False, None, f"SystemExit({exc.code})"
            except Exception as exc:  # noqa: BLE001
                ok, out, detail = False, None, repr(exc)
            check(f"预算 {budget:.0f} 元：候选生成不报错", ok, detail)
            if not ok or not out:
                continue
            check(f"预算 {budget:.0f} 元：不产生超预算组合",
                  all(c["menuTotalYuan"] <= budget + 0.005 for c in out["candidates"]))
            check(f"预算 {budget:.0f} 元：每个候选都含已确认汉堡锚点",
                  all(any(by_code[i["code"]]["canSatisfyBurgerConstraint"]
                          for i in c["items"]) for c in out["candidates"]))
            check(f"预算 {budget:.0f} 元：候选商品全部来自商品池",
                  all(i["code"] in by_code for c in out["candidates"] for i in c["items"]))

    # 新增的大额套餐在足够预算下应能被搜到
    with tempfile.TemporaryDirectory() as td:
        op = Path(td) / "o.json"
        plan.cmd_candidates(type("A", (), {
            "menu": str(MENU_FIXTURE), "map": str(PRODUCT_MAP), "budget": 30.0,
            "prefer_drink": False, "prefer_side": False, "max_items": 4,
            "max_candidates": 20, "store_code": "3230019", "out": str(op)})())
        out = json.loads(op.read_text(encoding="utf-8"))
    found = {i["code"] for c in out["candidates"] for i in c["items"]}
    check("新增的 9900016073 龙焰鸡腿堡三件套（¥26.9）进入候选",
          "9900016073" in found, str(sorted(found)[:8]))


# --------------------------------------------------- 8. 证据完整性
def test_evidence_integrity():
    pmap, _, _ = load()
    v = pmap["verifiedProducts"]
    for code in NEW_CODES:
        rec = v[code]
        ev = rec.get("evidence") or {}
        check(f"{code} 有 evidence.tool", ev.get("tool") == "query-meal-detail")
        check(f"{code} 有 evidence.storeCode", bool(ev.get("storeCode")), str(ev.get("storeCode")))
        check(f"{code} 有 evidence.at 时间戳", isinstance(ev.get("at"), str) and "2026-10-16T10:0" in ev.get("at", ""))
        check(f"{code} 有 evidence.raw 原始结构描述", isinstance(ev.get("raw"), str) and len(ev["raw"]) > 20)
        check(f"{code} confidence 为 confirmed_detail", rec.get("confidence") == "confirmed_detail")
        check(f"{code} roundsVerified 为 True", rec.get("roundsVerified") is True)
        check(f"{code} 默认组成每项都有 isDefault=True",
              all(c.get("isDefault") is True for c in rec["defaultComposition"]))

    check("verificationContext 记录了时段切换说明",
          "daypartNote" in pmap.get("verificationContext", {}))
    check("requiresUserSelection 的语义有单独说明",
          "requiresUserSelectionNote" in pmap)


# --------------------------------------------------- 9. 映射表不含价格
def test_no_price_in_map():
    raw = PRODUCT_MAP.read_text(encoding="utf-8")
    for key in ("priceYuan", "currentPrice", "originalPrice", "\"price\""):
        check(f"映射表仍不含价格字段 {key}", key not in raw)
    pmap = plan.load_json(PRODUCT_MAP)
    for code, rec in pmap["verifiedProducts"].items():
        check(f"{code} 记录不含价格键",
              not any(k in rec for k in ("price", "priceYuan", "currentPrice")))


# ------------------------------------------- 10. 编码轮换对映射表的影响（P1.7）
def test_code_rotation_mapping_integrity():
    """编码轮换后：旧条目被排除、新条目独立生效、组成未被错误改写。"""
    pmap, menu, pool = load()
    v = pmap["verifiedProducts"]
    by_code = {i["code"] for i in pool}
    rotation = {p["retiredCode"]: p for p in pmap["codeRotation"]["pairs"]}

    # 1. 已确认失效的旧编码不得继续作为有效候选
    for code in rotation:
        check(f"失效编码 {code} 标了 retired", bool(v[code].get("retired")))
        check(f"失效编码 {code} 不在商品池中（不会被推荐）", code not in by_code)
        check(f"失效编码 {code} 不在刷新后的菜单快照中", code not in menu)

    # 2. 已确认的新编码能够正确进入候选搜索
    for code, p in rotation.items():
        new = p["currentCode"]
        check(f"新编码 {new}（替代 {code}）在映射表中", new in v)
        check(f"新编码 {new} 有 confidence=confirmed_detail", v[new]["confidence"] == "confirmed_detail")
        check(f"新编码 {new} 在商品池中", new in by_code)
        if v[new]["containsBurgerByDefault"]:
            check(f"新编码 {new} 可进入硬约束候选池",
                  new in {i["code"] for i in pool if i["canSatisfyBurgerConstraint"]})

    # 3. ★ 套餐组成没有因编码替换被错误改写：
    #    新条目必须是**独立核实**的结果，而不是把旧条目的组成复制粘贴过来。
    #    A 级：默认组成应当逐项一致；B 级：必须**存在**差异（否则说明忘了重新核实）。
    for code, p in rotation.items():
        old_comp = [c["choiceCode"] for c in v[code]["defaultComposition"]]
        new_comp = [c["choiceCode"] for c in v[p["currentCode"]]["defaultComposition"]]
        if p["equivalence"] == "A":
            check(f"A 级 {code} -> {p['currentCode']}：默认组成逐项一致",
                  old_comp == new_comp, f"{old_comp} vs {new_comp}")
        else:
            check(f"B 级 {code} -> {p['currentCode']}：默认组成确实不同（证明是重新核实过）",
                  old_comp != new_comp, f"{old_comp} vs {new_comp}")
            check(f"B 级 {code} -> {p['currentCode']}：新条目声明了不等价警告",
                  bool(v[p["currentCode"]].get("equivalenceWarning")))
        check(f"{code} -> {p['currentCode']}：新条目记录了轮换来源",
              v[p["currentCode"]].get("rotationFrom") == code,
              str(v[p["currentCode"]].get("rotationFrom")))
        check(f"{code} -> {p['currentCode']}：新条目证据时间晚于 (或等于) 旧条目",
              v[p["currentCode"]]["evidence"]["at"] >= "2026-10-16T10:55",
              v[p["currentCode"]]["evidence"]["at"])

    # 4. 未确认的替代编码不得被当作已验证映射
    for u in pmap["codeRotation"]["unresolved"]:
        check(f"未决编码 {u['code']} 未被标 retired", "retired" not in v.get(u["code"], {}))
        check(f"未决编码 {u['code']} 未被标为 confirmed_detail 条目",
              v.get(u["code"], {}).get("confidence") != "confirmed_detail")
    for item in pmap["codeRotation"]["codeChangesNotInMap"]:
        check(f"非映射表内的变更 {item['retiredCode']} 有结论字段",
              bool(item.get("conclusion") or item.get("whyNotInPairs")))

    # 5. 未核实组成的在售商品不得进入硬约束池。
    #    ⚠️ P1.8 修正：这里原先写死 1100 / 4648 / 9900003586，P1.8 把它们核实转正后
    #    就成了「假失败」。真正的不变量是**「凡不在 verifiedProducts 里的商品，一律不得作锚点」**，
    #    因此改成由数据推导，不再绑定某几个具体编码。
    unverified = [c for c in menu if c not in v]
    check("存在未核实的在售商品（避免断言空转）", len(unverified) > 0, f"{len(unverified)} 个")
    by_code_all = {i["code"]: i for i in pool}
    offenders = [c for c in unverified
                 if by_code_all.get(c) and by_code_all[c]["canSatisfyBurgerConstraint"]]
    check("凡未列入映射表的商品一律不得作锚点", offenders == [], str(sorted(offenders)[:8]))
    for code, name in [("1600", "麦香鱼"), ("9900005460", "麦香鱼套餐")]:
        item = by_code_all.get(code)
        check(f"未核实的「{name}」不因名称而作锚点",
              item is None or item["canSatisfyBurgerConstraint"] is False)


# ------------------------------------- 11. P1.8 补齐的 11 个疑似汉堡商品
def test_p1_8_priority_list_cleared():
    """P1.8：疑似汉堡清单已清空；新增条目全部转正且未被 proteinType 影响。"""
    pmap, menu, pool = load()
    v = pmap["verifiedProducts"]
    by_code = {i["code"]: i for i in pool}
    stats = plan.coverage_stats(menu, pmap)

    check("「名称疑似含汉堡但未核实」清单已清空",
          stats["uncoveredBurgerLike"] == [],
          str([(x["code"], x["name"]) for x in stats["uncoveredBurgerLike"]]))
    for code in P1_8_CODES:
        check(f"P1.8 新增 {code} 已转正为 confirmed_detail",
              v.get(code, {}).get("confidence") == "confirmed_detail")
        check(f"P1.8 新增 {code} 可作锚点",
              by_code.get(code, {}).get("canSatisfyBurgerConstraint") is True)
    check("proteinType 不影响锚点判定（鱼堡也是锚点）",
          all(by_code[c]["canSatisfyBurgerConstraint"] for c in P1_8_CODES
              if v[c].get("proteinType") == "fish"))
    check("映射条目数 ≥ 56（38 + P1.8 的 11 + P1.12 的 7；只增不减）",
          len(v) >= 56, f"实际 {len(v)}")
    inactive = sum(1 for r in v.values() if not r.get("retired"))
    check("在售（非 retired）条目数 ≥ 49", inactive >= 49, f"实际 {inactive}")
    check("覆盖度恒等式仍成立",
          stats["coveredCount"] + stats["uncoveredCount"] + stats["excludedCount"]
          == stats["menuItemCount"])


# ---------------------------------------------------------------------- main
def main() -> int:
    test_hard_constraint_pool()
    test_optional_choices_are_not_default()
    test_requires_user_selection()
    test_original_entries_unchanged()
    test_new_entries_do_not_break_pipeline()
    test_evidence_integrity()
    test_no_price_in_map()
    test_code_rotation_mapping_integrity()
    test_p1_8_priority_list_cleared()

    failed = [r for r in RESULTS if not r[1]]
    for label, ok, detail in RESULTS:
        print(f"{'[通过]' if ok else '[失败]'} {label}" + (f"  ({detail})" if detail else ""))
    print(f"\n映射测试结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
