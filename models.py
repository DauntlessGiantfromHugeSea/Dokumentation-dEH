"""Database layer for the Erste-Hilfe documentation app.

Uses SQLite with raw SQL via the standard library — keeps the dependency
footprint tiny and the schema readable.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from flask import g
from werkzeug.security import check_password_hash, generate_password_hash


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name     TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS patients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    geburtsdatum TEXT NOT NULL,
    stammnummer  TEXT,
    UNIQUE(name, geburtsdatum)
);

CREATE TABLE IF NOT EXISTS protocols (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id            INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    laufende_nr           TEXT,
    deh                   TEXT,
    unfall_datum_uhrzeit  TEXT,
    unfallort             TEXT,
    unfallhergang         TEXT,
    art_umfang_verletzung TEXT,
    name_zeugen           TEXT,
    eh_datum_uhrzeit      TEXT,
    name_ersthelfer       TEXT,
    art_weise_massnahmen  TEXT,
    verbrauchtes_material TEXT,
    created_by            INTEGER REFERENCES users(id),
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_protocols_patient ON protocols(patient_id);
CREATE INDEX IF NOT EXISTS idx_protocols_eh_datum ON protocols(eh_datum_uhrzeit);

CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id INTEGER NOT NULL REFERENCES protocols(id) ON DELETE CASCADE,
    author_id   INTEGER REFERENCES users(id),
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_comments_protocol ON comments(protocol_id);
"""


def get_db() -> sqlite3.Connection:
    """Return the request-scoped DB connection, opening one if needed."""
    if "db" not in g:
        db_path: Path = g.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(db_path: Path) -> None:
    """Create the schema. Safe to call repeatedly."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def standalone_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """For CLI scripts that run outside a Flask request."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- Users ----------

def create_user(conn: sqlite3.Connection, username: str, password: str,
                full_name: Optional[str] = None) -> int:
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, full_name) VALUES (?, ?, ?)",
        (username, generate_password_hash(password), full_name),
    )
    return cur.lastrowid


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def verify_password(user_row: sqlite3.Row, password: str) -> bool:
    return check_password_hash(user_row["password_hash"], password)


# ---------- Patients ----------

def upsert_patient(conn: sqlite3.Connection, name: str, geburtsdatum: str,
                   stammnummer: Optional[str]) -> int:
    """Look up patient by (name, geburtsdatum); insert if missing.

    Updates stammnummer if a new one is provided and previously empty.
    """
    name = name.strip()
    geburtsdatum = geburtsdatum.strip()
    row = conn.execute(
        "SELECT id, stammnummer FROM patients WHERE name = ? AND geburtsdatum = ?",
        (name, geburtsdatum),
    ).fetchone()
    if row:
        if stammnummer and not row["stammnummer"]:
            conn.execute("UPDATE patients SET stammnummer = ? WHERE id = ?",
                         (stammnummer.strip(), row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO patients (name, geburtsdatum, stammnummer) VALUES (?, ?, ?)",
        (name, geburtsdatum, (stammnummer or "").strip() or None),
    )
    return cur.lastrowid


def get_patient(conn: sqlite3.Connection, patient_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()


def list_patients(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT p.*,
               COUNT(pr.id) AS protocol_count,
               MAX(pr.eh_datum_uhrzeit) AS last_treatment
        FROM patients p
        LEFT JOIN protocols pr ON pr.patient_id = p.id
        GROUP BY p.id
        ORDER BY p.name COLLATE NOCASE ASC
        """
    ).fetchall()


# ---------- Protocols ----------

PROTOCOL_FIELDS = (
    "laufende_nr",
    "deh",
    "unfall_datum_uhrzeit",
    "unfallort",
    "unfallhergang",
    "art_umfang_verletzung",
    "name_zeugen",
    "eh_datum_uhrzeit",
    "name_ersthelfer",
    "art_weise_massnahmen",
    "verbrauchtes_material",
)


def create_protocol(conn: sqlite3.Connection, patient_id: int,
                    data: dict, created_by: Optional[int]) -> int:
    cols = ["patient_id"] + list(PROTOCOL_FIELDS) + ["created_by"]
    placeholders = ",".join(["?"] * len(cols))
    values = [patient_id] + [data.get(f) or None for f in PROTOCOL_FIELDS] + [created_by]
    cur = conn.execute(
        f"INSERT INTO protocols ({','.join(cols)}) VALUES ({placeholders})",
        values,
    )
    return cur.lastrowid


def update_protocol(conn: sqlite3.Connection, protocol_id: int, data: dict) -> None:
    set_clause = ", ".join(f"{f} = ?" for f in PROTOCOL_FIELDS)
    values = [data.get(f) or None for f in PROTOCOL_FIELDS] + [protocol_id]
    conn.execute(f"UPDATE protocols SET {set_clause} WHERE id = ?", values)


def get_protocol(conn: sqlite3.Connection, protocol_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT pr.*,
               p.name          AS patient_name,
               p.geburtsdatum  AS patient_geburtsdatum,
               p.stammnummer   AS patient_stammnummer,
               u.username      AS author_username,
               u.full_name     AS author_full_name
        FROM protocols pr
        JOIN patients p ON p.id = pr.patient_id
        LEFT JOIN users u ON u.id = pr.created_by
        WHERE pr.id = ?
        """,
        (protocol_id,),
    ).fetchone()


def list_protocols(conn: sqlite3.Connection, *,
                   patient_id: Optional[int] = None,
                   date_from: Optional[str] = None,
                   date_to: Optional[str] = None,
                   stammnummer: Optional[str] = None,
                   name_query: Optional[str] = None) -> list[sqlite3.Row]:
    sql = [
        """
        SELECT pr.id, pr.patient_id, pr.laufende_nr, pr.deh,
               pr.unfall_datum_uhrzeit, pr.eh_datum_uhrzeit,
               pr.name_ersthelfer, pr.created_at,
               p.name AS patient_name, p.geburtsdatum AS patient_geburtsdatum,
               p.stammnummer AS patient_stammnummer
        FROM protocols pr
        JOIN patients p ON p.id = pr.patient_id
        WHERE 1=1
        """
    ]
    params: list = []
    if patient_id is not None:
        sql.append("AND pr.patient_id = ?")
        params.append(patient_id)
    if date_from:
        sql.append("AND date(pr.eh_datum_uhrzeit) >= date(?)")
        params.append(date_from)
    if date_to:
        sql.append("AND date(pr.eh_datum_uhrzeit) <= date(?)")
        params.append(date_to)
    if stammnummer:
        sql.append("AND p.stammnummer LIKE ?")
        params.append(f"%{stammnummer}%")
    if name_query:
        sql.append("AND p.name LIKE ?")
        params.append(f"%{name_query}%")
    sql.append("ORDER BY COALESCE(pr.eh_datum_uhrzeit, pr.created_at) DESC, pr.id DESC")
    return conn.execute("\n".join(sql), params).fetchall()


def list_patient_protocols(conn: sqlite3.Connection, patient_id: int) -> list[sqlite3.Row]:
    return list_protocols(conn, patient_id=patient_id)


# ---------- Comments ----------

def add_comment(conn: sqlite3.Connection, protocol_id: int,
                text: str, author_id: Optional[int]) -> int:
    cur = conn.execute(
        "INSERT INTO comments (protocol_id, author_id, text) VALUES (?, ?, ?)",
        (protocol_id, author_id, text.strip()),
    )
    return cur.lastrowid


def list_comments(conn: sqlite3.Connection, protocol_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.*, u.username AS author_username, u.full_name AS author_full_name
        FROM comments c
        LEFT JOIN users u ON u.id = c.author_id
        WHERE c.protocol_id = ?
        ORDER BY c.created_at ASC, c.id ASC
        """,
        (protocol_id,),
    ).fetchall()


def format_dt(value: Optional[str]) -> str:
    """Format an ISO datetime/date string for display. Returns '' on None."""
    if not value:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime(
                "%d.%m.%Y" if fmt == "%Y-%m-%d" else "%d.%m.%Y %H:%M"
            )
        except ValueError:
            continue
    return value
