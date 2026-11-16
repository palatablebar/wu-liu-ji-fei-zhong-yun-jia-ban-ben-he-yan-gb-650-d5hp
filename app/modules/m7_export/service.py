"""M7 编排：导出内容的组装与统计。交付只读已发布代。"""

from __future__ import annotations

from app import store
from app.modules.m6_reconcile import service as reconcile_service
from app.modules.m7_export import csv_out, invoice_out


def invoice_csv(run_id: str) -> str:
    return invoice_out.invoice_csv(run_id)


def legs_csv(run_id: str) -> str:
    return invoice_out.legs_csv(run_id)


def evidence_csv(run_id: str) -> str:
    return csv_out.csv_text(run_id, store.released_generation(run_id) or None)


def manifest_json(run_id: str) -> str:
    return invoice_out.manifest_json(run_id)


def overview(run_id: str) -> dict:
    generation = store.released_generation(run_id)
    invoices = invoice_out.invoice_rows(run_id)
    evidence = csv_out.rows(run_id, generation or None)
    doc = invoice_out.manifest_doc(run_id)
    report = reconcile_service.reconcile(run_id, generation) if generation else {}
    return {
        "run_id": run_id,
        "generation": generation,
        "draft_generation": store.draft_generation(run_id),
        "unpublished": bool(store.draft_generation(run_id)),
        "invoice_rows": len(invoices),
        "leg_rows": len(invoice_out.leg_rows(run_id)),
        "evidence_rows": len(evidence),
        "carriers": invoice_out.carrier_summary(run_id),
        "manifest": doc,
        "input_sha256": doc.get("input_sha256", ""),
        "amount_total": doc.get("amount_total", "0.00"),
        "contract_total": report.get("contract_total", "0.00"),
        "waiver_total": report.get("waiver_total", "0.00"),
        "issues": report.get("issues", []),
        "rows": doc.get("rows", {}),
    }
