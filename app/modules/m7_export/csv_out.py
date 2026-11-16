"""证据行导出：原始行号 + 原因码 + 计费重依据 + 版本号 + 分段依据，供人工复核。

每条证据行按「运单 × 段」展开，能直接看到这一段的承运商、服务、分区、命中版本、
体积重与计费重、以及该段的分项金额；整数票行同时保留汇总口径。
"""

from __future__ import annotations

import csv
import io

from app import store
from app.reasons import label

HEADER = [
    "line_no", "waybill_no", "leg_no", "carrier", "service", "zone", "pickup_utc",
    "actual_kg", "volumetric_kg", "source", "billable_kg", "steps", "version_id",
    "base_amount", "step_amount", "remote_amount", "fuel_amount", "insurance_amount",
    "contract_amount", "waiver_amount", "total_amount", "reason_codes", "reason_labels",
]


def _merge_codes(*groups) -> list[str]:
    """各层原因码合并，保持首次出现顺序。"""
    merged: list[str] = []
    for group in groups:
        for code in (group or "").split(","):
            if code and code not in merged:
                merged.append(code)
    return merged


def rows(run_id: str, generation: int | None = None) -> list[dict]:
    generation = store.released_generation(run_id) if generation is None else generation
    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    weights = {(row["waybill_no"], int(row["leg_no"])): row
               for row in store.weight_legs(run_id, generation)}
    charge_legs = {(row["waybill_no"], int(row["leg_no"])): row
                   for row in store.charge_legs(run_id, generation)}
    charge_lines = {row["waybill_no"]: row for row in store.charge_lines(run_id, generation)}

    merged: list[dict] = []
    keys = sorted(
        set(weights) | set(charge_legs),
        key=lambda key: (waybills.get(key[0], {}).get("line_no", 0), key[0], key[1]),
    )
    for waybill_no, leg_no in keys:
        waybill = waybills.get(waybill_no, {})
        weight = weights.get((waybill_no, leg_no), {})
        charge = charge_legs.get((waybill_no, leg_no), {})
        line = charge_lines.get(waybill_no, {})
        last_leg = leg_no == max(
            (key[1] for key in keys if key[0] == waybill_no), default=leg_no
        )
        codes = _merge_codes(
            weight.get("codes"),
            charge.get("codes"),
            line.get("codes") if last_leg else "",
        )
        merged.append({
            "line_no": waybill.get("line_no", 0),
            "waybill_no": waybill_no,
            "leg_no": leg_no,
            "carrier": charge.get("carrier") or weight.get("carrier") or waybill.get("carrier", ""),
            "service": charge.get("service") or weight.get("service") or waybill.get("service", ""),
            "zone": charge.get("zone") or weight.get("zone") or waybill.get("zone", ""),
            "pickup_utc": waybill.get("pickup_utc", ""),
            "actual_kg": weight.get("actual_kg", ""),
            "volumetric_kg": weight.get("volumetric_kg", ""),
            "source": weight.get("source", ""),
            "billable_kg": weight.get("billable_kg", ""),
            "steps": weight.get("steps", 0),
            "version_id": weight.get("version_id") or charge.get("version_id", ""),
            "base_amount": charge.get("base_amount", "0.00"),
            "step_amount": charge.get("step_amount", "0.00"),
            "remote_amount": charge.get("remote_amount", "0.00"),
            "fuel_amount": charge.get("fuel_amount", "0.00"),
            "insurance_amount": charge.get("insurance_amount", "0.00"),
            "contract_amount": charge.get("contract_total", "0.00"),
            "waiver_amount": charge.get("waiver_amount", "0.00"),
            "total_amount": charge.get("billed_total", "0.00"),
            "reason_codes": " ".join(codes),
            "reason_labels": " ".join(label(c) for c in codes),
        })
    return merged


def csv_text(run_id: str, generation: int | None = None) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    for row in rows(run_id, generation):
        writer.writerow([row[key] for key in HEADER])
    return buffer.getvalue()
