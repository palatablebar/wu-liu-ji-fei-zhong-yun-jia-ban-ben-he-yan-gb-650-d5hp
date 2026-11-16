"""M5 编排：按运单逐段组合分区价与附加费，落库账单行、分段行与定价问题。

整票账单行是各段开票金额之和的可回查汇总；整票只收一次首重，后续段首重记为让利。
定价失败（某段无有效期运价、路线缺段）时整票不进账单，问题带段号进 pricing_issues，
并由发布流程挡住这一代结果，旧版账单保持可读。
"""

from __future__ import annotations

from decimal import Decimal

from app import manifest, store
from app.errors import DomainError
from app.modules.m1_waybill import route as route_lib
from app.modules.m2_rates import service as rates
from app.modules.m2_rates import version as rateversion
from app.modules.m4_weights import billable
from app.modules.m5_charges import legs as leg_pricing
from app.modules.m5_charges import money
from app.reasons import sort_codes

ZERO3 = Decimal("0.000")


def _legs_of(row: dict) -> tuple[list[dict], list[dict]]:
    text = row.get("route") or ""
    legs, problems = route_lib.parse(text, row["carrier"], row["service"], row["zone"])
    return legs, problems


def compute_charges(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise DomainError("RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    # 先定位草稿代，再取该代的计费重：不能拿已发布代的计费重去算一张新草稿的账单。
    generation = store.ensure_draft(run_id)
    weights = store.weight_lines(run_id, generation)
    if not weights:
        raise DomainError("NO_WEIGHTS", "该运行还没有计费重", 409)
    weight_legs = {(row["waybill_no"], int(row["leg_no"])): row
                   for row in store.weight_legs(run_id, generation)}
    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    index_map = rates.index()

    lines: list[dict] = []
    charge_legs: list[dict] = []
    issues: list[dict] = []
    contract_total = Decimal("0.00")
    waiver_total = Decimal("0.00")
    priced_legs = 0

    for weight in weights:
        waybill = waybills.get(weight["waybill_no"])
        if waybill is None:
            continue
        pickup = rateversion.parse_utc(waybill["pickup_utc"])
        route_legs, route_problems = _legs_of(waybill)
        if weight["source"] == billable.UNBILLED and not route_problems:
            # 计费重阶段已经判断出无法定价，问题在 weight_legs 的原因码里带上。
            for item in route_legs:
                leg_row = weight_legs.get((waybill["waybill_no"], item["leg_no"]), {})
                if leg_row.get("status") == "priced":
                    continue
                for code in (leg_row.get("codes") or "RATE_MISSING").split(","):
                    if code:
                        issues.append({
                            "waybill_no": waybill["waybill_no"],
                            "leg_no": item["leg_no"],
                            "code": code,
                            "detail": f"第 {item['leg_no']} 段 "
                                      f"{item['carrier']}/{item['service']}/{item['zone']} 无法定价",
                        })
            continue

        declared = Decimal(waybill["declared_value"])
        priced: list[dict] = []
        failed = False
        for item in route_legs:
            card, codes = rates.select_card(
                index_map, item["carrier"], item["service"], item["zone"], pickup
            )
            weight_leg = weight_legs.get((waybill["waybill_no"], item["leg_no"]))
            if card is None:
                failed = True
                for code in codes:
                    issues.append({
                        "waybill_no": waybill["waybill_no"],
                        "leg_no": item["leg_no"],
                        "code": code,
                        "detail": f"第 {item['leg_no']} 段 "
                                  f"{item['carrier']}/{item['service']}/{item['zone']} 没有有效运价",
                    })
                continue
            billable_kg = Decimal(weight_leg["billable_kg"]) if weight_leg else Decimal("0.000")
            steps = int(weight_leg["steps"]) if weight_leg else 0
            priced.append(leg_pricing.leg_pricing(card, billable_kg, steps, declared, item["leg_no"])
                          | {"carrier": item["carrier"], "service": item["service"],
                             "zone": item["zone"], "version_id": card["version_id"],
                             "status": "priced"})
        if failed or not priced:
            continue

        waiver = leg_pricing.apply_waiver(priced)
        row_total = leg_pricing.bill_legs(priced)
        contract = money.total([Decimal(item["contract_total"]) for item in priced])
        row_codes = ["INTERLINE_ROUTE"] if len(priced) > 1 else []
        if waiver > 0:
            row_codes.append("FIRST_WEIGHT_WAIVED")
        if any(Decimal(item["min_topup"]) > 0 for item in priced):
            row_codes.append("MIN_CHARGE_APPLIED")
        if any(Decimal(item["remote_amount"]) > 0 for item in priced):
            row_codes.append("ZONE_REMOTE")
        if weight["source"] == billable.VOLUMETRIC:
            row_codes.append("VOLUMETRIC_APPLIED")

        base = money.total([Decimal(item["base_billed"]) for item in priced])
        step_part = money.total([Decimal(item["step_amount"]) for item in priced])
        remote = money.total([Decimal(item["remote_amount"]) for item in priced])
        fuel = money.total([Decimal(item["fuel_billed"]) for item in priced])
        insurance = money.total([Decimal(item["insurance_amount"]) for item in priced])

        lines.append({
            "waybill_no": weight["waybill_no"],
            "version_id": weight["version_id"] or "+".join(
                item["version_id"] for item in priced),
            "base_amount": str(base),
            "step_amount": str(step_part),
            "remote_amount": str(remote),
            "fuel_amount": str(fuel),
            "insurance_amount": str(insurance),
            "waiver_amount": str(waiver),
            "contract_total": str(contract),
            "total_amount": str(row_total),
            "codes": ",".join(sort_codes(row_codes)),
        })
        for item in priced:
            charge_legs.append({
                "waybill_no": weight["waybill_no"],
                "leg_no": item["leg_no"],
                "carrier": item["carrier"],
                "service": item["service"],
                "zone": item["zone"],
                "status": item["status"],
                "version_id": item["version_id"],
                "billable_kg": item["billable_kg"],
                "steps": item["steps"],
                "base_amount": item["base_billed"],
                "step_amount": item["step_amount"],
                "remote_amount": item["remote_amount"],
                "fuel_amount": item["fuel_billed"],
                "insurance_amount": item["insurance_amount"],
                "contract_total": item["contract_total"],
                "waiver_amount": item["waiver_amount"],
                "billed_total": item["billed_total"],
                "codes": ",".join(sort_codes(
                    (["FIRST_WEIGHT_WAIVED"] if Decimal(item["waiver_amount"]) > 0 else [])
                    + (["MIN_CHARGE_APPLIED"] if Decimal(item["min_topup"]) > 0 else [])
                    + (["ZONE_REMOTE"] if Decimal(item["remote_amount"]) > 0 else [])
                )),
            })
            priced_legs += 1
            contract_total += Decimal(item["contract_total"])
            waiver_total += Decimal(item["waiver_amount"])

    store.replace_charge_generation(run_id, generation, lines, charge_legs, issues)
    grand = money.total([Decimal(line["total_amount"]) for line in lines])
    contract = money.total([contract_total])
    waiver = money.total([waiver_total])
    now = manifest.utc_now_iso()
    store.mark_generation(run_id, generation, {
        "amount_total": str(grand),
        "contract_total": str(contract),
        "waiver_total": str(waiver),
        "legs_priced": priced_legs,
        "rate_digest": rates.rate_digest(),
    })
    store.add_stage(run_id, "charges", now)

    # 核对通过才发布：整代结果原子置为已发布，旧代仍可按代次回查。
    # 平价失败时保留草稿供复核页查看，run 上的合计与发布指针保持旧版不动。
    from app.modules.m6_reconcile import service as reconcile_service

    published = False
    reused = False
    block: dict = {}
    try:
        result = reconcile_service.publish(run_id, generation)
        published = bool(result.get("published"))
        reused = bool(result.get("reused"))
        if reused:
            generation = int(result.get("generation") or generation)
    except DomainError as exc:
        block = {"code": exc.code, "detail": exc.detail}

    return {
        "run_id": run_id,
        "generation": generation,
        "published": published,
        "reused": reused,
        "blocked": block or None,
        "billed": len(lines),
        "legs": priced_legs,
        "amount_total": str(grand),
        "contract_total": str(contract),
        "waiver_total": str(waiver),
        "issues": len(issues),
        "rate_digest": rates.rate_digest(),
    }


def lines(run_id: str) -> list[dict]:
    return store.charge_lines(run_id) if run_id else []


def leg_lines(run_id: str) -> list[dict]:
    return store.charge_legs(run_id) if run_id else []


def issues(run_id: str) -> list[dict]:
    return store.pricing_issues(run_id) if run_id else []


def has_draft(run_id: str) -> bool:
    return bool(store.draft_generation(run_id))


def amounts(run_id: str) -> dict:
    items = lines(run_id)
    legs = leg_lines(run_id)
    parts = {
        "base": money.total([Decimal(i["base_amount"]) for i in items]),
        "step": money.total([Decimal(i["step_amount"]) for i in items]),
        "remote": money.total([Decimal(i["remote_amount"]) for i in items]),
        "fuel": money.total([Decimal(i["fuel_amount"]) for i in items]),
        "insurance": money.total([Decimal(i["insurance_amount"]) for i in items]),
    }
    grand = money.total([Decimal(i["total_amount"]) for i in items])
    contract = money.total([Decimal(i["contract_total"]) for i in items])
    waiver = money.total([Decimal(i["waiver_amount"]) for i in items])
    return {
        "count": len(items),
        "legs": len(legs),
        "parts": parts,
        "total": grand,
        "contract": contract,
        "waiver": waiver,
        "conserved": money.conserved(grand, parts.values()),
        "contract_conserved": money.q2(contract - waiver) == grand,
        "legs_sum": money.total([Decimal(i["billed_total"]) for i in legs]),
    }

