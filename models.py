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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    full_name       TEXT,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    role            TEXT NOT NULL DEFAULT 'full',
    totp_secret     TEXT,
    totp_confirmed  INTEGER NOT NULL DEFAULT 0,
    totp_required   INTEGER NOT NULL DEFAULT 1,
    perm_view_contact INTEGER NOT NULL DEFAULT 0,
    perm_export_pdf   INTEGER NOT NULL DEFAULT 0,
    perm_export_akte  INTEGER NOT NULL DEFAULT 0,
    perm_edit_patient INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS patients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    geburtsdatum TEXT NOT NULL,
    stammnummer  TEXT,
    -- Notfallkontakt + medizinische Hinweise (Anmeldungs-Daten)
    emergency_contact_name     TEXT,
    emergency_contact_phone    TEXT,
    emergency_contact_relation TEXT,
    has_allergies              INTEGER,    -- NULL=unbekannt, 0=nein, 1=ja
    allergies_text             TEXT,
    has_medications            INTEGER,    -- NULL=unbekannt, 0=nein, 1=ja
    medications_text           TEXT,
    extras_notes               TEXT,
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

CREATE TABLE IF NOT EXISTS central_comments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    central_protocol_id INTEGER NOT NULL REFERENCES central_protocols(id) ON DELETE CASCADE,
    author_id           INTEGER REFERENCES users(id),
    text                TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_central_comments_protocol ON central_comments(central_protocol_id);

-- Audit-Log für Änderungen an Patientendaten (wer/wann/was)
CREATE TABLE IF NOT EXISTS patient_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id  INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    changed_by  INTEGER REFERENCES users(id),
    field_name  TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    changed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_patient_changes_patient ON patient_changes(patient_id);

-- Audit-Log für Entschlüsselungs-Zugriffe auf Notfall-/Med-Daten
CREATE TABLE IF NOT EXISTS emergency_unlocks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id    INTEGER NOT NULL,
    requested_by  INTEGER REFERENCES users(id),
    approved_by   INTEGER REFERENCES users(id),
    unlocked_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_emergency_unlocks_patient ON emergency_unlocks(patient_id);

-- Globaler, fortlaufender Zähler für ALLE Berichte (dezentral + zentral).
-- Jeder neue Bericht bekommt eine neue Zeile hier; das per id automatisch
-- vergebene auto-increment ist die "Bericht-Nr." über beide Systeme hinweg.
CREATE TABLE IF NOT EXISTS protocol_sequence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,           -- 'decentral' | 'central'
    source_id   INTEGER NOT NULL,        -- protocols.id oder central_protocols.id
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_type, source_id)
);

-- PRIOR-Triage-Eingang (Anmeldung). Patienten werden bei Ankunft kurz
-- eingestuft (SK I rot / SK II gelb / SK III grün) und tauchen dann in
-- der Wartebereich-Liste auf, sortiert nach Akutität + Wartezeit.
CREATE TABLE IF NOT EXISTS triage_entries (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id               INTEGER REFERENCES patients(id) ON DELETE SET NULL,
    name                     TEXT,
    geburtsdatum             TEXT,
    arrival_at               TEXT NOT NULL DEFAULT (datetime('now')),
    category                 TEXT NOT NULL,    -- 'SK1' | 'SK2' | 'SK3'
    indicators               TEXT,             -- JSON-Array der Schlüssel
    notes                    TEXT,
    status                   TEXT NOT NULL DEFAULT 'wartend',
                              -- 'wartend' | 'in_behandlung' | 'abgeschlossen' | 'abgebrochen'
    treatment_started_at     TEXT,
    treatment_protocol_id    INTEGER REFERENCES central_protocols(id)
                              ON DELETE SET NULL,
    created_by               INTEGER REFERENCES users(id),
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_triage_status ON triage_entries(status);
CREATE INDEX IF NOT EXISTS idx_triage_category ON triage_entries(category);
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
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        # Migration: add columns introduced after the initial release.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "is_admin" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
            )
        if "role" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'full'"
            )
        if "totp_secret" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT")
        if "totp_confirmed" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN totp_confirmed "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "totp_required" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN totp_required "
                "INTEGER NOT NULL DEFAULT 1"
            )
        if "admin_pin_hash" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN admin_pin_hash TEXT")
        for perm in ("perm_view_contact", "perm_export_pdf",
                     "perm_export_akte", "perm_edit_patient"):
            if perm not in cols:
                conn.execute(
                    f"ALTER TABLE users ADD COLUMN {perm} "
                    "INTEGER NOT NULL DEFAULT 0"
                )

        # Patient extras (Notfallkontakt / Allergien / Medikamente)
        pat_cols = {row[1] for row in conn.execute("PRAGMA table_info(patients)")}
        for col, decl in [
            ("emergency_contact_name", "TEXT"),
            ("emergency_contact_phone", "TEXT"),
            ("emergency_contact_relation", "TEXT"),
            ("has_allergies", "INTEGER"),
            ("allergies_text", "TEXT"),
            ("has_medications", "INTEGER"),
            ("medications_text", "TEXT"),
            ("extras_notes", "TEXT"),
        ]:
            if col not in pat_cols:
                conn.execute(f"ALTER TABLE patients ADD COLUMN {col} {decl}")

        # Migration: global_id columns for both protocol tables.
        proto_cols = {row[1] for row in conn.execute("PRAGMA table_info(protocols)")}
        if "global_id" not in proto_cols:
            conn.execute("ALTER TABLE protocols ADD COLUMN global_id INTEGER")
        cent_cols = {row[1] for row in conn.execute("PRAGMA table_info(central_protocols)")}
        if "global_id" not in cent_cols:
            conn.execute("ALTER TABLE central_protocols ADD COLUMN global_id INTEGER")
        if "laufende_nr" not in cent_cols:
            conn.execute("ALTER TABLE central_protocols ADD COLUMN laufende_nr TEXT")

        # Backfill global_id (and matching laufende_nr) for any rows that
        # don't have one yet — chronologically, oldest first.
        _backfill_global_ids(conn)
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


def _backfill_global_ids(conn: sqlite3.Connection) -> None:
    """Assign global_id (+ laufende_nr) to any rows still missing one.

    Runs inside init_db so it's idempotent and triggers automatically
    after the schema migration adds the columns.
    """
    rows = conn.execute(
        """
        SELECT * FROM (
          SELECT 'decentral' AS source, id AS source_id, created_at
          FROM protocols WHERE global_id IS NULL
          UNION ALL
          SELECT 'central'   AS source, id AS source_id, created_at
          FROM central_protocols WHERE global_id IS NULL
        )
        ORDER BY datetime(created_at), source, source_id
        """
    ).fetchall()
    for r in rows:
        # Each backfill insert assigns the next sequence id.
        cur = conn.execute(
            "INSERT INTO protocol_sequence (source_type, source_id) VALUES (?, ?)",
            (r["source"], r["source_id"]),
        )
        gid = cur.lastrowid
        prefix = "dEH" if r["source"] == "decentral" else "zEH"
        nr = f"#{prefix}{gid}"
        if r["source"] == "decentral":
            conn.execute(
                "UPDATE protocols SET global_id = ?, laufende_nr = ? WHERE id = ?",
                (gid, nr, r["source_id"]),
            )
        else:
            conn.execute(
                "UPDATE central_protocols SET global_id = ?, laufende_nr = ? WHERE id = ?",
                (gid, nr, r["source_id"]),
            )


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

VALID_ROLES = ("full", "zentral_writer", "triage_intake")


ROLE_LABELS = {
    "full": "Voll (Lesen + Schreiben)",
    "zentral_writer": "Nur Zentrale Erste Hilfe schreiben",
    "triage_intake": "Nur Anmeldung (Triage-Kiosk)",
}


def create_user(conn: sqlite3.Connection, username: str, password: str,
                full_name: Optional[str] = None,
                is_admin: bool = False,
                role: str = "full") -> int:
    if role not in VALID_ROLES:
        raise ValueError(f"unknown role: {role}")
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, full_name, is_admin, role) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, generate_password_hash(password), full_name,
         1 if is_admin else 0, role),
    )
    return cur.lastrowid


def set_user_role(conn: sqlite3.Connection, user_id: int, role: str) -> None:
    if role not in VALID_ROLES:
        raise ValueError(f"unknown role: {role}")
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def verify_password(user_row: sqlite3.Row, password: str) -> bool:
    return check_password_hash(user_row["password_hash"], password)


USER_PERMISSIONS = (
    "perm_view_contact",   # Adresse / Krankenkasse / Telefon ohne PIN sehen
    "perm_export_pdf",     # Notfallprotokoll-PDF erzeugen
    "perm_export_akte",    # Akten-Export (PDF) erzeugen
    "perm_edit_patient",   # Patientenstammdaten + Notfallkontakt bearbeiten
)


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cols = ", ".join(USER_PERMISSIONS)
    return conn.execute(
        f"SELECT id, username, full_name, is_admin, role, "
        f"       totp_secret, totp_confirmed, totp_required, "
        f"       {cols}, created_at "
        f"FROM users ORDER BY username COLLATE NOCASE"
    ).fetchall()


def set_user_permission(conn: sqlite3.Connection, user_id: int,
                        perm: str, value: bool) -> None:
    if perm not in USER_PERMISSIONS:
        raise ValueError(f"unknown permission: {perm}")
    conn.execute(
        f"UPDATE users SET {perm} = ? WHERE id = ?",
        (1 if value else 0, user_id),
    )


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


def set_totp_secret(conn: sqlite3.Connection, user_id: int,
                    secret: Optional[str], confirmed: bool = False) -> None:
    conn.execute(
        "UPDATE users SET totp_secret = ?, totp_confirmed = ? WHERE id = ?",
        (secret, 1 if confirmed else 0, user_id),
    )


def set_totp_confirmed(conn: sqlite3.Connection, user_id: int,
                       confirmed: bool) -> None:
    conn.execute(
        "UPDATE users SET totp_confirmed = ? WHERE id = ?",
        (1 if confirmed else 0, user_id),
    )


def reset_totp(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "UPDATE users SET totp_secret = NULL, totp_confirmed = 0 WHERE id = ?",
        (user_id,),
    )


def set_totp_required(conn: sqlite3.Connection, user_id: int,
                      required: bool) -> None:
    conn.execute(
        "UPDATE users SET totp_required = ? WHERE id = ?",
        (1 if required else 0, user_id),
    )


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


def _assign_global_id(conn: sqlite3.Connection, source_type: str,
                      source_id: int) -> tuple[int, str]:
    """Insert into protocol_sequence and return (global_id, laufende_nr)."""
    cur = conn.execute(
        "INSERT INTO protocol_sequence (source_type, source_id) VALUES (?, ?)",
        (source_type, source_id),
    )
    gid = cur.lastrowid
    prefix = "dEH" if source_type == "decentral" else "zEH"
    return gid, f"#{prefix}{gid}"


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
    gid, nr = _assign_global_id(conn, "decentral", new_id)
    conn.execute(
        "UPDATE protocols SET global_id = ?, laufende_nr = ? WHERE id = ?",
        (gid, nr, new_id),
    )
    return new_id


def update_protocol(conn: sqlite3.Connection, protocol_id: int, data: dict) -> None:
    set_clause = ", ".join(f"{f} = ?" for f in PROTOCOL_FIELDS)
    values = [data.get(f) or None for f in PROTOCOL_FIELDS] + [protocol_id]
    conn.execute(f"UPDATE protocols SET {set_clause} WHERE id = ?", values)


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
                           patient_id: Optional[int] = None,
                           created_by: Optional[int] = None
                           ) -> list[sqlite3.Row]:
    sql = [
        """
        SELECT cp.id, cp.patient_id, cp.einsatznummer, cp.datum,
               cp.name_summary, cp.global_id, cp.laufende_nr,
               cp.created_by, cp.created_at, cp.updated_at,
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
    if created_by is not None:
        sql.append("AND cp.created_by = ?")
        params.append(created_by)
    sql.append("ORDER BY datetime(cp.updated_at) DESC")
    return conn.execute("\n".join(sql), params).fetchall()


def list_unified_protocols(conn: sqlite3.Connection, *,
                           date_from: Optional[str] = None,
                           date_to: Optional[str] = None,
                           stammnummer: Optional[str] = None,
                           name_query: Optional[str] = None,
                           source_filter: Optional[str] = None
                           ) -> list[sqlite3.Row]:
    """Unified Bericht list across both protocol types, sorted by global_id."""
    decentral_filter = " AND 1=1"
    central_filter = " AND 1=1"
    decentral_params: list = []
    central_params: list = []

    if date_from:
        decentral_filter += " AND date(p.eh_datum_uhrzeit) >= date(?)"
        decentral_params.append(date_from)
        central_filter += " AND date(c.datum) >= date(?)"
        central_params.append(date_from)
    if date_to:
        decentral_filter += " AND date(p.eh_datum_uhrzeit) <= date(?)"
        decentral_params.append(date_to)
        central_filter += " AND date(c.datum) <= date(?)"
        central_params.append(date_to)
    if stammnummer:
        decentral_filter += " AND pat.stammnummer LIKE ?"
        decentral_params.append(f"%{stammnummer}%")
        central_filter += " AND pat.stammnummer LIKE ?"
        central_params.append(f"%{stammnummer}%")
    if name_query:
        decentral_filter += " AND pat.name LIKE ?"
        decentral_params.append(f"%{name_query}%")
        central_filter += (
            " AND (pat.name LIKE ? OR c.name_summary LIKE ?)"
        )
        central_params.extend([f"%{name_query}%", f"%{name_query}%"])

    parts = []
    params: list = []
    if source_filter != "central":
        parts.append(f"""
            SELECT 'decentral' AS source, p.id AS source_id,
                   p.global_id, p.laufende_nr,
                   p.eh_datum_uhrzeit AS event_date,
                   p.name_ersthelfer AS responder,
                   p.created_at,
                   p.patient_id,
                   pat.name AS patient_name,
                   pat.geburtsdatum AS patient_geburtsdatum,
                   pat.stammnummer AS patient_stammnummer
            FROM protocols p
            LEFT JOIN patients pat ON pat.id = p.patient_id
            WHERE p.global_id IS NOT NULL{decentral_filter}
        """)
        params.extend(decentral_params)
    if source_filter != "decentral":
        parts.append(f"""
            SELECT 'central' AS source, c.id AS source_id,
                   c.global_id, c.laufende_nr,
                   c.datum AS event_date,
                   c.name_summary AS responder,
                   c.created_at,
                   c.patient_id,
                   COALESCE(pat.name, c.name_summary) AS patient_name,
                   pat.geburtsdatum AS patient_geburtsdatum,
                   pat.stammnummer AS patient_stammnummer
            FROM central_protocols c
            LEFT JOIN patients pat ON pat.id = c.patient_id
            WHERE c.global_id IS NOT NULL{central_filter}
        """)
        params.extend(central_params)

    if not parts:
        return []
    sql = " UNION ALL ".join(parts) + " ORDER BY global_id DESC"
    return conn.execute(sql, params).fetchall()


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
    new_id = cur.lastrowid
    gid, nr = _assign_global_id(conn, "central", new_id)
    conn.execute(
        "UPDATE central_protocols SET global_id = ?, laufende_nr = ? WHERE id = ?",
        (gid, nr, new_id),
    )
    return new_id


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


def patient_last_treatment(conn: sqlite3.Connection,
                           patient_id: int) -> Optional[str]:
    """Most recent treatment date across both protocol types, or None."""
    row = conn.execute(
        """
        SELECT MAX(d) AS last FROM (
            SELECT eh_datum_uhrzeit AS d FROM protocols WHERE patient_id = ?
            UNION ALL
            SELECT datum AS d FROM central_protocols WHERE patient_id = ?
        )
        """,
        (patient_id, patient_id),
    ).fetchone()
    return row["last"] if row and row["last"] else None


# ---------- Suche & Lookup ----------

def search_patients(conn: sqlite3.Connection, *,
                    name_query: Optional[str] = None,
                    geburtsdatum: Optional[str] = None,
                    limit: int = 20) -> list[sqlite3.Row]:
    """Suche Patienten — nach Name (substring, case-insensitive) und/oder
    Geburtsdatum (exakt). Beide Felder optional, mindestens eines muss
    gefüllt sein (sonst leer)."""
    name_query = (name_query or "").strip()
    geburtsdatum = (geburtsdatum or "").strip()
    if not name_query and not geburtsdatum:
        return []
    sql = ["SELECT id, name, geburtsdatum, stammnummer FROM patients WHERE 1=1"]
    params: list = []
    if name_query:
        sql.append("AND name LIKE ? COLLATE NOCASE")
        params.append(f"%{name_query}%")
    if geburtsdatum:
        sql.append("AND geburtsdatum = ?")
        params.append(geburtsdatum)
    sql.append("ORDER BY name COLLATE NOCASE LIMIT ?")
    params.append(limit)
    return conn.execute("\n".join(sql), params).fetchall()


def patient_history(conn: sqlite3.Connection,
                    patient_id: int) -> list[dict]:
    """Alle Berichte (dezentral + zentral) eines Patienten, chronologisch
    absteigend. Wird für die Vorbehandlungs-Popup-Liste genutzt."""
    rows = conn.execute(
        """
        SELECT 'decentral' AS source, id, laufende_nr,
               COALESCE(eh_datum_uhrzeit, created_at) AS event_date,
               name_ersthelfer AS responder, created_at
        FROM protocols WHERE patient_id = ?
        UNION ALL
        SELECT 'central' AS source, id, laufende_nr,
               COALESCE(datum, created_at) AS event_date,
               name_summary AS responder, created_at
        FROM central_protocols WHERE patient_id = ?
        ORDER BY event_date DESC, created_at DESC
        """,
        (patient_id, patient_id),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- Dashboard ----------

def _count_decentral(conn, where_sql, params):
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM protocols WHERE {where_sql}", params
    ).fetchone()["n"]


def _count_central(conn, where_sql, params):
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM central_protocols WHERE {where_sql}",
        params,
    ).fetchone()["n"]


def _treatment_date_decentral():
    return "COALESCE(date(eh_datum_uhrzeit), date(created_at))"


def _treatment_date_central():
    return "COALESCE(date(datum), date(created_at))"


def dashboard_stats(conn: sqlite3.Connection, ref_date) -> dict:
    """Liefert Statistiken bezogen auf ref_date (datetime.date)."""
    from datetime import date as _date, timedelta
    iso = ref_date.isoformat()

    def one(d_iso):
        d = _count_decentral(conn, f"{_treatment_date_decentral()} = ?", (d_iso,))
        c = _count_central(conn, f"{_treatment_date_central()} = ?", (d_iso,))
        return {"d": d, "c": c}

    def rng(start_iso, end_iso):
        d = _count_decentral(conn,
            f"{_treatment_date_decentral()} BETWEEN ? AND ?",
            (start_iso, end_iso))
        c = _count_central(conn,
            f"{_treatment_date_central()} BETWEEN ? AND ?",
            (start_iso, end_iso))
        return {"d": d, "c": c}

    week_start = ref_date - timedelta(days=ref_date.weekday())
    month_start = ref_date.replace(day=1)
    yesterday = ref_date - timedelta(days=1)

    return {
        "selected": one(iso),
        "yesterday": one(yesterday.isoformat()),
        "week": rng(week_start.isoformat(), iso),
        "month": rng(month_start.isoformat(), iso),
        "total": {
            "d": conn.execute("SELECT COUNT(*) AS n FROM protocols").fetchone()["n"],
            "c": conn.execute("SELECT COUNT(*) AS n FROM central_protocols").fetchone()["n"],
        },
    }


def dashboard_daily_counts(conn: sqlite3.Connection, *,
                           end_date, days: int = 14) -> list[dict]:
    """Pro Tag (rückwärts ab end_date) Zähler dezentral/zentral."""
    from datetime import timedelta
    out = []
    for i in range(days - 1, -1, -1):
        d = end_date - timedelta(days=i)
        d_iso = d.isoformat()
        out.append({
            "date": d,
            "decentral": _count_decentral(
                conn, f"{_treatment_date_decentral()} = ?", (d_iso,)
            ),
            "central": _count_central(
                conn, f"{_treatment_date_central()} = ?", (d_iso,)
            ),
        })
    return out


def list_decentral_responders(conn: sqlite3.Connection) -> list[str]:
    """Liefert alle bisher in der dezentralen Erfassung verwendeten
    Ersthelfer-Namen, dedupliziert + alphabetisch."""
    rows = conn.execute(
        """
        SELECT DISTINCT TRIM(name_ersthelfer) AS h
        FROM protocols
        WHERE name_ersthelfer IS NOT NULL AND TRIM(name_ersthelfer) != ''
        ORDER BY h COLLATE NOCASE
        """
    ).fetchall()
    return [r["h"] for r in rows]


def top_decentral_responders(conn: sqlite3.Connection,
                             limit: int = 5) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT TRIM(name_ersthelfer) AS responder, COUNT(*) AS n
        FROM protocols
        WHERE name_ersthelfer IS NOT NULL AND TRIM(name_ersthelfer) != ''
        GROUP BY responder COLLATE NOCASE
        ORDER BY n DESC, responder COLLATE NOCASE
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


# ---------- Central comments (zentral, separates Tabellen-Pendant zu comments) ----------

def add_central_comment(conn: sqlite3.Connection, central_protocol_id: int,
                        text: str, author_id: Optional[int]) -> int:
    cur = conn.execute(
        "INSERT INTO central_comments "
        "(central_protocol_id, author_id, text) VALUES (?, ?, ?)",
        (central_protocol_id, author_id, text.strip()),
    )
    return cur.lastrowid


def list_central_comments(conn: sqlite3.Connection,
                          central_protocol_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.*, u.username AS author_username, u.full_name AS author_full_name
        FROM central_comments c
        LEFT JOIN users u ON u.id = c.author_id
        WHERE c.central_protocol_id = ?
        ORDER BY c.created_at ASC, c.id ASC
        """,
        (central_protocol_id,),
    ).fetchall()


# ---------- Patient editing + Audit-Log ----------

# Felder, die über das Patienten-Bearbeiten-Formular gepflegt werden.
# Reihenfolge bestimmt auch die Anzeige.
PATIENT_EDIT_FIELDS = (
    "name",
    "geburtsdatum",
    "stammnummer",
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relation",
    "has_allergies",
    "allergies_text",
    "has_medications",
    "medications_text",
    "extras_notes",
)

PATIENT_FIELD_LABELS = {
    "name": "Name",
    "geburtsdatum": "Geburtsdatum",
    "stammnummer": "Stammnummer",
    "emergency_contact_name": "Notfallkontakt — Name",
    "emergency_contact_phone": "Notfallkontakt — Telefon",
    "emergency_contact_relation": "Notfallkontakt — Beziehung",
    "has_allergies": "Allergien (ja/nein)",
    "allergies_text": "Allergien (Details)",
    "has_medications": "Medikamente (ja/nein)",
    "medications_text": "Medikamente (Details)",
    "extras_notes": "Sonstige Hinweise",
    "__akte_export__": "Akten-PDF exportiert",
}

SENSITIVE_PATIENT_FIELDS = (
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relation",
    "allergies_text",
    "medications_text",
    "extras_notes",
)


def _normalize_optional(v):
    """Empty strings → None; ints stay int; strings get stripped."""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v if v else None
    return v


def update_patient(conn: sqlite3.Connection, patient_id: int,
                   updates: dict, changed_by: Optional[int]) -> int:
    """Apply patch to patients table and write a row in patient_changes
    for every field whose value actually changes. Returns the number
    of changes recorded.
    """
    current = conn.execute(
        "SELECT * FROM patients WHERE id = ?", (patient_id,)
    ).fetchone()
    if not current:
        raise ValueError(f"patient {patient_id} not found")

    changes = []
    for field in PATIENT_EDIT_FIELDS:
        if field not in updates:
            continue
        new_val = _normalize_optional(updates[field])
        old_val = current[field] if field in current.keys() else None
        # Compare with type coercion: '0' == 0 etc.
        if old_val is None and new_val is None:
            continue
        if str(old_val if old_val is not None else "") == str(new_val if new_val is not None else ""):
            continue
        changes.append((field, old_val, new_val))

    if not changes:
        return 0

    # Apply update
    set_clause = ", ".join(f"{f} = ?" for f, _, _ in changes)
    values = [v for _, _, v in changes] + [patient_id]
    conn.execute(f"UPDATE patients SET {set_clause} WHERE id = ?", values)

    # Audit-log
    for field, old_val, new_val in changes:
        # For sensitive fields we still log the change, but redact the value
        # — only that "etwas wurde geändert" is shown to non-admins.
        conn.execute(
            "INSERT INTO patient_changes "
            "(patient_id, changed_by, field_name, old_value, new_value) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                patient_id, changed_by, field,
                None if old_val is None else str(old_val),
                None if new_val is None else str(new_val),
            ),
        )
    return len(changes)


def list_patient_changes(conn: sqlite3.Connection,
                         patient_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT pc.*,
               u.username   AS author_username,
               u.full_name  AS author_full_name
        FROM patient_changes pc
        LEFT JOIN users u ON u.id = pc.changed_by
        WHERE pc.patient_id = ?
        ORDER BY pc.changed_at DESC, pc.id DESC
        """,
        (patient_id,),
    ).fetchall()


# ---------- Sensitive Patient-Daten + Admin-PIN ----------

def patient_sensitive_summary(row: sqlite3.Row) -> dict:
    """Maskierte Sicht für nicht-Admins: nur Existenz-Indikatoren."""
    return {
        "has_emergency_contact": bool(row["emergency_contact_name"]
                                      or row["emergency_contact_phone"]),
        "has_allergies": (None if row["has_allergies"] is None
                          else bool(row["has_allergies"])),
        "has_medications": (None if row["has_medications"] is None
                            else bool(row["has_medications"])),
        "has_extras_notes": bool(row["extras_notes"]),
    }


def patient_sensitive_full(row: sqlite3.Row) -> dict:
    """Volle Sicht für Admins (oder nach erfolgreicher PIN-Freigabe)."""
    summary = patient_sensitive_summary(row)
    return {
        **summary,
        "emergency_contact_name": row["emergency_contact_name"] or "",
        "emergency_contact_phone": row["emergency_contact_phone"] or "",
        "emergency_contact_relation": row["emergency_contact_relation"] or "",
        "allergies_text": row["allergies_text"] or "",
        "medications_text": row["medications_text"] or "",
        "extras_notes": row["extras_notes"] or "",
    }


# Adresse + Telefon + Krankenkasse: für Nicht-Admins versteckt.
# Werden im API-GET gestrippt, in der PUT-Logik für Nicht-Admins aus dem
# Bestand übernommen und im PDF maskiert.
CENTRAL_CONTACT_FIELDS = (
    "strasse", "plz", "stadt", "telefon", "krankenkasse",
)


def strip_central_contact(data: dict) -> dict:
    """Returnt eine Kopie ohne Adresse/Telefon/Krankenkasse."""
    out = dict(data or {})
    for f in CENTRAL_CONTACT_FIELDS:
        out.pop(f, None)
    return out


def merge_central_contact(incoming: dict, existing: dict) -> dict:
    """Setzt Adresse/Telefon/Krankenkasse im incoming auf die Werte aus
    existing — wird in PUT für Nicht-Admins benutzt, damit ihr Save die
    bestehenden Werte nicht überschreibt (sie sehen die Felder ja gar nicht).
    """
    out = dict(incoming or {})
    for f in CENTRAL_CONTACT_FIELDS:
        if existing and existing.get(f) is not None:
            out[f] = existing.get(f)
        else:
            out.pop(f, None)
    return out


def list_admin_users_with_pin(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Admin-User, die einen PIN gesetzt haben — für Dropdowns in Unlock-UI."""
    return conn.execute(
        "SELECT id, username, full_name FROM users "
        "WHERE is_admin = 1 AND admin_pin_hash IS NOT NULL "
        "ORDER BY full_name COLLATE NOCASE, username COLLATE NOCASE"
    ).fetchall()


def set_admin_pin(conn: sqlite3.Connection, user_id: int,
                  pin: Optional[str]) -> None:
    """Hasht PIN (Werkzeug). pin=None löscht die PIN."""
    if pin is None or pin == "":
        conn.execute("UPDATE users SET admin_pin_hash = NULL WHERE id = ?",
                     (user_id,))
    else:
        conn.execute(
            "UPDATE users SET admin_pin_hash = ? WHERE id = ?",
            (generate_password_hash(pin), user_id),
        )


def verify_admin_pin(conn: sqlite3.Connection, username: str,
                     pin: str) -> Optional[sqlite3.Row]:
    """Find an admin with that username and matching PIN.
    Returns the user row (so the caller can log who approved) or None.
    """
    row = conn.execute(
        "SELECT id, username, full_name, is_admin, admin_pin_hash "
        "FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if not row or not row["is_admin"] or not row["admin_pin_hash"]:
        return None
    if not check_password_hash(row["admin_pin_hash"], pin):
        return None
    return row


def log_emergency_unlock(conn: sqlite3.Connection, patient_id: int,
                         requested_by: Optional[int],
                         approved_by: int) -> None:
    conn.execute(
        "INSERT INTO emergency_unlocks (patient_id, requested_by, approved_by) "
        "VALUES (?, ?, ?)",
        (patient_id, requested_by, approved_by),
    )


# ---------- Reset / Hard-Wipe ----------

def reset_all_protocols(conn: sqlite3.Connection) -> dict:
    """Delete every protocol (decentral + central) + comments + sequence
    counter. Returns counts of what was wiped. Patients & users stay.
    """
    # Order matters because of FKs (CASCADE handles comments).
    n_central_comments = conn.execute("SELECT COUNT(*) AS n FROM central_comments").fetchone()["n"]
    n_comments = conn.execute("SELECT COUNT(*) AS n FROM comments").fetchone()["n"]
    n_central = conn.execute("SELECT COUNT(*) AS n FROM central_protocols").fetchone()["n"]
    n_decentral = conn.execute("SELECT COUNT(*) AS n FROM protocols").fetchone()["n"]

    conn.execute("DELETE FROM central_comments")
    conn.execute("DELETE FROM comments")
    conn.execute("DELETE FROM central_protocols")
    conn.execute("DELETE FROM protocols")
    conn.execute("DELETE FROM protocol_sequence")
    # Auto-increment counter zurücksetzen, sonst startet die nächste
    # laufende Nr. bei #6 statt #1.
    conn.execute(
        "DELETE FROM sqlite_sequence WHERE name IN "
        "('protocols', 'central_protocols', 'protocol_sequence', "
        "'comments', 'central_comments')"
    )
    return {
        "decentral": n_decentral,
        "central": n_central,
        "comments": n_comments,
        "central_comments": n_central_comments,
    }


# ---------- PRIOR-Triage (Anmeldung) ----------

# Indikatoren mit ihrer SK-Zuordnung. Reihenfolge = Priorität: erstes
# Match gewinnt (SK1 schlägt SK2 schlägt SK3).
PRIOR_INDICATORS = [
    # key,                label,                                    category, group
    ("blutung",           "Lebensbedrohliche Blutung (z. B. innere Blutung)", "SK1", "leitsymptom"),
    ("a_bewusstlos",      "Bewusstlos / drohende Atemwegsverlegung", "SK1", "abcde"),
    ("b_atmung",          "Atemstörung (Atemstillstand, krankhaftes Atemgeräusch)", "SK1", "abcde"),
    ("c_kreislauf",       "Kreislaufstörung (fehlender Puls, verzögerte Nagelbettfüllung)", "SK1", "abcde"),
    ("d_bewusstsein",     "Bewusstseinsstörung (desorientiert, somnolent)", "SK1", "abcde"),
    ("e_schmerz",         "Starke Schmerzen am Körperstamm (Thorax/Abdomen/Becken)", "SK1", "abcde"),
    ("k_zyanose",         "Kind: Zyanose",                          "SK1", "kind"),
    ("k_nasenfluegeln",   "Kind: Nasenflügeln",                     "SK1", "kind"),
    ("k_blasse",          "Kind: Blässe",                           "SK1", "kind"),
    ("k_lethargie",       "Kind: Lethargie",                        "SK1", "kind"),
    ("k_haut",            "Kind: punktförmige Hauteinblutungen",    "SK1", "kind"),
    ("liegend",           "Liegend, kann nicht ohne Hilfe gehen",   "SK2", "mobilitaet"),
    ("anschlag_manv",     "ANSCHLAG-MANV (Eigenschutz erforderlich)", "SK1", "manv"),
    ("cbrn_manv",         "CBRN-MANV (Eigenschutz erforderlich)",   "SK1", "manv"),
]

PRIOR_INDICATOR_BY_KEY = {k: (label, cat, group)
                          for k, label, cat, group in PRIOR_INDICATORS}


def classify_prior(indicator_keys: list[str]) -> str:
    """Wendet die PRIOR-Logik an: erstes SK1-Match → SK1, sonst erstes
    SK2 → SK2, sonst SK3."""
    cats = {PRIOR_INDICATOR_BY_KEY.get(k, (None, None, None))[1]
            for k in (indicator_keys or [])
            if k in PRIOR_INDICATOR_BY_KEY}
    if "SK1" in cats:
        return "SK1"
    if "SK2" in cats:
        return "SK2"
    return "SK3"


def create_triage_entry(conn, *, name=None, geburtsdatum=None,
                         indicators=None, notes=None,
                         created_by=None) -> int:
    indicators = indicators or []
    category = classify_prior(indicators)
    # Patienten matchen, falls Name + Geburtsdatum ausreichend sind
    patient_id = None
    if name and geburtsdatum:
        patient_id = upsert_patient(conn, name, geburtsdatum, None)
    cur = conn.execute(
        """
        INSERT INTO triage_entries
          (patient_id, name, geburtsdatum, category, indicators, notes,
           created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (patient_id, (name or "").strip() or None,
         (geburtsdatum or "").strip() or None,
         category, _json.dumps(indicators), (notes or "").strip() or None,
         created_by),
    )
    return cur.lastrowid


def get_triage_entry(conn, tid: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM triage_entries WHERE id = ?", (tid,)
    ).fetchone()
    if not row:
        return None
    rec = dict(row)
    try:
        rec["indicators"] = _json.loads(rec.get("indicators") or "[]")
    except Exception:
        rec["indicators"] = []
    return rec


def list_triage_waiting(conn) -> list[sqlite3.Row]:
    """Patient*innen, die noch warten — sortiert nach SK-Akutität (SK1
    zuerst), innerhalb gleicher Kategorie nach Ankunftszeit (älteste oben).
    """
    return conn.execute(
        """
        SELECT t.*,
               p.name AS patient_name_resolved,
               p.geburtsdatum AS patient_geburtsdatum_resolved,
               p.stammnummer AS patient_stammnummer
        FROM triage_entries t
        LEFT JOIN patients p ON p.id = t.patient_id
        WHERE t.status = 'wartend'
        ORDER BY
            CASE t.category
                WHEN 'SK1' THEN 1
                WHEN 'SK2' THEN 2
                WHEN 'SK3' THEN 3
                ELSE 4
            END ASC,
            datetime(t.arrival_at) ASC
        """
    ).fetchall()


def list_triage_active(conn, limit: int = 50) -> list[sqlite3.Row]:
    """Aktive (in_behandlung) Triage-Einträge — für die History-Übersicht."""
    return conn.execute(
        """
        SELECT t.*, p.name AS patient_name_resolved,
               cp.laufende_nr AS protocol_laufende_nr
        FROM triage_entries t
        LEFT JOIN patients p ON p.id = t.patient_id
        LEFT JOIN central_protocols cp ON cp.id = t.treatment_protocol_id
        WHERE t.status IN ('in_behandlung', 'abgeschlossen')
        ORDER BY datetime(t.arrival_at) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def start_triage_treatment(conn, tid: int,
                            protocol_id: Optional[int] = None) -> bool:
    cur = conn.execute(
        """
        UPDATE triage_entries
        SET status = 'in_behandlung',
            treatment_started_at = datetime('now'),
            treatment_protocol_id = COALESCE(?, treatment_protocol_id)
        WHERE id = ? AND status = 'wartend'
        """,
        (protocol_id, tid),
    )
    return cur.rowcount > 0


def link_triage_to_central_protocol(conn, tid: int, protocol_id: int) -> None:
    conn.execute(
        """
        UPDATE triage_entries
        SET treatment_protocol_id = ?,
            status = CASE WHEN status = 'wartend'
                          THEN 'in_behandlung' ELSE status END,
            treatment_started_at = COALESCE(treatment_started_at,
                                             datetime('now'))
        WHERE id = ?
        """,
        (protocol_id, tid),
    )


def cancel_triage_entry(conn, tid: int) -> bool:
    cur = conn.execute(
        "UPDATE triage_entries SET status = 'abgebrochen' "
        "WHERE id = ? AND status = 'wartend'",
        (tid,),
    )
    return cur.rowcount > 0
