"""M5 入口：分区附加页、账单计算与查询。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import store, web
from app.modules.m5_charges import service

router = APIRouter(tags=["charges"])


@router.get("/charges", response_class=HTMLResponse)
def page_charges(request: Request):
    context = web.shell(request, active="charges")
    run_id = context["run_id"]
    published_gen = store.published_generation(run_id) if run_id else 0
    draft_gen = store.draft_generation(run_id) if run_id else 0
    context.update({
        "lines": service.lines(run_id),
        "leg_lines": service.leg_lines(run_id),
        "issues": service.issues(run_id),
        "amounts": service.amounts(run_id) if run_id else {},
        "published_generation": published_gen,
        "draft_generation": draft_gen,
        "unpublished": bool(draft_gen and draft_gen != published_gen),
    })
    return web.page("charges.html", context)


@router.get("/api/charges")
def api_charges(run: str = ""):
    run_id = run or store.latest_run_id()
    return {
        "ok": True,
        "run_id": run_id,
        "amounts": service.amounts(run_id) if run_id else {},
        "lines": service.lines(run_id),
        "legs": service.leg_lines(run_id),
        "issues": service.issues(run_id),
    }


@router.post("/api/charges")
def api_compute(run: str = ""):
    run_id = run or store.latest_run_id()
    return {"ok": True, "result": service.compute_charges(run_id)}


@router.get("/api/charges/legs")
def api_legs(run: str = ""):
    run_id = run or store.latest_run_id()
    return {
        "ok": True,
        "run_id": run_id,
        "legs": service.leg_lines(run_id),
        "issues": service.issues(run_id),
    }
