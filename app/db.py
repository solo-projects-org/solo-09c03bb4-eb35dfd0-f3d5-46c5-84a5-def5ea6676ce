"""SQLite 持久化：书目记录、采集进度、采集运行历史。

所有写操作在单连接单事务内完成，保证“整批采完才提交”。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

DB_PATH = os.environ.get("APP_DB_PATH", "/data/harvester.db")

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS bib_records (
    identifier      TEXT PRIMARY KEY,            -- OAI 记录唯一标识
    datestamp       TEXT NOT NULL,               -- OAI header datestamp
    status          TEXT NOT NULL,               -- active / withdrawn
    title           TEXT,
    creator         TEXT,
    publisher       TEXT,
    subject         TEXT,
    description     TEXT,
    date            TEXT,
    type            TEXT,
    format          TEXT,
    identifier_dc   TEXT,
    source          TEXT,
    language        TEXT,
    relation        TEXT,
    coverage        TEXT,
    rights          TEXT,
    raw_dc          TEXT NOT NULL,               -- 原始 Dublin Core JSON
    write_guarded   INTEGER NOT NULL DEFAULT 0,  -- 1=元数据已被清理，补采按库内 datestamp 守卫
    cleared_at      TEXT,                        -- 最近一次元数据清理时间（UTC）
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harvest_progress (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_until      TEXT NOT NULL,               -- 上次成功采集的截止日（含）
    last_harvest_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harvest_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    from_date       TEXT NOT NULL,
    until_date      TEXT NOT NULL,
    status          TEXT NOT NULL,               -- success / failed
    pages           INTEGER NOT NULL DEFAULT 0,
    fetched         INTEGER NOT NULL DEFAULT 0,
    inserted        INTEGER NOT NULL DEFAULT 0,
    updated         INTEGER NOT NULL DEFAULT 0,
    withdrawn       INTEGER NOT NULL DEFAULT 0,
    skipped         INTEGER NOT NULL DEFAULT 0,  -- 守卫规则忽略的更早/同日冲突记录
    retries         INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db(db_path: str | None = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        # 旧库迁移：补清理功能引入的列（SQLite 不支持 IF NOT EXISTS ADD COLUMN）
        bib_cols = _column_names(conn, "bib_records")
        if "write_guarded" not in bib_cols:
            conn.execute(
                "ALTER TABLE bib_records "
                "ADD COLUMN write_guarded INTEGER NOT NULL DEFAULT 0"
            )
        if "cleared_at" not in bib_cols:
            conn.execute("ALTER TABLE bib_records ADD COLUMN cleared_at TEXT")
        run_cols = _column_names(conn, "harvest_runs")
        if "skipped" not in run_cols:
            conn.execute(
                "ALTER TABLE harvest_runs "
                "ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务上下文：异常回滚，正常时由调用方负责 commit。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    except Exception:
        conn.rollback()
        raise


def get_last_until(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT last_until FROM harvest_progress WHERE id = 1"
    ).fetchone()
    return row["last_until"] if row else None


def advance_progress(conn: sqlite3.Connection, until_date: str) -> None:
    """整批成功后推进截止日（单行 upsert）。"""
    conn.execute(
        """
        INSERT INTO harvest_progress (id, last_until, last_harvest_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_until = excluded.last_until,
            last_harvest_at = excluded.last_harvest_at
        """,
        (until_date, utc_now_iso()),
    )


def start_run(conn: sqlite3.Connection, from_date: str, until_date: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO harvest_runs (started_at, from_date, until_date, status)
        VALUES (?, ?, ?, 'running')
        """,
        (utc_now_iso(), from_date, until_date),
    )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str,
               pages: int, fetched: int, inserted: int, updated: int,
               withdrawn: int, skipped: int, retries: int,
               error: str | None) -> None:
    conn.execute(
        """
        UPDATE harvest_runs SET
            finished_at = ?, status = ?, pages = ?, fetched = ?,
            inserted = ?, updated = ?, withdrawn = ?, skipped = ?,
            retries = ?, error = ?
        WHERE id = ?
        """,
        (utc_now_iso(), status, pages, fetched, inserted, updated,
         withdrawn, skipped, retries, error, run_id),
    )


def upsert_bib_record(conn: sqlite3.Connection, *, identifier: str,
                      datestamp: str, fields: dict, raw_dc: str) -> str:
    """按标识 upsert 书目；返回 'inserted' 或 'updated'。"""
    existing = conn.execute(
        "SELECT identifier FROM bib_records WHERE identifier = ?",
        (identifier,),
    ).fetchone()
    now = utc_now_iso()
    params = (
        identifier, datestamp, "active",
        fields.get("title"), fields.get("creator"), fields.get("publisher"),
        fields.get("subject"), fields.get("description"), fields.get("date"),
        fields.get("type"), fields.get("format"), fields.get("identifier"),
        fields.get("source"), fields.get("language"), fields.get("relation"),
        fields.get("coverage"), fields.get("rights"), raw_dc, now,
    )
    if existing is None:
        conn.execute(
            """
            INSERT INTO bib_records (
                identifier, datestamp, status, title, creator, publisher,
                subject, description, date, type, format, identifier_dc,
                source, language, relation, coverage, rights, raw_dc, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        return "inserted"
    conn.execute(
        """
        UPDATE bib_records SET
            datestamp = ?, status = ?, title = ?, creator = ?, publisher = ?,
            subject = ?, description = ?, date = ?, type = ?, format = ?,
            identifier_dc = ?, source = ?, language = ?, relation = ?,
            coverage = ?, rights = ?, raw_dc = ?, updated_at = ?
        WHERE identifier = ?
        """,
        params[1:] + (identifier,),
    )
    return "updated"


def withdraw_bib_record(conn: sqlite3.Connection, *, identifier: str,
                        datestamp: str) -> bool:
    """遇删除标记则撤下（标记 withdrawn）。不存在返回 False。

    不改动 write_guarded / raw_dc / DC 各列：守卫标记与已清理状态需要
    跨越撤下、重新上架持续保留。
    """
    cur = conn.execute(
        """
        UPDATE bib_records
        SET status = 'withdrawn', datestamp = ?, updated_at = ?
        WHERE identifier = ?
        """,
        (datestamp, utc_now_iso(), identifier),
    )
    return cur.rowcount > 0


def insert_withdrawn_tombstone(conn: sqlite3.Connection, *, identifier: str,
                               datestamp: str) -> None:
    """删除标记记录此前不在库中时，写入 withdrawn 墓碑，保留撤下痕迹。"""
    conn.execute(
        """
        INSERT INTO bib_records (
            identifier, datestamp, status, raw_dc, updated_at
        ) VALUES (?, ?, 'withdrawn', ?, ?)
        ON CONFLICT(identifier) DO NOTHING
        """,
        (identifier, datestamp, "{}", utc_now_iso()),
    )


# ---------------------------------------------------------------------------
# 元数据清理（留存期到期的撤下书目）
# ---------------------------------------------------------------------------

# Dublin Core 14 个落库列（identifier_dc 对应 dc:identifier）
DC_COLUMNS = (
    "title", "creator", "publisher", "subject", "description", "date",
    "type", "format", "identifier_dc", "source", "language", "relation",
    "coverage", "rights",
)


def cleanup_withdrawn_metadata(conn: sqlite3.Connection, *,
                               cutoff_date: str) -> list[str]:
    """清理仍为撤下且 datestamp 不晚于截止日的书目元数据。

    幂等闸门为 write_guarded=0：首次清理后置 1，再次调用不会重复处理，
    因此返回的标识即本次实际清掉元数据（清空 DC 列、raw_dc 置空对象）
    的标识。墓碑（raw_dc 本就是空对象）也在处理范围内，会一并打上
    守卫标记，保证后续补采同样按库内 datestamp 守卫。

    保留 identifier/datestamp/status；不触碰 harvest_progress，续采
    进度不变。
    """
    rows = conn.execute(
        """
        SELECT identifier
        FROM bib_records
        WHERE status = 'withdrawn'
          AND write_guarded = 0
          AND substr(datestamp, 1, 10) <= ?
        ORDER BY identifier
        """,
        (cutoff_date,),
    ).fetchall()
    identifiers = [row["identifier"] for row in rows]
    if not identifiers:
        return []

    set_clause = ", ".join(f"{col} = NULL" for col in DC_COLUMNS)
    placeholders = ",".join("?" for _ in identifiers)
    conn.execute(
        f"""
        UPDATE bib_records
        SET {set_clause},
            raw_dc = '{{}}',
            write_guarded = 1,
            cleared_at = ?,
            updated_at = ?
        WHERE identifier IN ({placeholders})
        """,
        (utc_now_iso(), utc_now_iso(), *identifiers),
    )
    return identifiers


def get_guarded_state(conn: sqlite3.Connection, identifier: str) -> dict | None:
    """读取守卫记录的库内状态，供补采按库内 datestamp 做日粒度比较。"""
    row = conn.execute(
        """
        SELECT identifier, datestamp, status, write_guarded
        FROM bib_records
        WHERE identifier = ? AND write_guarded = 1
        """,
        (identifier,),
    ).fetchone()
    return dict(row) if row is not None else None
