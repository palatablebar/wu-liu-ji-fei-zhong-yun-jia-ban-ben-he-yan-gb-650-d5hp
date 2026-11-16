"""导出：账单明细、分段明细、证据行、按承运商汇总、可复算的清单 JSON。

交付口径只认已发布代（store.released_generation）：
核对未通过时这里看不到新结果，旧版导出照常可下载。
"""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal

from app import config, manifest, store
from app.modules.m6_reconcile import service as reconcile_service

INVOICE_HEADER = [
    "waybill_no", "carrier", "service", "zone", "route", "pickup_utc", "billable_kg",
    "legs", "version_id", "base_amount", "step_amount", "remote_amount", "fuel_amount",
    "insurance_amount", "contract_amount", "waiver_amount", "total_amount", "currency",
]

LEG_HEADER = [
    "waybill_no", "leg_no", "carrier", "service", "zone", "version_id", "billable_kg",
    "steps", "base_amount", "step_amount", "remote_amount", "fuel_amount",
    "insurance_amount", "contract_amount", "waiver_amount", "billed_amount", "reason_codes",
]


def currency_of() -> str:
    for card in store.rate_cards():
        return card["currency"]
    return "CNY"


def _released(run_id: str) -> int:
    return store.released_generation(run_id)


def invoice_rows(run_id: str) -> list[dict]:
    currency = currency_of()
    generation = _released(run_id)
    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    billable = {row["waybill_no"]: row["billable_kg"]
                for row in store.weight_lines(run_id, generation)}
    legs_by: dict[str, list[dict]] = {}
    for row in store.charge_legs(run_id, generation):
        legs_by.setdefault(row["waybill_no"], []).append(row)
    rows = []
    for row in store.charge_lines(run_id, generation):
        waybill = waybills.get(row["waybill_no"], {})
        legs = legs_by.get(row["waybill_no"], [])
        rows.append({
            "waybill_no": row["waybill_no"],
            "carrier": waybill.get("carrier", ""),
            "service": waybill.get("service", ""),
            "zone": waybill.get("zone", ""),
            "route": waybill.get("route", "") or (
                f"{legs[0]['carrier']}/{legs[0]['service']}/{legs[0]['zone']}" if legs else ""
            ),
            "pickup_utc": waybill.get("pickup_utc", ""),
            "billable_kg": billable.get(row["waybill_no"], "0.000"),
            "legs": len(legs),
            "version_id": row["version_id"],
            "base_amount": row["base_amount"],
            "step_amount": row["step_amount"],
            "remote_amount": row["remote_amount"],
            "fuel_amount": row["fuel_amount"],
            "insurance_amount": row["insurance_amount"],
            "contract_amount": row["contract_total"],
            "waiver_amount": row["waiver_amount"],
            "total_amount": row["total_amount"],
            "currency": currency,
        })
    rows.sort(key=lambda row: row["waybill_no"])
    return rows


def invoice_csv(run_id: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(INVOICE_HEADER)
    for row in invoice_rows(run_id):
        writer.writerow([row[key] for key in INVOICE_HEADER])
    return buffer.getvalue()


def leg_rows(run_id: str) -> list[dict]:
    generation = _released(run_id)
    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    rows = []
    for row in store.charge_legs(run_id, generation):
        waybill = waybills.get(row["waybill_no"], {})
        rows.append({
            "waybill_no": row["waybill_no"],
            "leg_no": row["leg_no"],
            "carrier": row["carrier"],
            "service": row["service"],
            "zone": row["zone"],
            "version_id": row["version_id"],
            "billable_kg": row["billable_kg"],
            "steps": row["steps"],
            "base_amount": row["base_amount"],
            "step_amount": row["step_amount"],
            "remote_amount": row["remote_amount"],
            "fuel_amount": row["fuel_amount"],
            "insurance_amount": row["insurance_amount"],
            "contract_amount": row["contract_total"],
            "waiver_amount": row["waiver_amount"],
            "billed_amount": row["billed_total"],
            "reason_codes": row["codes"],
            "pickup_utc": waybill.get("pickup_utc", ""),
        })
    rows.sort(key=lambda row: (row["waybill_no"], row["leg_no"]))
    return rows


def legs_csv(run_id: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(LEG_HEADER)
    for row in leg_rows(run_id):
        writer.writerow([row[key] for key in LEG_HEADER])
    return buffer.getvalue()


def carrier_summary(run_id: str) -> list[dict]:
    """按段承运商汇总：让利归因到让出首重的那一段，便于与承运商结算对照。"""
    buckets: dict[str, dict] = {}
    currency = currency_of()
    for row in leg_rows(run_id):
        bucket = buckets.setdefault(row["carrier"], {
            "carrier": row["carrier"],
            "legs": 0,
            "waybills": set(),
            "contract": Decimal("0.00"),
            "waiver": Decimal("0.00"),
            "billed": Decimal("0.00"),
            "currency": currency,
        })
        bucket["legs"] += 1
        bucket["waybills"].add(row["waybill_no"])
        bucket["contract"] += Decimal(row["contract_amount"])
        bucket["waiver"] += Decimal(row["waiver_amount"])
        bucket["billed"] += Decimal(row["billed_amount"])
    summary = []
    for bucket in sorted(buckets.values(), key=lambda b: b["carrier"]):
        summary.append({
            "carrier": bucket["carrier"],
            "count": len(bucket["waybills"]),
            "legs": bucket["legs"],
            "contract": f"{bucket['contract']:,.2f}",
            "waiver": f"{bucket['waiver']:,.2f}",
            "total": f"{bucket['billed']:,.2f}",
            "currency": bucket["currency"],
        })
    return summary


def manifest_doc(run_id: str) -> dict:
    run = store.get_run(run_id) or {}
    generation = _released(run_id)
    # 从未发布过任何结果时，清单里的合计留空而不是写 0：0 会被误读成“算出应收 0 元”。
    unpublished = not generation
    report = reconcile_service.reconcile(run_id, generation) if generation else {
        "total": "", "conserved": False,
        "verdict": {"balanced": False, "broken": ["unpublished"], "count": 0},
        "contract_total": "", "waiver_total": "",
    }
    legs = leg_rows(run_id)
    doc = manifest.build(run, {
        "rows": {
            "input": int(run.get("rows_in") or 0),
            "billed": len(invoice_rows(run_id)),
            "quarantined": int(run.get("rows_quarantined") or 0),
        },
        "amount": {
            "total": report["total"],
            "conserved": report["conserved"],
            "contract_total": report["contract_total"],
            "waiver_total": report["waiver_total"],
        },
        "reconcile": report["verdict"],
        "generation": {
            "published": generation,
            "draft": store.draft_generation(run_id),
            "legs": len(legs),
            "interline_waybills": sum(1 for row in invoice_rows(run_id) if row["legs"] > 1),
        },
        "unpublished": unpublished,
        "rate_set_version": store.max_rate_set_version(),
        "params": {
            "profile": config.PROFILE, "quant": "0.01", "step": "0.5",
            "first_weight": "once-per-waybill",
            "interline_credit_rate": str(config.INTERLINE_CREDIT_RATE),
        },
        "export": {
            "invoice_rows": len(invoice_rows(run_id)),
            "leg_rows": len(legs),
        },
    })
    if unpublished:
        doc["amount_total"] = ""
    return doc


def manifest_json(run_id: str) -> str:
    return json.dumps(manifest_doc(run_id), ensure_ascii=False, indent=2, sort_keys=True, default=str)
