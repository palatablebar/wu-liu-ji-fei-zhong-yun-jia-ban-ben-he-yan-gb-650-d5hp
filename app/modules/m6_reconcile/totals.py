"""合计：分项求和与守恒判定，全部走 Decimal 两位小数。

除既有五类分项外，补两套联运口径的合计：
- contract_total：按承运合同逐段相加（未减让利）
- waiver_total：整票只收一次首重让掉的金额
- billed_legs_total：各段按开票口径相加
"""

from __future__ import annotations

from decimal import Decimal

from app.modules.m5_charges.money import q2, total

PART_FIELDS = (
    ("base", "base_amount", "基础运费"),
    ("step", "step_amount", "续重运费"),
    ("remote", "remote_amount", "偏远附加"),
    ("fuel", "fuel_amount", "燃油附加"),
    ("insurance", "insurance_amount", "保价费"),
)


def parts_of(charge_rows: list[dict]) -> dict:
    return {
        key: total([Decimal(row[field]) for row in charge_rows])
        for key, field, _label in PART_FIELDS
    }


def grand_total(charge_rows: list[dict]) -> Decimal:
    return total([Decimal(row["total_amount"]) for row in charge_rows])


def contract_total(charge_rows: list[dict]) -> Decimal:
    return total([Decimal(row.get("contract_total") or "0.00") for row in charge_rows])


def waiver_total(charge_rows: list[dict]) -> Decimal:
    return total([Decimal(row.get("waiver_amount") or "0.00") for row in charge_rows])


def billed_legs_total(charge_legs: list[dict]) -> Decimal:
    return total([Decimal(row["billed_total"]) for row in charge_legs])


def part_labels() -> dict:
    return {key: label for key, _field, label in PART_FIELDS}


def conserved(charge_rows: list[dict]) -> bool:
    parts = parts_of(charge_rows)
    return q2(grand_total(charge_rows)) == q2(sum(parts.values(), Decimal("0")))


def step_total(charge_rows: list[dict]) -> int:
    return sum(1 for row in charge_rows if Decimal(row["step_amount"]) > 0)
