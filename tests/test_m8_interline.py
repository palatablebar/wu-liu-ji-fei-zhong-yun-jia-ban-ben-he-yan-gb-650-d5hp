"""联运：路线解析、分段计费、整票一次首重、分批发布与重跑幂等。"""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal

import pytest

from fixtures import samples
from fixtures.expected import HEADER

from app import store
from app.errors import DomainError
from app.modules.m1_waybill import route
from app.modules.m1_waybill import service as waybills
from app.modules.m4_weights import service as weights
from app.modules.m5_charges import service as charges
from app.modules.m6_reconcile import service as reconcile
from app.modules.m7_export import invoice_out
from app.modules.m7_export import service as export

INTERLINE_SAMPLE = "waybills_interline.csv"
INTERLINE_HEADER = HEADER + ",route"


def _read(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


@pytest.fixture(scope="module")
def interline(pipeline):
    ingest = waybills.import_sample(INTERLINE_SAMPLE)
    run_id = ingest["run_id"]
    weights.compute_weights(run_id)
    result = charges.compute_charges(run_id)
    return {"run_id": run_id, "ingest": ingest, "charges": result}


# --- 路线解析 -------------------------------------------------------------

def test_route_parses_two_legs():
    legs, problems = route.parse("SF/standard/Z1>JD/standard/Z2", "SF", "standard", "Z1")
    assert problems == []
    assert [(l["leg_no"], l["carrier"], l["zone"]) for l in legs] == [
        (1, "SF", "Z1"), (2, "JD", "Z2")
    ]


def test_empty_route_is_single_leg():
    legs, problems = route.parse("", "SF", "standard", "Z1")
    assert problems == []
    assert len(legs) == 1
    assert legs[0]["leg_no"] == 1


def test_route_missing_segment_points_at_leg():
    legs, problems = route.parse("SF/standard/Z1>JD/standard/Z2>>YT/standard/Z1", "", "", "")
    assert [p["code"] for p in problems] == ["ROUTE_GAP"]
    assert problems[0]["leg_no"] == 3


def test_route_short_leg_is_gap():
    _legs, problems = route.parse("SF/standard/Z1>JD/standard", "SF", "standard", "Z1")
    assert problems[0]["code"] == "ROUTE_GAP"
    assert problems[0]["leg_no"] == 2


def test_route_duplicate_consecutive_leg_rejected():
    _legs, problems = route.parse("SF/standard/Z1>SF/standard/Z1", "SF", "standard", "Z1")
    assert problems[0]["code"] == "ROUTE_DUP_LEG"


def test_route_arrow_separator_accepted():
    legs, problems = route.parse("SF/standard/Z1 → JD/standard/Z2", "", "", "")
    assert problems == []
    assert len(legs) == 2


# --- 采信阶段：缺段与缺运价都要指出段号 ------------------------------------

def test_missing_leg_and_missing_rate_carry_leg_no():
    raw = samples.csv_bytes([
        samples.row("WB-7001") + ",SF/standard/Z1>JD/standard",       # 缺字段
        samples.row("WB-7002") + ",SF/standard/Z1>JD/standard/Z9",    # 缺运价
    ], header=INTERLINE_HEADER)
    ingest = waybills.import_bytes(raw, "route_bad.csv")
    assert ingest["rows_ok"] == 0
    rows = {item["waybill_no"]: item for item in store.quarantine(ingest["run_id"])}
    assert rows["WB-7001"]["code"] == "ROUTE_GAP"
    assert rows["WB-7001"]["leg_no"] == 2
    assert rows["WB-7002"]["code"] == "RATE_MISSING"
    assert rows["WB-7002"]["leg_no"] == 2
    assert "第 2 段" in rows["WB-7002"]["detail"]


# --- 分段计价 -------------------------------------------------------------

def test_interline_sample_counts(interline):
    assert interline["ingest"]["rows_in"] == 8
    assert interline["ingest"]["rows_ok"] == 5
    assert interline["ingest"]["rows_quarantined"] == 3


def test_interline_legs_priced_per_segment(interline):
    run_id = interline["run_id"]
    legs = store.charge_legs(run_id)
    assert len(legs) == 10
    by_waybill = {}
    for row in legs:
        by_waybill.setdefault(row["waybill_no"], []).append(row)
    assert len(by_waybill["WB-4003"]) == 3
    # 每段命中自己的版本
    assert by_waybill["WB-4001"][0]["version_id"] == "SF-standard-Z1-v2"
    assert by_waybill["WB-4001"][1]["version_id"] == "JD-standard-Z2-v2"


def test_first_leg_keeps_first_weight_later_legs_waive(interline):
    run_id = interline["run_id"]
    legs = {row["leg_no"]: row for row in store.charge_legs(run_id)
            if row["waybill_no"] == "WB-4001"}
    assert Decimal(legs[1]["waiver_amount"]) == Decimal("0.00")
    assert Decimal(legs[1]["base_amount"]) == Decimal("13.00")
    # 第二段首重价 16.00 全额让掉，基础运费抵到 0；燃油随之只按续重计，
    # 让利 = 合同额 - 开票额 = 16.00 + 少收的燃油 1.36
    assert Decimal(legs[2]["base_amount"]) == Decimal("0.00")
    assert Decimal(legs[2]["fuel_amount"]) == Decimal("2.18")
    assert Decimal(legs[2]["contract_total"]) == Decimal("51.14")
    assert Decimal(legs[2]["waiver_amount"]) == Decimal("17.36")
    assert Decimal(legs[2]["billed_total"]) == Decimal("33.78")


def test_waybill_total_equals_sum_of_legs(interline):
    run_id = interline["run_id"]
    totals = {}
    for row in store.charge_legs(run_id):
        totals[row["waybill_no"]] = totals.get(row["waybill_no"], Decimal("0")) + Decimal(
            row["billed_total"]
        )
    for line in store.charge_lines(run_id):
        assert totals[line["waybill_no"]] == Decimal(line["total_amount"]), line["waybill_no"]


def test_each_leg_parts_sum_to_contract_minus_waiver(interline):
    """每段可独立复核：分项之和 = 合同额 − 让利 = 开票额。"""
    for row in store.charge_legs(interline["run_id"]):
        parts = sum(
            (Decimal(row[key]) for key in (
                "base_amount", "step_amount", "remote_amount",
                "fuel_amount", "insurance_amount",
            )),
            Decimal("0.00"),
        )
        assert parts == Decimal(row["billed_total"]), (row["waybill_no"], row["leg_no"])
        assert Decimal(row["contract_total"]) - Decimal(row["waiver_amount"]) == parts


def test_contract_minus_waiver_equals_total(interline):
    run_id = interline["run_id"]
    amounts = charges.amounts(run_id)
    assert str(amounts["contract"]) == "353.23"
    assert str(amounts["waiver"]) == "91.01"
    assert str(amounts["total"]) == "262.22"
    assert amounts["contract_conserved"] is True
    assert amounts["legs_sum"] == amounts["total"]


def test_interline_result_published_and_five_accounts_balanced(interline):
    report = reconcile.reconcile(interline["run_id"])
    assert report["published_generation"] == interline["charges"]["generation"]
    assert report["verdict"]["balanced"] is True
    assert report["leg_account"]["balanced"] is True
    assert report["leg_account"]["contract_delta"] == "0.00"
    assert report["issues"] == []


# --- 重跑幂等：不重复生成分段费用 ------------------------------------------

def test_rerun_does_not_duplicate_leg_charges(interline):
    run_id = interline["run_id"]
    before = (len(store.charge_legs(run_id)), len(store.charge_lines(run_id)),
              store.get_run(run_id)["amount_total"])
    weights.compute_weights(run_id)
    again = charges.compute_charges(run_id)
    after = (len(store.charge_legs(run_id)), len(store.charge_lines(run_id)),
             store.get_run(run_id)["amount_total"])
    assert before == after
    assert again["amount_total"] == "262.22"
    assert again["reused"] is True
    assert again["published"] is True


def test_rerun_reuses_published_generation(interline):
    run_id = interline["run_id"]
    published = store.published_generation(run_id)
    weights.compute_weights(run_id)
    charges.compute_charges(run_id)
    assert store.published_generation(run_id) == published
    assert store.draft_generation(run_id) == 0
    statuses = [g["status"] for g in store.list_generations(run_id)]
    assert statuses.count("published") == 1
    assert "draft" not in statuses


# --- 核对不通过：整代不发布，旧版结果完整保留 ------------------------------

def test_blocked_rerun_keeps_previous_published_result(interline):
    run_id = interline["run_id"]
    published = store.published_generation(run_id)
    old_invoice = invoice_out.invoice_csv(run_id)
    old_legs = invoice_out.legs_csv(run_id)
    old_total = store.get_run(run_id)["amount_total"]

    # 抽掉第二段用到的运价版本，制造「第 2 段没有有效运价」
    saved = store.rate_cards()
    trimmed = [card for card in saved if not (
        card["carrier"] == "JD" and card["service"] == "standard" and card["zone"] == "Z2"
    )]
    try:
        store.replace_rate_cards(trimmed, 3, "now")
        weights.compute_weights(run_id)
        result = charges.compute_charges(run_id)

        assert result["published"] is False
        assert result["blocked"]["code"] == "PRICING_INCOMPLETE"
        assert "第 2 段" in result["blocked"]["detail"]

        report = reconcile.reconcile(run_id)
        assert report["blocked"] is True
        assert [i["leg_no"] for i in report["issues"]] == [2, 1]
        assert report["leg_account"]["balanced"] is False
        assert "第 2 段" in report["leg_account"]["note"]

        # 旧版结果与导出保持原样
        assert store.published_generation(run_id) == published
        assert store.get_run(run_id)["amount_total"] == old_total
        assert invoice_out.invoice_csv(run_id) == old_invoice
        assert invoice_out.legs_csv(run_id) == old_legs
        assert len(invoice_out.invoice_rows(run_id)) == 5

        with pytest.raises(DomainError) as excinfo:
            reconcile.publish(run_id)
        assert excinfo.value.code == "PRICING_INCOMPLETE"
    finally:
        store.replace_rate_cards(saved, 3, "now")


def test_recovered_rerun_returns_to_published_state(interline):
    """运价补齐后重跑，结果与已发布代逐行一致：丢弃草稿、沿用已发布代。"""
    run_id = interline["run_id"]
    old_published = store.published_generation(run_id)
    weights.compute_weights(run_id)
    result = charges.compute_charges(run_id)
    assert result["published"] is True       # 结果仍是已发布的那一版
    assert result["reused"] is True          # 没有新写一代
    assert result["generation"] == old_published
    assert store.published_generation(run_id) == old_published
    assert store.draft_generation(run_id) == 0
    assert store.get_run(run_id)["amount_total"] == "262.22"
    assert reconcile.reconcile(run_id)["verdict"]["balanced"] is True


# --- 导出：分段可回查 -----------------------------------------------------

def test_legs_csv_has_one_row_per_leg(interline):
    rows = _read(export.legs_csv(interline["run_id"]))
    assert len(rows) == 10
    assert list(rows[0]) == invoice_out.LEG_HEADER
    first = [row for row in rows if row["waybill_no"] == "WB-4001"]
    assert [row["leg_no"] for row in first] == ["1", "2"]
    assert first[1]["waiver_amount"] == "17.36"
    assert "FIRST_WEIGHT_WAIVED" in first[1]["reason_codes"]


def test_invoice_csv_exposes_contract_and_waiver(interline):
    rows = _read(export.invoice_csv(interline["run_id"]))
    assert len(rows) == 5
    assert "contract_amount" in rows[0] and "waiver_amount" in rows[0]
    total = sum(Decimal(row["total_amount"]) for row in rows)
    contract = sum(Decimal(row["contract_amount"]) for row in rows)
    waiver = sum(Decimal(row["waiver_amount"]) for row in rows)
    assert str(total) == "262.22"
    assert contract - waiver == total


def test_manifest_reports_interline_generation(interline):
    doc = json.loads(export.manifest_json(interline["run_id"]))
    assert doc["generation"]["legs"] == 10
    assert doc["generation"]["interline_waybills"] == 4
    assert doc["amount"]["contract_total"] == "353.23"
    assert doc["amount"]["waiver_total"] == "91.01"
    assert doc["params"]["first_weight"] == "once-per-waybill"


def test_carrier_summary_attributes_waiver_to_leg(interline):
    summary = {row["carrier"]: row for row in invoice_out.carrier_summary(interline["run_id"])}
    assert summary["JD"]["waiver"] == "39.16"
    assert summary["YT"]["waiver"] == "30.05"
    assert summary["SF"]["waiver"] == "21.80"
    billed = sum(Decimal(row["total"].replace(",", "")) for row in summary.values())
    assert str(billed) == "262.22"


# --- 多段与边界 -----------------------------------------------------------

def test_three_leg_route_waives_every_later_leg():
    body = (
        INTERLINE_HEADER + "\n"
        "WB-7101,SF,standard,Z1,2026-04-05T09:00:00+08:00,2,kg,20,20,20,cm,0,1,"
        "SF/standard/Z1>JD/standard/Z2>YT/economy/Z2\n"
    )
    ingest = waybills.import_bytes(body.encode(), "three_legs.csv")
    run_id = ingest["run_id"]
    weights.compute_weights(run_id)
    result = charges.compute_charges(run_id)
    assert result["published"] is True
    legs = store.charge_legs(run_id)
    assert [row["leg_no"] for row in legs] == [1, 2, 3]
    assert [Decimal(row["waiver_amount"]) > 0 for row in legs] == [False, True, True]
    assert reconcile.reconcile(run_id)["leg_account"]["contract_delta"] == "0.00"


def test_trailing_separator_is_route_gap():
    body = (
        INTERLINE_HEADER + "\n"
        "WB-7102,SF,standard,Z1,2026-04-05T09:00:00+08:00,2,kg,20,20,20,cm,0,1,"
        "SF/standard/Z1>JD/standard/Z2>\n"
    )
    ingest = waybills.import_bytes(body.encode(), "trailing.csv")
    assert ingest["rows_ok"] == 0
    item = store.quarantine(ingest["run_id"])[0]
    assert item["code"] == "ROUTE_GAP"
    assert item["leg_no"] == 3


def test_single_leg_route_is_not_interline():
    body = (
        INTERLINE_HEADER + "\n"
        "WB-7103,SF,standard,Z1,2026-04-05T09:00:00+08:00,2,kg,20,20,20,cm,0,1,"
        "SF/standard/Z1\n"
    )
    ingest = waybills.import_bytes(body.encode(), "single.csv")
    assert ingest["rows_ok"] == 0
    item = store.quarantine(ingest["run_id"])[0]
    assert item["code"] == "ROUTE_GAP"
    assert item["leg_no"] == 2
