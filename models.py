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

from flask import current_app, g
from werkzeug.security import check_password_hash, generate_password_hash


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name     TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS central_protocols (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id    INTEGER REFERENCES patients(id) ON DELETE SET NULL,
    einsatznummer TEXT,
    datum         TEXT,
    name_summary  TEXT,
    data          TEXT NOT NULL,
    created_by    INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_central_patient ON central_protocols(patient_id);
CREATE INDEX IF NOT EXISTS idx_central_datum ON central_protocols(datum);
"""


def get_db() -> sqlite3.Connection:
    """Return the app-context DB connection, opening one if needed."""
    if "db" not in g:
        db_path = Path(current_app.config["DB_PATH"])
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
    """Create the schema and apply lightweight migrations.

    Safe to call repeatedly (each migration is idempotent).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        # Migration: add is_admin to existing users tables.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "is_admin" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
            )
        # Promote oldest user to admin if there isn't one yet — keeps the
        # initial bootstrap simple ("first user = admin").
        has_admin = conn.execute(
            "SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1"
        ).fetchone()
        if not has_admin:
            conn.execute(
                "UPDATE users SET is_admin = 1 "
                "WHERE id = (SELECT MIN(id) FROM users)"
            )
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
                full_name: Optional[str] = None,
                is_admin: bool = False) -> int:
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, full_name, is_admin) "
        "VALUES (?, ?, ?, ?)",
        (username, generate_password_hash(password), full_name,
         1 if is_admin else 0),
    )
    return cur.lastrowid


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def verify_password(user_row: sqlite3.Row, password: str) -> bool:
    return check_password_hash(user_row["password_hash"], password)


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, username, full_name, is_admin, created_at "
        "FROM users ORDER BY username COLLATE NOCASE"
    ).fetchall()


def set_user_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (generate_password_hash(password), user_id))


def set_user_admin(conn: sqlite3.Connection, user_id: int, is_admin: bool) -> None:
    conn.execute("UPDATE users SET is_admin = ? WHERE id = ?",
                 (1 if is_admin else 0, user_id))


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def count_admins(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
    ).fetchone()["n"]


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
               COALESCE(d.n, 0) AS decentral_count,
               COALESCE(c.n, 0) AS central_count,
               (COALESCE(d.n, 0) + COALESCE(c.n, 0)) AS protocol_count,
               COALESCE(d.last_d, c.last_c) AS last_treatment_any,
               d.last_d AS last_decentral,
               c.last_c AS last_central
        FROM patients p
        LEFT JOIN (
          SELECT patient_id, COUNT(*) AS n,
                 MAX(eh_datum_uhrzeit) AS last_d
          FROM protocols GROUP BY patient_id
        ) d ON d.patient_id = p.id
        LEFT JOIN (
          SELECT patient_id, COUNT(*) AS n, MAX(datum) AS last_c
          FROM central_protocols WHERE patient_id IS NOT NULL
          GROUP BY patient_id
        ) c ON c.patient_id = p.id
        ORDER BY p.name COLLATE NOCASE ASC
        """
    ).fetchall()


# ---------- Protocols ----------

# Fields editable through the form. `laufende_nr` and `deh` are auto-managed
# (laufende_nr = "#dEH<id>") and never accepted from form input.
PROTOCOL_FIELDS = (
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


def laufende_nr_for(protocol_id: int) -> str:
    return f"#dEH{protocol_id}"


def create_protocol(conn: sqlite3.Connection, patient_id: int,
                    data: dict, created_by: Optional[int]) -> int:
    cols = ["patient_id"] + list(PROTOCOL_FIELDS) + ["created_by"]
    placeholders = ",".join(["?"] * len(cols))
    values = [patient_id] + [data.get(f) or None for f in PROTOCOL_FIELDS] + [created_by]
    cur = conn.execute(
        f"INSERT INTO protocols ({','.join(cols)}) VALUES ({placeholders})",
        values,
    )
    new_id = cur.lastrowid
    conn.execute("UPDATE protocols SET laufende_nr = ? WHERE id = ?",
                 (laufende_nr_for(new_id), new_id))
    return new_id


def update_protocol(conn: sqlite3.Connection, protocol_id: int, data: dict) -> None:
    set_clause = ", ".join(f"{f} = ?" for f in PROTOCOL_FIELDS)
    values = [data.get(f) or None for f in PROTOCOL_FIELDS] + [protocol_id]
    conn.execute(f"UPDATE protocols SET {set_clause} WHERE id = ?", values)
    # Backfill laufende_nr in case an older record was missing it.
    conn.execute(
        "UPDATE protocols SET laufende_nr = ? WHERE id = ? AND "
        "(laufende_nr IS NULL OR laufende_nr = '')",
        (laufende_nr_for(protocol_id), protocol_id),
    )


def delete_protocol(conn: sqlite3.Connection, protocol_id: int) -> bool:
    cur = conn.execute("DELETE FROM protocols WHERE id = ?", (protocol_id,))
    return cur.rowcount > 0


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


# ---------- Central protocols (Notfallprotokoll, größere Form) ----------

import json as _json


def _scalar(v):
    """Form data values can be lists (multi-checkbox); take first scalar."""
    if isinstance(v, list):
        return v[0] if v else ""
    return "" if v is None else str(v)


def _name_summary(data: dict) -> str:
    vorname = _scalar(data.get("vorname")).strip()
    nachname = _scalar(data.get("nachname")).strip()
    return " ".join(p for p in (vorname, nachname) if p)


def _link_central_to_patient(conn: sqlite3.Connection, data: dict) -> Optional[int]:
    """Upsert a patient based on data['vorname'] + data['nachname'] +
    data['geburtsdatum'] so central and decentral protocols share patients.
    Returns patient_id or None if name/birthday missing.
    """
    name = _name_summary(data)
    geburtsdatum = _scalar(data.get("geburtsdatum")).strip()
    if not name or not geburtsdatum:
        return None
    return upsert_patient(conn, name, geburtsdatum, None)


def list_central_protocols(conn: sqlite3.Connection, *,
                           patient_id: Optional[int] = None) -> list[sqlite3.Row]:
    sql = [
        """
        SELECT cp.id, cp.patient_id, cp.einsatznummer, cp.datum,
               cp.name_summary, cp.created_at, cp.updated_at,
               p.name AS patient_name, p.geburtsdatum AS patient_geburtsdatum,
               p.stammnummer AS patient_stammnummer
        FROM central_protocols cp
        LEFT JOIN patients p ON p.id = cp.patient_id
        WHERE 1=1
        """
    ]
    params: list = []
    if patient_id is not None:
        sql.append("AND cp.patient_id = ?")
        params.append(patient_id)
    sql.append("ORDER BY datetime(cp.updated_at) DESC")
    return conn.execute("\n".join(sql), params).fetchall()


def get_central_protocol(conn: sqlite3.Connection, pid: int) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT cp.*, p.name AS patient_name, p.geburtsdatum AS patient_geburtsdatum
        FROM central_protocols cp
        LEFT JOIN patients p ON p.id = cp.patient_id
        WHERE cp.id = ?
        """,
        (pid,),
    ).fetchone()
    if not row:
        return None
    rec = dict(row)
    rec["data"] = _json.loads(rec["data"] or "{}")
    return rec


def create_central_protocol(conn: sqlite3.Connection, data: dict,
                            created_by: Optional[int]) -> int:
    patient_id = _link_central_to_patient(conn, data)
    cur = conn.execute(
        """
        INSERT INTO central_protocols
          (patient_id, einsatznummer, datum, name_summary, data, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            patient_id,
            _scalar(data.get("einsatznummer")) or None,
            _scalar(data.get("datum")) or None,
            _name_summary(data) or None,
            _json.dumps(data, ensure_ascii=False),
            created_by,
        ),
    )
    return cur.lastrowid


def update_central_protocol(conn: sqlite3.Connection, pid: int,
                            data: dict) -> bool:
    patient_id = _link_central_to_patient(conn, data)
    cur = conn.execute(
        """
        UPDATE central_protocols
           SET patient_id = ?, einsatznummer = ?, datum = ?,
               name_summary = ?, data = ?, updated_at = datetime('now')
         WHERE id = ?
        """,
        (
            patient_id,
            _scalar(data.get("einsatznummer")) or None,
            _scalar(data.get("datum")) or None,
            _name_summary(data) or None,
            _json.dumps(data, ensure_ascii=False),
            pid,
        ),
    )
    return cur.rowcount > 0


def delete_central_protocol(conn: sqlite3.Connection, pid: int) -> bool:
    cur = conn.execute("DELETE FROM central_protocols WHERE id = ?", (pid,))
    return cur.rowcount > 0


def patient_protocol_counts(conn: sqlite3.Connection, patient_id: int) -> dict:
    """How many decentral and central protocols exist for this patient."""
    decentral = conn.execute(
        "SELECT COUNT(*) AS n FROM protocols WHERE patient_id = ?",
        (patient_id,),
    ).fetchone()["n"]
    central = conn.execute(
        "SELECT COUNT(*) AS n FROM central_protocols WHERE patient_id = ?",
        (patient_id,),
    ).fetchone()["n"]
    return {"decentral": decentral, "central": central}
