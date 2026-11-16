"""分段计价：每段按自己的运价收首重与续重，整票只向客户收一次首重。

承运合同是「每段各收首重」，客户协议是「整票只承担一次首重」。
做法：第一段照价收首重；后续段仍按合同算出首重价，但整票首重已由第一段承担，
后续段的这份首重不再向客户收取，且燃油只按实收运费计——两者合计就是让利。

三个金额互相可验：
- contract_total：按承运合同逐段相加、应付给承运商的钱
- waiver_amount：整票少收的金额 = 后续段首重价 + 随之少收的燃油
- total_amount：向客户开票的金额 = contract_total - waiver_amount（逐段差额相加，分毫不差）

每段开票口径的分项也自洽：基础（扣掉让掉的首重）+ 续重 + 偏远 + 燃油（按实收基础与续重）+ 保价
等于该段开票金额，因此「整票分项合计 = 应收」与「合同总额 - 让利 = 应收」同时成立。
"""

from __future__ import annotations

from decimal import Decimal

from app import config
from app.modules.m5_charges import addon, money, zone
from app.modules.m5_charges.money import q2

ZERO = Decimal("0.00")
FIRST = 1


def leg_pricing(card: dict, billable_kg: Decimal, steps: int,
                declared_value: Decimal, leg_no: int) -> dict:
    """单段按承运合同计价，不含让利。让利在整票层按段计算。"""
    first = zone.first_amount(card)
    step_part = zone.step_amount(steps, card)
    topup = zone.min_topup(first + step_part, card)
    base = q2(first + topup)
    remote = addon.remote_amount(card)
    fuel = addon.fuel_amount(base, step_part, card)
    insurance = addon.insurance_amount(declared_value, card)
    contract = money.total([base, step_part, remote, fuel, insurance])
    return {
        "leg_no": leg_no,
        "billable_kg": str(billable_kg),
        "steps": steps,
        "base_amount": str(base),
        "step_amount": str(step_part),
        "remote_amount": str(remote),
        "fuel_amount": str(fuel),
        "insurance_amount": str(insurance),
        "contract_total": str(contract),
        "min_topup": str(topup),
        "first_amount": str(first),
        "fuel_pct": str(card["fuel_pct"]),
    }


def _fuel_on(base: Decimal, step_part: Decimal, fuel_pct: str) -> Decimal:
    rate = Decimal(fuel_pct or "0")
    if rate <= 0:
        return ZERO
    return q2((q2(base) + q2(step_part)) * rate)


def apply_waiver(legs: list[dict]) -> Decimal:
    """给每段算让利并回写开票口径分项；返回整票让利（合同额与开票额之差）。

    第一段收首重；后续段的首重价（按 INTERLINE_CREDIT_RATE 折算）不再收，
    燃油随之只按实收的基础与续重计算。让利 = 合同额 - 开票额，逐段精确定义。
    """
    ordered = sorted(legs, key=lambda leg: leg["leg_no"])
    for item in ordered:
        item.setdefault("first_waiver", str(ZERO))
        item.setdefault("fuel_waiver", str(ZERO))
        if item.get("status") != "priced":
            item["base_billed"] = item.get("base_amount", str(ZERO))
            item["waiver_amount"] = str(ZERO)
            item["billed_total"] = str(q2(Decimal(item.get("contract_total") or ZERO)))
            continue
        base = q2(Decimal(item["base_amount"]))
        step_part = q2(Decimal(item["step_amount"]))
        fuel = q2(Decimal(item["fuel_amount"]))
        if item["leg_no"] == FIRST:
            item["base_billed"] = str(base)
            item["waiver_amount"] = str(ZERO)
            item["billed_total"] = str(q2(Decimal(item["contract_total"])))
            continue

        first_waiver = min(
            q2(Decimal(item["first_amount"]) * config.INTERLINE_CREDIT_RATE), base
        )
        base_billed = q2(base - first_waiver)
        fuel_billed = _fuel_on(base_billed, step_part, item.get("fuel_pct", "0"))
        fuel_waiver = q2(fuel - fuel_billed)
        billed = money.total([
            base_billed, step_part, q2(Decimal(item["remote_amount"])),
            fuel_billed, q2(Decimal(item["insurance_amount"])),
        ])
        item["base_billed"] = str(base_billed)
        item["fuel_billed"] = str(fuel_billed)
        item["first_waiver"] = str(first_waiver)
        item["fuel_waiver"] = str(fuel_waiver)
        item["billed_total"] = str(billed)
        item["waiver_amount"] = str(q2(Decimal(item["contract_total"]) - billed))
    for item in ordered:
        item.setdefault("fuel_billed", item.get("fuel_amount", str(ZERO)))
    return q2(sum((Decimal(item["waiver_amount"]) for item in ordered), ZERO))


def bill_legs(legs: list[dict]) -> Decimal:
    """逐段开票金额求和；返回整票应收。"""
    return q2(sum((q2(Decimal(item["billed_total"])) for item in legs), ZERO))
