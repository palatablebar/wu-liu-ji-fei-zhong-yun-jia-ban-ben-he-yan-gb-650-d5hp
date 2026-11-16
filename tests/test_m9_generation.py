"""结果代次：中断读取口径、旧库迁移、发布指针。"""

from __future__ import annotations

import sqlite3

import pytest

from app import config, store
from app.modules.m1_waybill import service as waybills
from app.modules.m4_weights import service as weights
from app.modules.m5_charges import service as charges
from app.modules.m6_reconcile import service as reconcile
from app.modules.m7_export import invoice_out


@pytest.fixture(scope="module")
def interline():
    """独立的联运运行，避免与其它测试文件共用 pipeline 时的代次互相影响。"""
    ingest = waybills.import_sample("waybills_interline.csv")
    run_id = ingest["run_id"]
    weights.compute_weights(run_id)
    charges.compute_charges(run_id)
    return {"run_id": run_id}


def test_interrupted_weights_run_keeps_published_view(interline):
    """只重算了计费重、账单还没算：页面与对账仍展示上一已发布版。"""
    run_id = interline["run_id"]
    published = store.published_generation(run_id)
    before = reconcile.reconcile(run_id)
    weights.compute_weights(run_id)

    assert store.draft_generation(run_id) > published
    assert store.read_generation(run_id) == published
    after = reconcile.reconcile(run_id)
    assert after["generation"] == published
    assert after["total"] == before["total"]
    assert after["verdict"]["balanced"] is True
    assert len(invoice_out.invoice_rows(run_id)) == 5


def test_export_ignores_unpublished_draft(interline):
    run_id = interline["run_id"]
    old = invoice_out.invoice_csv(run_id)
    weights.compute_weights(run_id)   # 制造未完成草稿
    assert invoice_out.invoice_csv(run_id) == old


def test_publish_pointer_only_moves_forward_on_success(interline):
    run_id = interline["run_id"]
    published = store.published_generation(run_id)
    assert published >= 1
    run = store.get_run(run_id)
    assert int(run["published_generation"]) == published
    assert run["published_at"]
    assert store.released_generation(run_id) == published


LEGACY_SCHEMA = """
CREATE TABLE runs (run_id TEXT PRIMARY KEY, input_name TEXT NOT NULL DEFAULT '',
  input_sha256 TEXT NOT NULL DEFAULT '', rules_version TEXT NOT NULL DEFAULT '',
  profile TEXT NOT NULL DEFAULT '', rows_in INTEGER NOT NULL DEFAULT 0,
  rows_ok INTEGER NOT NULL DEFAULT 0, rows_quarantined INTEGER NOT NULL DEFAULT 0,
  amount_total TEXT NOT NULL DEFAULT '0.00', rate_digest TEXT NOT NULL DEFAULT '',
  stages TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
CREATE TABLE waybills (run_id TEXT NOT NULL, line_no INTEGER NOT NULL,
  waybill_no TEXT NOT NULL, carrier TEXT NOT NULL, service TEXT NOT NULL,
  zone TEXT NOT NULL, pickup_utc TEXT NOT NULL, weight_kg TEXT NOT NULL,
  length_cm TEXT NOT NULL, width_cm TEXT NOT NULL, height_cm TEXT NOT NULL,
  declared_value TEXT NOT NULL, pieces INTEGER NOT NULL,
  codes TEXT NOT NULL DEFAULT '', PRIMARY KEY (run_id, waybill_no));
CREATE TABLE quarantine (run_id TEXT NOT NULL, line_no INTEGER NOT NULL,
  waybill_no TEXT NOT NULL DEFAULT '', code TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '', raw TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, line_no, code));
CREATE TABLE weight_lines (run_id TEXT NOT NULL, waybill_no TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '', actual_kg TEXT NOT NULL DEFAULT '0.000',
  volumetric_kg TEXT NOT NULL DEFAULT '0.000', source TEXT NOT NULL DEFAULT '',
  billable_kg TEXT NOT NULL DEFAULT '0.000', steps INTEGER NOT NULL DEFAULT 0,
  codes TEXT NOT NULL DEFAULT '', PRIMARY KEY (run_id, waybill_no));
CREATE TABLE charge_lines (run_id TEXT NOT NULL, waybill_no TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '', base_amount TEXT NOT NULL DEFAULT '0.00',
  step_amount TEXT NOT NULL DEFAULT '0.00', remote_amount TEXT NOT NULL DEFAULT '0.00',
  fuel_amount TEXT NOT NULL DEFAULT '0.00', insurance_amount TEXT NOT NULL DEFAULT '0.00',
  total_amount TEXT NOT NULL DEFAULT '0.00', codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no));
INSERT INTO runs (run_id, rows_in, rows_ok, rows_quarantined, amount_total, stages,
  created_at, updated_at) VALUES ('RUN-OLD', 2, 1, 1, '20.62', 'ingest,weights,charges',
  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
INSERT INTO waybills (run_id, line_no, waybill_no, carrier, service, zone, pickup_utc,
  weight_kg, length_cm, width_cm, height_cm, declared_value, pieces)
  VALUES ('RUN-OLD', 2, 'WB-1', 'SF', 'standard', 'Z1', '2026-04-05T02:00:00Z',
  '1.000', '10', '10', '10', '0', 1);
INSERT INTO weight_lines VALUES ('RUN-OLD', 'WB-1', 'SF-standard-Z1-v2', '1.000',
  '0.167', 'actual', '1.000', 0, '');
INSERT INTO charge_lines VALUES ('RUN-OLD', 'WB-1', 'SF-standard-Z1-v2', '19.00',
  '0.00', '0.00', '1.62', '0.00', '20.62', 'MIN_CHARGE_APPLIED');
"""


def test_legacy_database_migrates_to_generation_one(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(store.config, "DB_PATH", path)
    store.init_db()
    store.init_db()  # 幂等：重复启动不再迁移

    assert store.published_generation("RUN-OLD") == 1
    lines = store.charge_lines("RUN-OLD")
    assert len(lines) == 1
    # 老库没有让利概念：合同额等于当时开票额，让利 0，三数仍自洽
    assert lines[0]["contract_total"] == lines[0]["total_amount"]
    assert lines[0]["waiver_amount"] == "0.00"
    assert "route" in store.waybills("RUN-OLD")[0]
    assert [g["generation"] for g in store.list_generations("RUN-OLD")] == [1]
    assert store.list_generations("RUN-OLD")[0]["status"] == "published"
