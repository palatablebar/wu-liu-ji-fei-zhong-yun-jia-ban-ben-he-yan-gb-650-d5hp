"""M6 入口：账页与对账接口，含发布动作与代次历史。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import store, web
from app.modules.m6_reconcile import service

router = APIRouter(tags=["reconcile"])


@router.get("/reconcile", response_class=HTMLResponse)
def page_reconcile(request: Request, generation: int = 0):
    context = web.shell(request, active="reconcile")
    run_id = context["run_id"]
    report = service.reconcile(run_id, generation or None) if run_id else {}
    context.update({
        "report": report,
        "issues": report.get("issues", []) if report else [],
        "generations": store.list_generations(run_id) if run_id else [],
        "published": store.published_generation(run_id) if run_id else 0,
    })
    return web.page("reconcile.html", context)


@router.get("/api/reconcile")
def api_reconcile(run: str = "", generation: int = 0):
    run_id = run or store.latest_run_id()
    return {"ok": True, "report": service.reconcile(run_id, generation or None)}


@router.get("/api/reconcile/published")
def api_reconcile_published(run: str = ""):
    run_id = run or store.latest_run_id()
    return {"ok": True, "report": service.published(run_id)}


@router.post("/api/reconcile/publish")
def api_publish(run: str = "", generation: int = 0):
    run_id = run or store.latest_run_id()
    result = service.publish(run_id, generation or None)
    return {"ok": True, "result": result}


@router.get("/api/generations")
def api_generations(run: str = ""):
    run_id = run or store.latest_run_id()
    return {
        "ok": True,
        "run_id": run_id,
        "published_generation": store.published_generation(run_id),
        "draft_generation": store.draft_generation(run_id),
        "generations": store.list_generations(run_id),
    }
