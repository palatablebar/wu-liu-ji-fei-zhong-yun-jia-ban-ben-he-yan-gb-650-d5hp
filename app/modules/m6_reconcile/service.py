"""M6 编排：取各代次的输入算账，核对通过才发布，失败保留旧版。

对账分两层：
1. 既有四账（运单行、金额、版本、计费重）
2. 分段账：整票应收 = 各段开票之和；合同总额 - 首重让利 = 应收

任何一账不平、或计价问题清单非空（某段缺运价/路线缺段）时不发布，
把问题连同段号返回给复核页；库里的旧已发布代完全不动。
"""

from __future__ import annotations

from app import store
from app.modules.m6_reconcile import compare, explain, totals


def _generation(run_id: str, generation: int | None) -> int:
    return int(generation or store.read_generation(run_id))


def reconcile(run_id: str, generation: int | None = None) -> dict:
    generation = _generation(run_id, generation)
    run = store.get_run(run_id) or {}
    quarantine_rows = store.quarantine(run_id)
    weight_rows = store.weight_lines(run_id, generation)
    charge_rows = store.charge_lines(run_id, generation)
    charge_legs = store.charge_legs(run_id, generation)
    issues = store.pricing_issues(run_id, generation)

    accounts = compare.accounts(run, quarantine_rows, weight_rows, charge_rows)
    # 分段账作为第五本账单独给出：整票应收必须等于各段开票之和，
    # 合同总额减首重让利也必须相等。既有四账的字段与顺序保持不变。
    leg_account = compare.leg_account(charge_rows, charge_legs, issues)
    explain.annotated(accounts)
    explain.annotated([leg_account])
    return {
        "run_id": run_id,
        "generation": generation,
        "published_generation": store.published_generation(run_id),
        "draft_generation": store.draft_generation(run_id),
        "accounts": accounts,
        "leg_account": leg_account,
        "verdict": explain.verdict(accounts + [leg_account]),
        "total": str(totals.grand_total(charge_rows)),
        "contract_total": str(totals.contract_total(charge_rows)),
        "waiver_total": str(totals.waiver_total(charge_rows)),
        "conserved": totals.conserved(charge_rows),
        "issues": issues,
        "blocked": bool(issues),
        "rows_in": int(run.get("rows_in") or 0),
        "rows_ok": int(run.get("rows_ok") or 0),
        "amount_total": str(run.get("amount_total") or "0.00"),
    }


def signature(run_id: str, generation: int) -> tuple:
    """一代结果的完整指纹：运单行、分段行、账单行、分段账单与问题。

    重跑同一批数据时，如果指纹与已发布代完全一致，就没有必要再发布一次。
    """
    def shape(items, keys):
        return tuple(tuple(str(row.get(key, "")) for key in keys) for row in items)

    return (
        shape(store.weight_lines(run_id, generation),
              ("waybill_no", "version_id", "actual_kg", "volumetric_kg", "source",
               "billable_kg", "steps", "codes")),
        shape(store.weight_legs(run_id, generation),
              ("waybill_no", "leg_no", "carrier", "service", "zone", "status",
               "version_id", "billable_kg", "steps", "codes")),
        shape(store.charge_lines(run_id, generation),
              ("waybill_no", "version_id", "base_amount", "step_amount", "remote_amount",
               "fuel_amount", "insurance_amount", "waiver_amount", "contract_total",
               "total_amount", "codes")),
        shape(store.charge_legs(run_id, generation),
              ("waybill_no", "leg_no", "carrier", "service", "zone", "version_id",
               "billable_kg", "steps", "base_amount", "step_amount", "remote_amount",
               "fuel_amount", "insurance_amount", "contract_total", "waiver_amount",
               "billed_total", "codes")),
        shape(store.pricing_issues(run_id, generation),
              ("waybill_no", "leg_no", "code", "detail")),
    )


def publish(run_id: str, generation: int | None = None) -> dict:
    """核对通过才发布：整代结果原子置为已发布，旧代转为 superseded 仍可回查。

    不通过则抛出 DomainError，草稿留在库里供复核，旧已发布代不受影响。
    若草稿与已发布代逐行一致，视为重跑同一批数据：丢弃草稿、沿用已发布代，
    不会生成第二份分段费用。
    """
    from app import manifest
    from app.errors import DomainError

    generation = int(generation or store.draft_generation(run_id))
    if not generation:
        raise DomainError("NO_DRAFT", "没有待发布的草案，请先计算账单", 409)

    previous = store.published_generation(run_id)
    if previous and previous != generation:
        if signature(run_id, generation) == signature(run_id, previous):
            store.discard_draft(run_id, generation)
            reused = reconcile(run_id, previous)
            return {
                "run_id": run_id,
                "generation": previous,
                "published": True,
                "reused": True,
                "amount_total": reused["total"],
                "contract_total": reused["contract_total"],
                "waiver_total": reused["waiver_total"],
                "verdict": reused["verdict"],
                "previous_generation": previous,
            }

    report = reconcile(run_id, generation)
    if report["blocked"]:
        first = report["issues"][0]
        raise DomainError(
            "PRICING_INCOMPLETE",
            "第 {0} 段 {1}：{2}；共 {3} 处问题，结果未发布，旧版仍可查".format(
                first["leg_no"] or "?", first["code"], first["detail"], len(report["issues"])
            ),
            409,
        )
    if not report["verdict"]["balanced"]:
        broken = ",".join(report["verdict"]["broken"])
        raise DomainError(
            "RECONCILE_FAILED",
            f"对账不平：{broken}；结果未发布，旧版仍可查",
            409,
        )
    now = manifest.utc_now_iso()
    store.publish_generation(
        run_id,
        generation,
        {
            "amount_total": report["total"],
            "contract_total": report["contract_total"],
            "waiver_total": report["waiver_total"],
            "published_generation": generation,
            "published_at": now,
            "updated_at": now,
        },
        {
            "rows_billed": sum(
                1 for row in store.charge_lines(run_id, generation)
            ),
            "legs_priced": len(store.charge_legs(run_id, generation)),
            "amount_total": report["total"],
            "contract_total": report["contract_total"],
            "waiver_total": report["waiver_total"],
            "reconcile_ok": 1,
            "published_at": now,
        },
    )
    return {
        "run_id": run_id,
        "generation": generation,
        "published": True,
        "amount_total": report["total"],
        "contract_total": report["contract_total"],
        "waiver_total": report["waiver_total"],
        "verdict": report["verdict"],
        "previous_generation": previous,
    }


def published(run_id: str) -> dict:
    """已发布结果的对账视图；从未发布时返回空结论。"""
    generation = store.published_generation(run_id)
    if not generation:
        return {
            "run_id": run_id, "generation": 0, "accounts": [],
            "verdict": {"balanced": False, "broken": ["unpublished"], "count": 0},
            "total": "0.00", "contract_total": "0.00", "waiver_total": "0.00",
            "conserved": False, "issues": [], "blocked": True,
        }
    return reconcile(run_id, generation)
