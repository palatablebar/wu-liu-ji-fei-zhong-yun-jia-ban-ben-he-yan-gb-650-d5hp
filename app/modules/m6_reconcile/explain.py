"""差异归因：把不平的账换成一句可复核的话，平的账只给结论。"""

from __future__ import annotations

from decimal import Decimal

BALANCED = "两侧相等"
ROW_TEMPLATE = "差 {delta} 行：采信与下游行数不等"
AMOUNT_TEMPLATE = "差 {delta} 元：分项之和与应收不等"
VERSION_TEMPLATE = "差 {delta} 行：有运单未落到单一版本"
WEIGHT_TEMPLATE = "差 {delta} 行：计费重来源分类不闭合"
LEG_TEMPLATE = "差 {delta} 元：整票应收与各段开票之和不等"
LEG_CONTRACT_TEMPLATE = "差 {delta} 元：合同总额减首重让利后与应收不等"
LEG_ISSUE_TEMPLATE = "有 {count} 处分段缺运价或路线缺段，最前一处是第 {leg_no} 段 {code}"

TEMPLATES = {
    "rows": ROW_TEMPLATE,
    "amount": AMOUNT_TEMPLATE,
    "version": VERSION_TEMPLATE,
    "weight": WEIGHT_TEMPLATE,
    "legs": LEG_TEMPLATE,
}


def note(account: dict) -> str:
    if account["balanced"]:
        return BALANCED
    if account["key"] == "legs":
        issues = account.get("issues") or []
        if issues:
            first = issues[0]
            return LEG_ISSUE_TEMPLATE.format(
                count=len(issues), leg_no=first["leg_no"] or "?", code=first["code"]
            )
        if account.get("contract_delta") not in (None, "0.00"):
            return LEG_CONTRACT_TEMPLATE.format(delta=account["contract_delta"])
    template = TEMPLATES.get(account["key"], "差 {delta}")
    return template.format(delta=account["delta"])


def annotated(accounts: list[dict]) -> list[dict]:
    for account in accounts:
        account["note"] = note(account)
    return accounts


def verdict(accounts: list[dict]) -> dict:
    broken = [account["key"] for account in accounts if not account["balanced"]]
    return {
        "balanced": not broken,
        "broken": broken,
        "count": len(accounts),
    }


def money_delta(value: Decimal) -> str:
    return f"{value:,.2f}"
