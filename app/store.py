"""SQLite 存取：运行清单、运价版本、运单、隔离区、计费重行、账单行、分段行。

只放数据访问，不放业务规则。每次调用开一个连接，用完即关。

结果行按「代次」隔离：一次计算先写一个草稿代，核对通过才把该代整体发布。
读取默认只看已发布代，因此中途失败或核对不平时，上一版结果仍完整可读；
重跑同一批数据只是在同一代内整体替换，不会叠加出重复的分段费用。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app import config

WEIGHT_LINES_DDL = """
CREATE TABLE IF NOT EXISTS weight_lines (
  run_id TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  waybill_no TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '',
  actual_kg TEXT NOT NULL DEFAULT '0.000',
  volumetric_kg TEXT NOT NULL DEFAULT '0.000',
  source TEXT NOT NULL DEFAULT '',
  billable_kg TEXT NOT NULL DEFAULT '0.000',
  steps INTEGER NOT NULL DEFAULT 0,
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, generation, waybill_no)
)
"""

CHARGE_LINES_DDL = """
CREATE TABLE IF NOT EXISTS charge_lines (
  run_id TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  waybill_no TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '',
  base_amount TEXT NOT NULL DEFAULT '0.00',
  step_amount TEXT NOT NULL DEFAULT '0.00',
  remote_amount TEXT NOT NULL DEFAULT '0.00',
  fuel_amount TEXT NOT NULL DEFAULT '0.00',
  insurance_amount TEXT NOT NULL DEFAULT '0.00',
  waiver_amount TEXT NOT NULL DEFAULT '0.00',
  contract_total TEXT NOT NULL DEFAULT '0.00',
  total_amount TEXT NOT NULL DEFAULT '0.00',
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, generation, waybill_no)
)
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  input_name TEXT NOT NULL DEFAULT '',
  input_sha256 TEXT NOT NULL DEFAULT '',
  rules_version TEXT NOT NULL DEFAULT '',
  profile TEXT NOT NULL DEFAULT '',
  rows_in INTEGER NOT NULL DEFAULT 0,
  rows_ok INTEGER NOT NULL DEFAULT 0,
  rows_quarantined INTEGER NOT NULL DEFAULT 0,
  amount_total TEXT NOT NULL DEFAULT '0.00',
  contract_total TEXT NOT NULL DEFAULT '0.00',
  waiver_total TEXT NOT NULL DEFAULT '0.00',
  rate_digest TEXT NOT NULL DEFAULT '',
  published_generation INTEGER NOT NULL DEFAULT 0,
  published_at TEXT NOT NULL DEFAULT '',
  stages TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS generations (
  run_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  input_sha256 TEXT NOT NULL DEFAULT '',
  rate_digest TEXT NOT NULL DEFAULT '',
  rules_version TEXT NOT NULL DEFAULT '',
  rows_billed INTEGER NOT NULL DEFAULT 0,
  legs_priced INTEGER NOT NULL DEFAULT 0,
  amount_total TEXT NOT NULL DEFAULT '0.00',
  contract_total TEXT NOT NULL DEFAULT '0.00',
  waiver_total TEXT NOT NULL DEFAULT '0.00',
  reconcile_ok INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  published_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, generation)
);
CREATE TABLE IF NOT EXISTS rate_sets (
  carrier TEXT PRIMARY KEY,
  rate_set_version INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_cards (
  carrier TEXT NOT NULL,
  service TEXT NOT NULL,
  zone TEXT NOT NULL,
  version_id TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT NOT NULL,
  first_weight_kg TEXT NOT NULL,
  first_price TEXT NOT NULL,
  step_weight_kg TEXT NOT NULL,
  step_price TEXT NOT NULL,
  min_charge TEXT NOT NULL,
  volume_divisor INTEGER NOT NULL,
  fuel_pct TEXT NOT NULL,
  remote_fee TEXT NOT NULL,
  insurance_pct TEXT NOT NULL,
  currency TEXT NOT NULL,
  rate_set_version INTEGER NOT NULL,
  PRIMARY KEY (carrier, service, zone, version_id)
);
CREATE TABLE IF NOT EXISTS waybills (
  run_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  waybill_no TEXT NOT NULL,
  carrier TEXT NOT NULL,
  service TEXT NOT NULL,
  zone TEXT NOT NULL,
  pickup_utc TEXT NOT NULL,
  weight_kg TEXT NOT NULL,
  length_cm TEXT NOT NULL,
  width_cm TEXT NOT NULL,
  height_cm TEXT NOT NULL,
  declared_value TEXT NOT NULL,
  pieces INTEGER NOT NULL,
  route TEXT NOT NULL DEFAULT '',
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS quarantine (
  run_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  waybill_no TEXT NOT NULL DEFAULT '',
  code TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  raw TEXT NOT NULL DEFAULT '',
  leg_no INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, line_no, code)
);
""" + WEIGHT_LINES_DDL + ";" + """
CREATE TABLE IF NOT EXISTS weight_legs (
  run_id TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  waybill_no TEXT NOT NULL,
  leg_no INTEGER NOT NULL,
  carrier TEXT NOT NULL DEFAULT '',
  service TEXT NOT NULL DEFAULT '',
  zone TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  version_id TEXT NOT NULL DEFAULT '',
  actual_kg TEXT NOT NULL DEFAULT '0.000',
  volumetric_kg TEXT NOT NULL DEFAULT '0.000',
  source TEXT NOT NULL DEFAULT '',
  billable_kg TEXT NOT NULL DEFAULT '0.000',
  steps INTEGER NOT NULL DEFAULT 0,
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, generation, waybill_no, leg_no)
);
""" + CHARGE_LINES_DDL + ";" + """
CREATE TABLE IF NOT EXISTS charge_legs (
  run_id TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  waybill_no TEXT NOT NULL,
  leg_no INTEGER NOT NULL,
  carrier TEXT NOT NULL DEFAULT '',
  service TEXT NOT NULL DEFAULT '',
  zone TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  version_id TEXT NOT NULL DEFAULT '',
  billable_kg TEXT NOT NULL DEFAULT '0.000',
  steps INTEGER NOT NULL DEFAULT 0,
  base_amount TEXT NOT NULL DEFAULT '0.00',
  step_amount TEXT NOT NULL DEFAULT '0.00',
  remote_amount TEXT NOT NULL DEFAULT '0.00',
  fuel_amount TEXT NOT NULL DEFAULT '0.00',
  insurance_amount TEXT NOT NULL DEFAULT '0.00',
  contract_total TEXT NOT NULL DEFAULT '0.00',
  waiver_amount TEXT NOT NULL DEFAULT '0.00',
  billed_total TEXT NOT NULL DEFAULT '0.00',
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, generation, waybill_no, leg_no)
);
CREATE TABLE IF NOT EXISTS pricing_issues (
  run_id TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  waybill_no TEXT NOT NULL DEFAULT '',
  leg_no INTEGER NOT NULL DEFAULT 0,
  code TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, generation, waybill_no, leg_no, code)
);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库补列与结果表换代。运价与运单原料保留，派生行迁到第 1 代。"""
    additions = {
        "runs": {
            "contract_total": "TEXT NOT NULL DEFAULT '0.00'",
            "waiver_total": "TEXT NOT NULL DEFAULT '0.00'",
            "published_generation": "INTEGER NOT NULL DEFAULT 0",
            "published_at": "TEXT NOT NULL DEFAULT ''",
        },
        "waybills": {"route": "TEXT NOT NULL DEFAULT ''"},
        "quarantine": {"leg_no": "INTEGER NOT NULL DEFAULT 0"},
    }
    for table, columns in additions.items():
        existing = _columns(conn, table)
        for name, ddl in columns.items():
            if existing and name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    migrated: list[str] = []
    for table, ddl in (("weight_lines", WEIGHT_LINES_DDL), ("charge_lines", CHARGE_LINES_DDL)):
        existing = _columns(conn, table)
        if existing and "generation" not in existing:
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            conn.execute(ddl)
            old = _columns(conn, f"{table}_old")
            copy = [c for c in old if c != "generation"]
            columns = ", ".join(["generation"] + copy)
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({columns}) "
                f"SELECT 1, {', '.join(copy)} FROM {table}_old"
            )
            conn.execute(f"DROP TABLE {table}_old")
            if table == "charge_lines":
                # 老库没有分段与让利的概念：合同额就等于当时开票额，让利为 0，
                # 这样「合同 − 让利 = 应收」在迁移后的旧代上也成立。
                conn.execute(
                    "UPDATE charge_lines SET contract_total = total_amount, "
                    "waiver_amount = '0.00' WHERE generation = 1"
                )
                migrated = [row["run_id"] for row in conn.execute(
                    "SELECT DISTINCT run_id FROM charge_lines WHERE generation = 1"
                )]
        elif existing:
            for name, ddl_type in (
                ("waiver_amount", "TEXT NOT NULL DEFAULT '0.00'"),
                ("contract_total", "TEXT NOT NULL DEFAULT '0.00'"),
            ):
                if name not in existing and table == "charge_lines":
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")

    # 老库已有的账单结果视为第 1 代已发布，页面与导出口径与迁移前一致。
    for run_id in migrated:
        conn.execute(
            "UPDATE runs SET published_generation = 1, published_at = updated_at "
            "WHERE run_id = ? AND published_generation = 0",
            (run_id,),
        )
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        exists = conn.execute(
            "SELECT 1 FROM generations WHERE run_id = ? AND generation = 1", (run_id,)
        ).fetchone()
        if row is None or exists:
            continue
        totals = conn.execute(
            "SELECT COUNT(*) AS billed, "
            "COALESCE(SUM(CAST(total_amount AS REAL)), 0) AS amount "
            "FROM charge_lines WHERE run_id = ? AND generation = 1",
            (run_id,),
        ).fetchone()
        legs = conn.execute(
            "SELECT COUNT(*) AS n FROM charge_legs WHERE run_id = ? AND generation = 1",
            (run_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO generations (run_id, generation, status, input_sha256, rate_digest, "
            "rules_version, rows_billed, legs_priced, amount_total, contract_total, "
            "waiver_total, reconcile_ok, notes, created_at, published_at) "
            "VALUES (?, 1, 'published', ?, ?, ?, ?, ?, ?, ?, '0.00', 0, ?, ?, ?)",
            (
                run_id, row["input_sha256"], row["rate_digest"], row["rules_version"],
                int(totals["billed"] or 0), int(legs["n"] or 0),
                f"{totals['amount']:.2f}", f"{totals['amount']:.2f}",
                "迁移前结果，未按分段规则复核", row["created_at"], row["updated_at"],
            ),
        )


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with session() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> dict | None:
    got = rows(sql, params)
    return got[0] if got else None


def execute(sql: str, params: tuple = ()) -> None:
    with session() as conn:
        conn.execute(sql, params)


def executemany(sql: str, seq) -> None:
    with session() as conn:
        conn.executemany(sql, list(seq))


# --- 运行清单 -------------------------------------------------------------

RUN_COLUMNS = (
    "input_name", "input_sha256", "rules_version", "profile",
    "rows_in", "rows_ok", "rows_quarantined", "amount_total",
    "contract_total", "waiver_total", "rate_digest", "published_generation",
    "published_at", "stages", "notes", "created_at", "updated_at",
)


def get_run(run_id: str) -> dict | None:
    return one("SELECT * FROM runs WHERE run_id = ?", (run_id,))


def list_runs(limit: int = 20) -> list[dict]:
    return rows("SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?", (limit,))


def upsert_run(run_id: str, data: dict) -> None:
    with session() as conn:
        existing = conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if existing:
            fields = [c for c in RUN_COLUMNS if c in data]
            assignments = ", ".join(f"{c} = ?" for c in fields)
            conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                tuple(data[c] for c in fields) + (run_id,),
            )
        else:
            payload = {c: data.get(c, "") for c in RUN_COLUMNS}
            for c in ("rows_in", "rows_ok", "rows_quarantined", "published_generation"):
                payload[c] = int(payload.get(c) or 0)
            cols = ("run_id",) + RUN_COLUMNS
            conn.execute(
                f"INSERT INTO runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                (run_id,) + tuple(payload[c] for c in RUN_COLUMNS),
            )


def add_stage(run_id: str, stage: str, updated_at: str) -> None:
    run = get_run(run_id)
    if not run:
        return
    parts = [p for p in (run.get("stages") or "").split(",") if p]
    if stage not in parts:
        parts.append(stage)
    execute(
        "UPDATE runs SET stages = ?, updated_at = ? WHERE run_id = ?",
        (",".join(parts), updated_at, run_id),
    )


# --- 结果代次 -------------------------------------------------------------

GENERATION_COLUMNS = (
    "status", "input_sha256", "rate_digest", "rules_version", "rows_billed",
    "legs_priced", "amount_total", "contract_total", "waiver_total",
    "reconcile_ok", "notes", "created_at", "published_at",
)


def generation_row(run_id: str, generation: int) -> dict | None:
    if not run_id or not generation:
        return None
    return one(
        "SELECT * FROM generations WHERE run_id = ? AND generation = ?",
        (run_id, generation),
    )


def list_generations(run_id: str) -> list[dict]:
    return rows(
        "SELECT * FROM generations WHERE run_id = ? ORDER BY generation DESC", (run_id,)
    )


def published_generation(run_id: str) -> int:
    run = get_run(run_id) or {}
    return int(run.get("published_generation") or 0)


def draft_generation(run_id: str) -> int:
    got = one(
        "SELECT MAX(generation) AS g FROM generations WHERE run_id = ? AND status = 'draft'",
        (run_id,),
    )
    return int(got["g"]) if got and got["g"] is not None else 0


def latest_generation(run_id: str) -> int:
    got = one(
        "SELECT MAX(generation) AS g FROM generations WHERE run_id = ?", (run_id,)
    )
    return int(got["g"]) if got and got["g"] is not None else 0


def read_generation(run_id: str) -> int:
    """对账/账单口径：优先看已进入计价阶段的草稿，否则看已发布代。

    计价阶段（写过账单行或问题清单）的草稿才是可复核对象；只算了计费重、
    账单还没生成的草稿不算完成，此时退回已发布代，避免把半成品当结果展示。
    """
    draft = draft_generation(run_id)
    if draft:
        started = one(
            "SELECT 1 AS hit FROM charge_lines WHERE run_id = ? AND generation = ? "
            "UNION ALL SELECT 1 FROM pricing_issues WHERE run_id = ? AND generation = ? "
            "LIMIT 1",
            (run_id, draft, run_id, draft),
        )
        if started:
            return draft
        published = published_generation(run_id)
        if published:
            return published
        return draft
    return published_generation(run_id) or latest_generation(run_id)


def weight_view_generation(run_id: str) -> int:
    """计费重口径：有草稿就显示草稿（刚算完的计费重），否则显示已发布代。"""
    return draft_generation(run_id) or published_generation(run_id) or latest_generation(run_id)


def released_generation(run_id: str) -> int:
    """对外口径：只认已发布代。导出与交付一律走这里，未核对的结果不外发。"""
    return published_generation(run_id)


def ensure_draft(run_id: str, **fields) -> int:
    """取该运行最新草稿代；没有就开新的一代。返回代次号。"""
    with session() as conn:
        draft = conn.execute(
            "SELECT generation FROM generations WHERE run_id = ? AND status = 'draft' "
            "ORDER BY generation DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if draft:
            return int(draft["generation"])
        row = conn.execute(
            "SELECT MAX(generation) AS g FROM generations WHERE run_id = ?", (run_id,)
        ).fetchone()
        generation = int(row["g"] or 0) + 1
        payload = {c: fields.get(c, "") for c in GENERATION_COLUMNS}
        payload["status"] = "draft"
        payload["rows_billed"] = int(payload.get("rows_billed") or 0)
        payload["legs_priced"] = int(payload.get("legs_priced") or 0)
        payload["reconcile_ok"] = int(payload.get("reconcile_ok") or 0)
        cols = ("run_id", "generation") + GENERATION_COLUMNS
        conn.execute(
            f"INSERT INTO generations ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            (run_id, generation) + tuple(payload[c] for c in GENERATION_COLUMNS),
        )
        return generation


def mark_generation(run_id: str, generation: int, data: dict) -> None:
    fields = [c for c in GENERATION_COLUMNS if c in data]
    if not fields:
        return
    assignments = ", ".join(f"{c} = ?" for c in fields)
    execute(
        f"UPDATE generations SET {assignments} WHERE run_id = ? AND generation = ?",
        tuple(data[c] for c in fields) + (run_id, generation),
    )


def discard_draft(run_id: str, generation: int) -> None:
    with session() as conn:
        conn.execute(
            "DELETE FROM generations WHERE run_id = ? AND generation = ? AND status = 'draft'",
            (run_id, generation),
        )
        for table in ("weight_lines", "weight_legs", "charge_lines", "charge_legs", "pricing_issues"):
            conn.execute(
                f"DELETE FROM {table} WHERE run_id = ? AND generation = ?",
                (run_id, generation),
            )


# --- 运价版本 -------------------------------------------------------------

CARD_COLUMNS = (
    "carrier", "service", "zone", "version_id", "effective_from", "effective_to",
    "first_weight_kg", "first_price", "step_weight_kg", "step_price", "min_charge",
    "volume_divisor", "fuel_pct", "remote_fee", "insurance_pct", "currency",
    "rate_set_version",
)


def replace_rate_cards(cards: list[dict], rate_set_version: int, updated_at: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM rate_cards")
        conn.executemany(
            f"INSERT INTO rate_cards ({', '.join(CARD_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in CARD_COLUMNS)})",
            [tuple(card[c] for c in CARD_COLUMNS) for card in cards],
        )
        carriers = sorted({card["carrier"] for card in cards})
        conn.execute("DELETE FROM rate_sets")
        conn.executemany(
            "INSERT INTO rate_sets (carrier, rate_set_version, updated_at) VALUES (?, ?, ?)",
            [(c, rate_set_version, updated_at) for c in carriers],
        )


def rate_cards() -> list[dict]:
    return rows("SELECT * FROM rate_cards ORDER BY carrier, service, zone, effective_from")


def rate_set_version(carrier: str) -> int:
    got = one("SELECT rate_set_version FROM rate_sets WHERE carrier = ?", (carrier,))
    return int(got["rate_set_version"]) if got else 0


def max_rate_set_version() -> int:
    got = one("SELECT MAX(rate_set_version) AS v FROM rate_sets")
    return int(got["v"]) if got and got["v"] is not None else 0


# --- 运单 / 隔离区 --------------------------------------------------------

def replace_waybills(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM waybills WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO waybills (run_id, line_no, waybill_no, carrier, service, zone, "
            "pickup_utc, weight_kg, length_cm, width_cm, height_cm, declared_value, pieces, "
            "route, codes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, it["line_no"], it["waybill_no"], it["carrier"], it["service"],
                    it["zone"], it["pickup_utc"], it["weight_kg"], it["length_cm"],
                    it["width_cm"], it["height_cm"], it["declared_value"], it["pieces"],
                    it.get("route", ""), it["codes"],
                )
                for it in items
            ],
        )


def waybills(run_id: str) -> list[dict]:
    return rows("SELECT * FROM waybills WHERE run_id = ? ORDER BY line_no", (run_id,))


def replace_quarantine(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM quarantine WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO quarantine (run_id, line_no, waybill_no, code, detail, raw, leg_no) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (run_id, it["line_no"], it["waybill_no"], it["code"], it["detail"], it["raw"],
                 int(it.get("leg_no") or 0))
                for it in items
            ],
        )


def quarantine(run_id: str) -> list[dict]:
    return rows(
        "SELECT * FROM quarantine WHERE run_id = ? ORDER BY line_no, code", (run_id,)
    )


# --- 计费重行（按代次） ---------------------------------------------------

def replace_weight_generation(run_id: str, generation: int, lines: list[dict],
                              legs: list[dict]) -> None:
    """一代的计费重整体替换：先删该代全部行再写入，重跑不会叠加。"""
    with session() as conn:
        conn.execute(
            "DELETE FROM weight_lines WHERE run_id = ? AND generation = ?",
            (run_id, generation),
        )
        conn.execute(
            "DELETE FROM weight_legs WHERE run_id = ? AND generation = ?",
            (run_id, generation),
        )
        conn.executemany(
            "INSERT INTO weight_lines (run_id, generation, waybill_no, version_id, actual_kg, "
            "volumetric_kg, source, billable_kg, steps, codes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, generation, it["waybill_no"], it["version_id"], it["actual_kg"],
                    it["volumetric_kg"], it["source"], it["billable_kg"], it["steps"],
                    it.get("codes", ""),
                )
                for it in lines
            ],
        )
        conn.executemany(
            "INSERT INTO weight_legs (run_id, generation, waybill_no, leg_no, carrier, service, "
            "zone, status, version_id, actual_kg, volumetric_kg, source, billable_kg, steps, codes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, generation, it["waybill_no"], it["leg_no"], it["carrier"],
                    it["service"], it["zone"], it["status"], it["version_id"],
                    it["actual_kg"], it["volumetric_kg"], it["source"], it["billable_kg"],
                    it["steps"], it.get("codes", ""),
                )
                for it in legs
            ],
        )


def weight_lines(run_id: str, generation: int | None = None) -> list[dict]:
    generation = read_generation(run_id) if generation is None else generation
    if not generation:
        return []
    return rows(
        "SELECT * FROM weight_lines WHERE run_id = ? AND generation = ? ORDER BY waybill_no",
        (run_id, generation),
    )


def weight_legs(run_id: str, generation: int | None = None) -> list[dict]:
    generation = read_generation(run_id) if generation is None else generation
    if not generation:
        return []
    return rows(
        "SELECT * FROM weight_legs WHERE run_id = ? AND generation = ? "
        "ORDER BY waybill_no, leg_no",
        (run_id, generation),
    )


def replace_charge_generation(run_id: str, generation: int, lines: list[dict],
                              legs: list[dict], issues: list[dict]) -> None:
    with session() as conn:
        conn.execute(
            "DELETE FROM charge_lines WHERE run_id = ? AND generation = ?",
            (run_id, generation),
        )
        conn.execute(
            "DELETE FROM charge_legs WHERE run_id = ? AND generation = ?",
            (run_id, generation),
        )
        conn.execute(
            "DELETE FROM pricing_issues WHERE run_id = ? AND generation = ?",
            (run_id, generation),
        )
        conn.executemany(
            "INSERT INTO charge_lines (run_id, generation, waybill_no, version_id, base_amount, "
            "step_amount, remote_amount, fuel_amount, insurance_amount, waiver_amount, "
            "contract_total, total_amount, codes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, generation, it["waybill_no"], it["version_id"], it["base_amount"],
                    it["step_amount"], it["remote_amount"], it["fuel_amount"],
                    it["insurance_amount"], it["waiver_amount"], it["contract_total"],
                    it["total_amount"], it["codes"],
                )
                for it in lines
            ],
        )
        conn.executemany(
            "INSERT INTO charge_legs (run_id, generation, waybill_no, leg_no, carrier, service, "
            "zone, status, version_id, billable_kg, steps, base_amount, step_amount, remote_amount, "
            "fuel_amount, insurance_amount, contract_total, waiver_amount, billed_total, codes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, generation, it["waybill_no"], it["leg_no"], it["carrier"],
                    it["service"], it["zone"], it["status"], it["version_id"],
                    it["billable_kg"], it["steps"], it["base_amount"], it["step_amount"],
                    it["remote_amount"], it["fuel_amount"], it["insurance_amount"],
                    it["contract_total"], it["waiver_amount"], it["billed_total"],
                    it.get("codes", ""),
                )
                for it in legs
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO pricing_issues "
            "(run_id, generation, waybill_no, leg_no, code, detail) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run_id, generation, it["waybill_no"], int(it.get("leg_no") or 0),
                 it["code"], it["detail"])
                for it in issues
            ],
        )


def charge_lines(run_id: str, generation: int | None = None) -> list[dict]:
    generation = read_generation(run_id) if generation is None else generation
    if not generation:
        return []
    return rows(
        "SELECT * FROM charge_lines WHERE run_id = ? AND generation = ? ORDER BY waybill_no",
        (run_id, generation),
    )


def charge_legs(run_id: str, generation: int | None = None) -> list[dict]:
    generation = read_generation(run_id) if generation is None else generation
    if not generation:
        return []
    return rows(
        "SELECT * FROM charge_legs WHERE run_id = ? AND generation = ? "
        "ORDER BY waybill_no, leg_no",
        (run_id, generation),
    )


def pricing_issues(run_id: str, generation: int | None = None) -> list[dict]:
    generation = read_generation(run_id) if generation is None else generation
    if not generation:
        return []
    return rows(
        "SELECT * FROM pricing_issues WHERE run_id = ? AND generation = ? "
        "ORDER BY waybill_no, leg_no, code",
        (run_id, generation),
    )


def publish_generation(run_id: str, generation: int, run_fields: dict,
                       generation_fields: dict) -> None:
    """原子发布：更新代次状态、运行合计与发布指针在同一个事务里完成。

    发布前先把旧的已发布代标记为 superseded，旧行本身不删，仍可按代次回查。
    """
    with session() as conn:
        conn.execute(
            "UPDATE generations SET status = 'superseded' "
            "WHERE run_id = ? AND status = 'published' AND generation <> ?",
            (run_id, generation),
        )
        conn.execute(
            "UPDATE generations SET status = 'published' WHERE run_id = ? AND generation = ?",
            (run_id, generation),
        )
        fields = [c for c in GENERATION_COLUMNS if c in generation_fields]
        if fields:
            conn.execute(
                f"UPDATE generations SET {', '.join(f'{c} = ?' for c in fields)} "
                "WHERE run_id = ? AND generation = ?",
                tuple(generation_fields[c] for c in fields) + (run_id, generation),
            )
        run_columns = [c for c in RUN_COLUMNS if c in run_fields]
        if run_columns:
            conn.execute(
                f"UPDATE runs SET {', '.join(f'{c} = ?' for c in run_columns)} WHERE run_id = ?",
                tuple(run_fields[c] for c in run_columns) + (run_id,),
            )


def latest_run_id() -> str:
    got = one("SELECT run_id FROM runs ORDER BY created_at DESC, run_id DESC LIMIT 1")
    return got["run_id"] if got else ""
