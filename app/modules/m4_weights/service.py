"""M4 编排：按运单逐段择版、算体积重、取大、进位，落库计费重行与分段行。

整票的计费重取各段之和：同一票货在每一段都占一次舱位，体积重与实重按段各自
取大、各自按该段运价的台阶进位，因此联运票的计费重通常大于单段口径。
整票行只做汇总，真正可回查的依据是 weight_legs 每一段。
"""

from __future__ import annotations

from decimal import Decimal

from app import manifest, store
from app.errors import DomainError
from app.modules.m1_waybill import route as route_lib
from app.modules.m2_rates import service as rates
from app.modules.m2_rates import version as rateversion
from app.modules.m4_weights import billable, rounding, volumetric
from app.reasons import sort_codes

ZERO3 = Decimal("0.000")


def _legs_of(row: dict) -> list[dict]:
    """运单的分段：优先用采信时归一好的路线列，缺列时按整票单段处理。"""
    text = row.get("route") or ""
    legs, problems = route_lib.parse(text, row["carrier"], row["service"], row["zone"])
    if problems:
        # 采信阶段已经拦下结构问题，这里只做兜底，不再产出新原因码。
        return [route_lib.leg(row["carrier"], row["service"], row["zone"], 1)]
    return legs


def _leg_weight(row: dict, item: dict, card: dict) -> dict:
    actual = Decimal(row["weight_kg"])
    volume = volumetric.volumetric_kg(
        Decimal(row["length_cm"]),
        Decimal(row["width_cm"]),
        Decimal(row["height_cm"]),
        int(card["volume_divisor"]),
    )
    raw, source = billable.pick(actual, volume)
    step = Decimal(card["step_weight_kg"])
    billable_kg = rounding.ceil_to_step(raw, step)
    steps = rounding.steps_above(billable_kg, Decimal(card["first_weight_kg"]), step)
    return {
        "leg_no": item["leg_no"],
        "carrier": item["carrier"],
        "service": item["service"],
        "zone": item["zone"],
        "status": "priced",
        "version_id": card["version_id"],
        "actual_kg": str(actual),
        "volumetric_kg": str(volume),
        "source": source,
        "billable_kg": str(billable_kg),
        "steps": steps,
        "codes": ",".join(billable.source_codes(source)),
    }


def _missing_leg(row: dict, item: dict, codes: list[str]) -> dict:
    return {
        "leg_no": item["leg_no"],
        "carrier": item["carrier"],
        "service": item["service"],
        "zone": item["zone"],
        "status": "unpriced",
        "version_id": "",
        "actual_kg": row["weight_kg"],
        "volumetric_kg": "0.000",
        "source": billable.UNBILLED,
        "billable_kg": "0.000",
        "steps": 0,
        "codes": ",".join(sort_codes(codes)),
    }


def compute_weights(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise DomainError("RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    rows = store.waybills(run_id)
    if not rows:
        raise DomainError("NO_ROWS", "该运行没有可计费的运单", 409)

    index_map = rates.index()
    generation = store.ensure_draft(
        run_id,
        input_sha256=run.get("input_sha256", ""),
        rules_version=run.get("rules_version", ""),
        rate_digest=rates.rate_digest(),
        created_at=manifest.utc_now_iso(),
    )

    lines: list[dict] = []
    legs: list[dict] = []
    unbilled = 0
    for row in rows:
        pickup = rateversion.parse_utc(row["pickup_utc"])
        leg_rows: list[dict] = []
        row_codes: list[str] = []
        missing = False
        for item in _legs_of(row):
            card, codes = rates.select_card(
                index_map, item["carrier"], item["service"], item["zone"], pickup
            )
            if card is None:
                missing = True
                leg_rows.append(_missing_leg(row, item, codes))
                row_codes.extend(codes)
                continue
            leg_rows.append(_leg_weight(row, item, card))
        for item in leg_rows:
            item["waybill_no"] = row["waybill_no"]
        legs.extend(leg_rows)

        total_kg = sum(
            (Decimal(item["billable_kg"]) for item in leg_rows), ZERO3
        ).quantize(Decimal("0.001"))
        sources = {item["source"] for item in leg_rows}
        if missing:
            unbilled += 1
            source = billable.UNBILLED
        elif billable.VOLUMETRIC in sources and billable.UNBILLED not in sources:
            source = billable.VOLUMETRIC if sources == {billable.VOLUMETRIC} else "mixed"
        else:
            source = billable.ACTUAL if billable.ACTUAL in sources else billable.TIE

        if len(leg_rows) > 1:
            row_codes.append("INTERLINE_ROUTE")
        if source == billable.VOLUMETRIC:
            row_codes.append("VOLUMETRIC_APPLIED")
        version_ids = [item["version_id"] for item in leg_rows if item["version_id"]]
        # 有任何一段没落到版本，整票就不算「单选命中」，避免版本账把半票算成已定版。
        version_id = "" if missing else (
            version_ids[0] if len(version_ids) == 1 else "+".join(version_ids)
        )
        lines.append({
            "waybill_no": row["waybill_no"],
            "version_id": version_id,
            "actual_kg": str(Decimal(row["weight_kg"])),
            "volumetric_kg": str(
                sum((Decimal(item["volumetric_kg"]) for item in leg_rows), ZERO3)
            ),
            "source": source,
            "billable_kg": str(total_kg),
            "steps": sum(int(item["steps"]) for item in leg_rows),
            "codes": ",".join(sort_codes(row_codes)),
        })

    store.replace_weight_generation(run_id, generation, lines, legs)
    # 计费重是账单的上游：重算后同一代里的旧账单行与问题清单必须作废，
    # 否则会出现「用了新计费重却留着旧金额」的半新半旧结果。
    store.replace_charge_generation(run_id, generation, [], [], [])
    now = manifest.utc_now_iso()
    priced_legs = sum(1 for item in legs if item["status"] == "priced")
    store.mark_generation(run_id, generation, {
        "rows_billed": len(lines) - unbilled,
        "legs_priced": priced_legs,
        "rate_digest": rates.rate_digest(),
    })
    store.add_stage(run_id, "weights", now)
    return {
        "run_id": run_id,
        "generation": generation,
        "billed": len(lines) - unbilled,
        "unbilled": unbilled,
        "volumetric_hits": sum(1 for line in lines if line["source"] == billable.VOLUMETRIC),
        "legs_priced": priced_legs,
        "steps_total": sum(int(line["steps"]) for line in lines),
        "rate_digest": rates.rate_digest(),
    }


def lines(run_id: str) -> list[dict]:
    """计费重展示：刚算完的草稿优先，否则已发布代。"""
    return store.weight_lines(run_id, store.weight_view_generation(run_id))


def leg_lines(run_id: str) -> list[dict]:
    return store.weight_legs(run_id, store.weight_view_generation(run_id))


def summary(run_id: str) -> dict:
    items = lines(run_id)
    legs = leg_lines(run_id)
    per_waybill: dict[str, int] = {}
    for item in legs:
        per_waybill[item["waybill_no"]] = per_waybill.get(item["waybill_no"], 0) + 1
    return {
        "count": len(items),
        "billed": sum(1 for i in items if i["source"] != billable.UNBILLED),
        "unbilled": sum(1 for i in items if i["source"] == billable.UNBILLED),
        "volumetric_hits": sum(1 for i in items if i["source"] == billable.VOLUMETRIC),
        "actual_hits": sum(1 for i in items if i["source"] in (billable.ACTUAL, billable.TIE)),
        "mixed_hits": sum(1 for i in items if i["source"] == "mixed"),
        "interline_waybills": sum(1 for count in per_waybill.values() if count > 1),
        "legs": len(legs),
        "legs_priced": sum(1 for i in legs if i["status"] == "priced"),
        "billable_total": str(sum((Decimal(i["billable_kg"]) for i in items), ZERO3)),
    }
