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

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    prefix       TEXT NOT NULL DEFAULT 'EH',
    start_date   TEXT,
    end_date     TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_event_permissions (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    can_view    INTEGER NOT NULL DEFAULT 1,
    can_create  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, event_id)
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
    event_id              INTEGER REFERENCES events(id) ON DELETE SET NULL,
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
    event_id      INTEGER REFERENCES events(id) ON DELETE SET NULL,
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
    event_id    INTEGER REFERENCES events(id) ON DELETE SET NULL,
    seq_no      INTEGER,
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
    event_id                 INTEGER REFERENCES events(id) ON DELETE SET NULL,
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
    treatment_started_by     INTEGER REFERENCES users(id),
    treatment_finished_at    TEXT,
    treatment_protocol_id    INTEGER REFERENCES central_protocols(id)
                              ON DELETE SET NULL,
    created_by               INTEGER REFERENCES users(id),
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_triage_status ON triage_entries(status);
CREATE INDEX IF NOT EXISTS idx_triage_category ON triage_entries(category);

CREATE TABLE IF NOT EXISTS medications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id    INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    dosage        TEXT,
    morgens       INTEGER NOT NULL DEFAULT 0,
    mittags       INTEGER NOT NULL DEFAULT 0,
    abends        INTEGER NOT NULL DEFAULT 0,
    nachts        INTEGER NOT NULL DEFAULT 0,
    bei_bedarf    INTEGER NOT NULL DEFAULT 0,
    lagerung      TEXT,
    notes         TEXT,
    start_date    TEXT,
    end_date      TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_by    INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_medications_patient ON medications(patient_id);

CREATE TABLE IF NOT EXISTS medication_administrations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    medication_id    INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
    day_date         TEXT NOT NULL,   -- YYYY-MM-DD
    slot             TEXT NOT NULL,   -- 'morgens' | 'mittags' | 'abends' | 'nachts' | 'bedarf'
    administered_at  TEXT NOT NULL DEFAULT (datetime('now')),
    administered_by  INTEGER REFERENCES users(id),
    notes            TEXT,
    UNIQUE(medication_id, day_date, slot)
);
CREATE INDEX IF NOT EXISTS idx_med_admin_med ON medication_administrations(medication_id);
CREATE INDEX IF NOT EXISTS idx_med_admin_day ON medication_administrations(day_date);
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

        _ensure_default_event(conn)

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
        if "event_id" not in proto_cols:
            conn.execute("ALTER TABLE protocols ADD COLUMN event_id INTEGER")
        if "global_id" not in proto_cols:
            conn.execute("ALTER TABLE protocols ADD COLUMN global_id INTEGER")
        cent_cols = {row[1] for row in conn.execute("PRAGMA table_info(central_protocols)")}
        if "event_id" not in cent_cols:
            conn.execute("ALTER TABLE central_protocols ADD COLUMN event_id INTEGER")
        if "global_id" not in cent_cols:
            conn.execute("ALTER TABLE central_protocols ADD COLUMN global_id INTEGER")
        if "laufende_nr" not in cent_cols:
            conn.execute("ALTER TABLE central_protocols ADD COLUMN laufende_nr TEXT")

        seq_cols = {row[1] for row in conn.execute("PRAGMA table_info(protocol_sequence)")}
        if "event_id" not in seq_cols:
            conn.execute("ALTER TABLE protocol_sequence ADD COLUMN event_id INTEGER")
        if "seq_no" not in seq_cols:
            conn.execute("ALTER TABLE protocol_sequence ADD COLUMN seq_no INTEGER")

        # Migration: triage treatment_finished_at column.
        triage_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(triage_entries)")
        }
        if "event_id" not in triage_cols:
            conn.execute("ALTER TABLE triage_entries ADD COLUMN event_id INTEGER")
        if "treatment_finished_at" not in triage_cols:
            conn.execute(
                "ALTER TABLE triage_entries ADD COLUMN treatment_finished_at TEXT"
            )
        if "treatment_started_by" not in triage_cols:
            conn.execute(
                "ALTER TABLE triage_entries ADD COLUMN treatment_started_by INTEGER"
            )

        default_event_id = get_default_event_id(conn)
        conn.execute(
            "UPDATE protocols SET event_id = ? WHERE event_id IS NULL",
            (default_event_id,),
        )
        conn.execute(
            "UPDATE central_protocols SET event_id = ? WHERE event_id IS NULL",
            (default_event_id,),
        )
        conn.execute(
            "UPDATE triage_entries SET event_id = ? WHERE event_id IS NULL",
            (default_event_id,),
        )
        conn.execute(
            "UPDATE protocol_sequence SET event_id = ? WHERE event_id IS NULL",
            (default_event_id,),
        )
        conn.execute(
            "UPDATE protocol_sequence SET seq_no = id WHERE seq_no IS NULL"
        )

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
        for u in conn.execute("SELECT id, role, is_admin FROM users").fetchall():
            for e in conn.execute("SELECT id FROM events").fetchall():
                exists = conn.execute(
                    "SELECT 1 FROM user_event_permissions "
                    "WHERE user_id = ? AND event_id = ?",
                    (u["id"], e["id"]),
                ).fetchone()
                if exists or u["is_admin"]:
                    continue
                can_create = u["role"] in ("full", "zentral_writer", "triage_intake")
                set_user_event_permission(
                    conn, u["id"], e["id"], can_view=True, can_create=can_create)
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
        event_id = get_default_event_id(conn)
        row = conn.execute(
            "SELECT COALESCE(MAX(seq_no), 0) + 1 AS next_no "
            "FROM protocol_sequence WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        seq_no = row["next_no"] if row else 1
        cur = conn.execute(
            "INSERT INTO protocol_sequence "
            "(event_id, seq_no, source_type, source_id) VALUES (?, ?, ?, ?)",
            (event_id, seq_no, r["source"], r["source_id"]),
        )
        gid = cur.lastrowid
        prefix = get_event_prefix(conn, event_id)
        nr = f"#{prefix}{seq_no}"
        if r["source"] == "decentral":
            conn.execute(
                "UPDATE protocols SET event_id = ?, global_id = ?, laufende_nr = ? WHERE id = ?",
                (event_id, gid, nr, r["source_id"]),
            )
        else:
            conn.execute(
                "UPDATE central_protocols SET event_id = ?, global_id = ?, laufende_nr = ? WHERE id = ?",
                (event_id, gid, nr, r["source_id"]),
            )


# ---------- Events / Veranstaltungen ----------

def _ensure_default_event(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM events ORDER BY id LIMIT 1").fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO events (name, prefix, is_active) VALUES (?, ?, 1)",
        ("Standard-Veranstaltung", "EH"),
    )
    return cur.lastrowid


def get_default_event_id(conn: sqlite3.Connection) -> int:
    return _ensure_default_event(conn)


def get_event(conn: sqlite3.Connection, event_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def get_event_prefix(conn: sqlite3.Connection, event_id: Optional[int]) -> str:
    if event_id:
        row = get_event(conn, event_id)
        if row and row["prefix"]:
            return row["prefix"]
    return "EH"


def sanitize_event_prefix(value: str) -> str:
    prefix = "".join(ch for ch in (value or "").strip().upper()
                     if ch.isalnum() or ch in ("-", "_"))
    return prefix[:12] or "EH"


def list_events(conn: sqlite3.Connection, *, active_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM events"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY COALESCE(start_date, '9999-12-31'), name COLLATE NOCASE"
    return conn.execute(sql).fetchall()


def list_user_events(conn: sqlite3.Connection, user_id: int,
                     is_admin: bool = False) -> list[sqlite3.Row]:
    if is_admin:
        return list_events(conn, active_only=True)
    return conn.execute(
        """
        SELECT e.*
        FROM events e
        JOIN user_event_permissions p ON p.event_id = e.id
        WHERE p.user_id = ? AND p.can_view = 1 AND e.is_active = 1
        ORDER BY COALESCE(e.start_date, '9999-12-31'), e.name COLLATE NOCASE
        """,
        (user_id,),
    ).fetchall()


def user_can_view_event(conn: sqlite3.Connection, user_id: int,
                        event_id: Optional[int], is_admin: bool = False) -> bool:
    if is_admin:
        return True
    if not event_id:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM user_event_permissions
        WHERE user_id = ? AND event_id = ? AND can_view = 1
        """,
        (user_id, event_id),
    ).fetchone()
    return bool(row)


def user_can_create_in_event(conn: sqlite3.Connection, user_id: int,
                             event_id: Optional[int], is_admin: bool = False) -> bool:
    if is_admin:
        return True
    if not event_id:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM user_event_permissions
        WHERE user_id = ? AND event_id = ? AND can_view = 1 AND can_create = 1
        """,
        (user_id, event_id),
    ).fetchone()
    return bool(row)


def create_event(conn: sqlite3.Connection, name: str, prefix: str,
                 start_date: Optional[str], end_date: Optional[str]) -> int:
    cur = conn.execute(
        "INSERT INTO events (name, prefix, start_date, end_date, is_active) "
        "VALUES (?, ?, ?, ?, 1)",
        (name.strip(), sanitize_event_prefix(prefix), start_date or None,
         end_date or None),
    )
    return cur.lastrowid


def update_event(conn: sqlite3.Connection, event_id: int, *, name: str,
                 prefix: str, start_date: Optional[str],
                 end_date: Optional[str], is_active: bool) -> None:
    conn.execute(
        """
        UPDATE events
           SET name = ?, prefix = ?, start_date = ?, end_date = ?, is_active = ?
         WHERE id = ?
        """,
        (name.strip(), sanitize_event_prefix(prefix), start_date or None,
         end_date or None, 1 if is_active else 0, event_id),
    )


def get_user_event_permissions(conn: sqlite3.Connection, user_id: int) -> dict[int, dict]:
    rows = conn.execute(
        """
        SELECT event_id, can_view, can_create
        FROM user_event_permissions
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchall()
    return {
        r["event_id"]: {
            "can_view": bool(r["can_view"]),
            "can_create": bool(r["can_create"]),
        }
        for r in rows
    }


def set_user_event_permission(conn: sqlite3.Connection, user_id: int,
                              event_id: int, *, can_view: bool,
                              can_create: bool) -> None:
    conn.execute(
        """
        INSERT INTO user_event_permissions
          (user_id, event_id, can_view, can_create)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, event_id)
        DO UPDATE SET can_view = excluded.can_view,
                      can_create = excluded.can_create
        """,
        (user_id, event_id, 1 if can_view else 0,
         1 if (can_view and can_create) else 0),
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


def list_patients(conn: sqlite3.Connection,
                  event_id: Optional[int] = None) -> list[sqlite3.Row]:
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
          FROM protocols
          WHERE (? IS NULL OR event_id = ?)
          GROUP BY patient_id
        ) d ON d.patient_id = p.id
        LEFT JOIN (
          SELECT patient_id, COUNT(*) AS n, MAX(datum) AS last_c
          FROM central_protocols
          WHERE patient_id IS NOT NULL AND (? IS NULL OR event_id = ?)
          GROUP BY patient_id
        ) c ON c.patient_id = p.id
        WHERE (COALESCE(d.n, 0) + COALESCE(c.n, 0)) > 0
        ORDER BY p.name COLLATE NOCASE ASC
        """,
        (event_id, event_id, event_id, event_id),
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
                      source_id: int, event_id: Optional[int]) -> tuple[int, str]:
    """Insert into protocol_sequence and return (global_id, laufende_nr)."""
    event_id = event_id or get_default_event_id(conn)
    row = conn.execute(
        "SELECT COALESCE(MAX(seq_no), 0) + 1 AS next_no "
        "FROM protocol_sequence WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    seq_no = row["next_no"] if row else 1
    cur = conn.execute(
        "INSERT INTO protocol_sequence (event_id, seq_no, source_type, source_id) "
        "VALUES (?, ?, ?, ?)",
        (event_id, seq_no, source_type, source_id),
    )
    gid = cur.lastrowid
    return gid, f"#{get_event_prefix(conn, event_id)}{seq_no}"


def create_protocol(conn: sqlite3.Connection, patient_id: int,
                    data: dict, created_by: Optional[int],
                    event_id: Optional[int] = None) -> int:
    event_id = event_id or get_default_event_id(conn)
    cols = ["event_id", "patient_id"] + list(PROTOCOL_FIELDS) + ["created_by"]
    placeholders = ",".join(["?"] * len(cols))
    values = [event_id, patient_id] + [data.get(f) or None for f in PROTOCOL_FIELDS] + [created_by]
    cur = conn.execute(
        f"INSERT INTO protocols ({','.join(cols)}) VALUES ({placeholders})",
        values,
    )
    new_id = cur.lastrowid
    gid, nr = _assign_global_id(conn, "decentral", new_id, event_id)
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
               e.name          AS event_name,
               e.prefix        AS event_prefix,
               p.name          AS patient_name,
               p.geburtsdatum  AS patient_geburtsdatum,
               p.stammnummer   AS patient_stammnummer,
               u.username      AS author_username,
               u.full_name     AS author_full_name
        FROM protocols pr
        JOIN patients p ON p.id = pr.patient_id
        LEFT JOIN events e ON e.id = pr.event_id
        LEFT JOIN users u ON u.id = pr.created_by
        WHERE pr.id = ?
        """,
        (protocol_id,),
    ).fetchone()


def list_protocols(conn: sqlite3.Connection, *,
                   patient_id: Optional[int] = None,
                   event_id: Optional[int] = None,
                   date_from: Optional[str] = None,
                   date_to: Optional[str] = None,
                   stammnummer: Optional[str] = None,
                   name_query: Optional[str] = None) -> list[sqlite3.Row]:
    sql = [
        """
        SELECT pr.id, pr.patient_id, pr.laufende_nr, pr.deh,
               pr.event_id, e.name AS event_name,
               pr.unfall_datum_uhrzeit, pr.eh_datum_uhrzeit,
               pr.name_ersthelfer, pr.created_at,
               p.name AS patient_name, p.geburtsdatum AS patient_geburtsdatum,
               p.stammnummer AS patient_stammnummer
        FROM protocols pr
        JOIN patients p ON p.id = pr.patient_id
        LEFT JOIN events e ON e.id = pr.event_id
        WHERE 1=1
        """
    ]
    params: list = []
    if event_id is not None:
        sql.append("AND pr.event_id = ?")
        params.append(event_id)
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


def list_patient_protocols(conn: sqlite3.Connection, patient_id: int,
                           event_id: Optional[int] = None) -> list[sqlite3.Row]:
    return list_protocols(conn, patient_id=patient_id, event_id=event_id)


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
                           event_id: Optional[int] = None,
                           created_by: Optional[int] = None
                           ) -> list[sqlite3.Row]:
    sql = [
        """
        SELECT cp.id, cp.patient_id, cp.einsatznummer, cp.datum,
               cp.event_id, e.name AS event_name,
               cp.name_summary, cp.global_id, cp.laufende_nr,
               cp.created_by, cp.created_at, cp.updated_at,
               p.name AS patient_name, p.geburtsdatum AS patient_geburtsdatum,
               p.stammnummer AS patient_stammnummer,
               u.username  AS author_username,
               u.full_name AS author_full_name
        FROM central_protocols cp
        LEFT JOIN events e ON e.id = cp.event_id
        LEFT JOIN patients p ON p.id = cp.patient_id
        LEFT JOIN users u ON u.id = cp.created_by
        WHERE 1=1
        """
    ]
    params: list = []
    if event_id is not None:
        sql.append("AND cp.event_id = ?")
        params.append(event_id)
    if patient_id is not None:
        sql.append("AND cp.patient_id = ?")
        params.append(patient_id)
    if created_by is not None:
        sql.append("AND cp.created_by = ?")
        params.append(created_by)
    sql.append("ORDER BY datetime(cp.updated_at) DESC")
    return conn.execute("\n".join(sql), params).fetchall()


def list_unified_protocols(conn: sqlite3.Connection, *,
                           event_id: Optional[int] = None,
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
    if event_id is not None:
        decentral_filter += " AND p.event_id = ?"
        decentral_params.append(event_id)
        central_filter += " AND c.event_id = ?"
        central_params.append(event_id)

    parts = []
    params: list = []
    if source_filter != "central":
        parts.append(f"""
            SELECT 'decentral' AS source, p.id AS source_id,
                   p.event_id, e.name AS event_name,
                   p.global_id, p.laufende_nr,
                   p.eh_datum_uhrzeit AS event_date,
                   p.name_ersthelfer AS responder,
                   p.created_at,
                   p.patient_id,
                   pat.name AS patient_name,
                   pat.geburtsdatum AS patient_geburtsdatum,
                   pat.stammnummer AS patient_stammnummer
            FROM protocols p
            LEFT JOIN events e ON e.id = p.event_id
            LEFT JOIN patients pat ON pat.id = p.patient_id
            WHERE p.global_id IS NOT NULL{decentral_filter}
        """)
        params.extend(decentral_params)
    if source_filter != "decentral":
        parts.append(f"""
            SELECT 'central' AS source, c.id AS source_id,
                   c.event_id, e.name AS event_name,
                   c.global_id, c.laufende_nr,
                   c.datum AS event_date,
                   c.name_summary AS responder,
                   c.created_at,
                   c.patient_id,
                   COALESCE(pat.name, c.name_summary) AS patient_name,
                   pat.geburtsdatum AS patient_geburtsdatum,
                   pat.stammnummer AS patient_stammnummer
            FROM central_protocols c
            LEFT JOIN events e ON e.id = c.event_id
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
        SELECT cp.*, e.name AS event_name, e.prefix AS event_prefix,
               p.name AS patient_name, p.geburtsdatum AS patient_geburtsdatum,
               p.stammnummer AS patient_stammnummer
        FROM central_protocols cp
        LEFT JOIN events e ON e.id = cp.event_id
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
                            created_by: Optional[int],
                            event_id: Optional[int] = None) -> int:
    event_id = event_id or get_default_event_id(conn)
    patient_id = _link_central_to_patient(conn, data)
    cur = conn.execute(
        """
        INSERT INTO central_protocols
          (event_id, patient_id, einsatznummer, datum, name_summary, data, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            patient_id,
            _scalar(data.get("einsatznummer")) or None,
            _scalar(data.get("datum")) or None,
            _name_summary(data) or None,
            _json.dumps(data, ensure_ascii=False),
            created_by,
        ),
    )
    new_id = cur.lastrowid
    gid, nr = _assign_global_id(conn, "central", new_id, event_id)
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


def delete_patient(conn: sqlite3.Connection, patient_id: int) -> bool:
    """Patient löschen — entfernt damit auch alle dEH-Berichte
    (CASCADE), die zentralen Berichte und Triage-Einträge bleiben mit
    patient_id=NULL erhalten (ON DELETE SET NULL).
    Audit-Log + Notfall-Unlocks fallen ebenfalls weg (CASCADE).
    """
    cur = conn.execute("DELETE FROM patients WHERE id = ?", (patient_id,))
    return cur.rowcount > 0


def patient_protocol_counts(conn: sqlite3.Connection, patient_id: int,
                            event_id: Optional[int] = None) -> dict:
    """How many decentral and central protocols exist for this patient."""
    event_sql = " AND event_id = ?" if event_id is not None else ""
    params = (patient_id, event_id) if event_id is not None else (patient_id,)
    decentral = conn.execute(
        f"SELECT COUNT(*) AS n FROM protocols WHERE patient_id = ?{event_sql}",
        params,
    ).fetchone()["n"]
    central = conn.execute(
        f"SELECT COUNT(*) AS n FROM central_protocols WHERE patient_id = ?{event_sql}",
        params,
    ).fetchone()["n"]
    return {"decentral": decentral, "central": central}


def patient_last_treatment(conn: sqlite3.Connection,
                           patient_id: int,
                           event_id: Optional[int] = None) -> Optional[str]:
    """Most recent treatment date across both protocol types, or None."""
    row = conn.execute(
        """
        SELECT MAX(d) AS last FROM (
            SELECT eh_datum_uhrzeit AS d FROM protocols
            WHERE patient_id = ? AND (? IS NULL OR event_id = ?)
            UNION ALL
            SELECT datum AS d FROM central_protocols
            WHERE patient_id = ? AND (? IS NULL OR event_id = ?)
        )
        """,
        (patient_id, event_id, event_id, patient_id, event_id, event_id),
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
                    patient_id: int,
                    event_id: Optional[int] = None) -> list[dict]:
    """Alle Berichte (dezentral + zentral) eines Patienten, chronologisch
    absteigend. Wird für die Vorbehandlungs-Popup-Liste genutzt."""
    rows = conn.execute(
        """
        SELECT 'decentral' AS source, id, laufende_nr,
               COALESCE(eh_datum_uhrzeit, created_at) AS event_date,
               name_ersthelfer AS responder, created_at
        FROM protocols WHERE patient_id = ?
          AND (? IS NULL OR event_id = ?)
        UNION ALL
        SELECT 'central' AS source, id, laufende_nr,
               COALESCE(datum, created_at) AS event_date,
               name_summary AS responder, created_at
        FROM central_protocols WHERE patient_id = ?
          AND (? IS NULL OR event_id = ?)
        ORDER BY event_date DESC, created_at DESC
        """,
        (patient_id, event_id, event_id, patient_id, event_id, event_id),
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


def dashboard_stats(conn: sqlite3.Connection, ref_date,
                    event_id: Optional[int] = None) -> dict:
    """Liefert Statistiken bezogen auf ref_date (datetime.date)."""
    from datetime import date as _date, timedelta
    iso = ref_date.isoformat()

    def one(d_iso):
        event_sql = " AND event_id = ?" if event_id is not None else ""
        params = (d_iso, event_id) if event_id is not None else (d_iso,)
        d = _count_decentral(conn, f"{_treatment_date_decentral()} = ?{event_sql}", params)
        c = _count_central(conn, f"{_treatment_date_central()} = ?{event_sql}", params)
        return {"d": d, "c": c}

    def rng(start_iso, end_iso):
        event_sql = " AND event_id = ?" if event_id is not None else ""
        params = ((start_iso, end_iso, event_id)
                  if event_id is not None else (start_iso, end_iso))
        d = _count_decentral(conn,
            f"{_treatment_date_decentral()} BETWEEN ? AND ?{event_sql}",
            params)
        c = _count_central(conn,
            f"{_treatment_date_central()} BETWEEN ? AND ?{event_sql}",
            params)
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
            "d": _count_decentral(conn, "event_id = ?" if event_id is not None else "1=1",
                                  (event_id,) if event_id is not None else ()),
            "c": _count_central(conn, "event_id = ?" if event_id is not None else "1=1",
                                (event_id,) if event_id is not None else ()),
        },
    }


def dashboard_daily_counts(conn: sqlite3.Connection, *,
                           end_date, days: int = 14,
                           event_id: Optional[int] = None) -> list[dict]:
    """Pro Tag (rückwärts ab end_date) Zähler dezentral/zentral."""
    from datetime import timedelta
    out = []
    for i in range(days - 1, -1, -1):
        d = end_date - timedelta(days=i)
        d_iso = d.isoformat()
        event_sql = " AND event_id = ?" if event_id is not None else ""
        params = (d_iso, event_id) if event_id is not None else (d_iso,)
        out.append({
            "date": d,
            "decentral": _count_decentral(
                conn, f"{_treatment_date_decentral()} = ?{event_sql}", params
            ),
            "central": _count_central(
                conn, f"{_treatment_date_central()} = ?{event_sql}", params
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
                             limit: int = 5,
                             event_id: Optional[int] = None) -> list[sqlite3.Row]:
    where = "WHERE name_ersthelfer IS NOT NULL AND TRIM(name_ersthelfer) != ''"
    params: list = []
    if event_id is not None:
        where += " AND event_id = ?"
        params.append(event_id)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT TRIM(name_ersthelfer) AS responder, COUNT(*) AS n
        FROM protocols
        {where}
        GROUP BY responder COLLATE NOCASE
        ORDER BY n DESC, responder COLLATE NOCASE
        LIMIT ?
        """,
        params,
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
                         created_by=None, event_id=None) -> int:
    indicators = indicators or []
    category = classify_prior(indicators)
    # Patienten matchen, falls Name + Geburtsdatum ausreichend sind
    patient_id = None
    if name and geburtsdatum:
        patient_id = upsert_patient(conn, name, geburtsdatum, None)
    cur = conn.execute(
        """
        INSERT INTO triage_entries
          (event_id, patient_id, name, geburtsdatum, category, indicators, notes,
           created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id or get_default_event_id(conn), patient_id, (name or "").strip() or None,
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


def get_triage_for_protocol(conn, protocol_id: int) -> Optional[dict]:
    """Triage-Eintrag, der zu einem zentralen Bericht verlinkt ist —
    plus aufgelöste Anmelder-/Behandler-Namen für die Akte."""
    row = conn.execute(
        """
        SELECT t.*,
               u_anm.username     AS anmelder_username,
               u_anm.full_name    AS anmelder_full_name,
               u_start.username   AS started_by_username,
               u_start.full_name  AS started_by_full_name,
               cp.created_by      AS protocol_created_by,
               u_beh.username     AS behandler_username,
               u_beh.full_name    AS behandler_full_name,
               cp.name_summary    AS behandler_name_summary
        FROM triage_entries t
        LEFT JOIN users u_anm           ON u_anm.id = t.created_by
        LEFT JOIN users u_start         ON u_start.id = t.treatment_started_by
        LEFT JOIN central_protocols cp  ON cp.id = t.treatment_protocol_id
        LEFT JOIN users u_beh           ON u_beh.id = cp.created_by
        WHERE t.treatment_protocol_id = ?
        ORDER BY datetime(t.arrival_at) DESC
        LIMIT 1
        """,
        (protocol_id,),
    ).fetchone()
    if not row:
        return None
    rec = dict(row)
    try:
        rec["indicators"] = _json.loads(rec.get("indicators") or "[]")
    except Exception:
        rec["indicators"] = []
    return rec


def protocol_summary(conn: sqlite3.Connection, source: str,
                     pid: int) -> Optional[dict]:
    """Kompakte Zusammenfassung eines Berichts für das Vorbehandlungs-Popup.
    Liefert Datum, Was, Maßnahmen, Ausgang, Behandler — ohne die kompletten
    Felder. Wird per /api/protocol-summary ausgeliefert."""
    if source == "central":
        rec = get_central_protocol(conn, pid)
        if not rec:
            return None
        d = rec.get("data") or {}
        # Was: notfallsituation > notfallart > verletzung
        was = (d.get("notfallsituation") or d.get("notfallart")
               or d.get("verletzung") or "").strip()
        if d.get("notfallart_sonstige"):
            was = (was + " · " + d["notfallart_sonstige"]).strip(" ·")
        # Maßnahmen: massnahme + massnahmen_sonstiges
        massn_raw = d.get("massnahme")
        if isinstance(massn_raw, list):
            massn = ", ".join(str(x) for x in massn_raw if x)
        else:
            massn = (massn_raw or "").strip()
        if d.get("massnahmen_sonstiges"):
            massn = (massn + " · " + d["massnahmen_sonstiges"]).strip(" ·")
        # Ausgang: uebergabe_an + ergebnis
        aus_parts = []
        if d.get("uebergabe_an"):
            aus_parts.append(f"Übergabe an {d['uebergabe_an']}")
        if d.get("ergebnis"):
            aus_parts.append(str(d["ergebnis"]))
        if d.get("uebergabezeit"):
            aus_parts.append(f"um {d['uebergabezeit']}")
        ausgang = " · ".join(aus_parts)
        # Behandler: einsatzkraft1 / einsatzkraft2 oder name_summary
        behandler_parts = [
            x for x in (d.get("einsatzkraft1"), d.get("einsatzkraft2")) if x
        ]
        behandler = ", ".join(behandler_parts) or rec.get("name_summary") or ""
        # Diagnose
        diagnose = (d.get("erstdiagnose") or "").strip()
        return {
            "source": "central",
            "id": pid,
            "laufende_nr": rec.get("laufende_nr") or f"#zEH{pid}",
            "type_label": "Zentrale Erste Hilfe",
            "event_date": d.get("datum") or rec.get("created_at"),
            "what": was,
            "diagnose": diagnose,
            "massnahmen": massn,
            "ausgang": ausgang,
            "behandler": behandler,
            "detail_url": f"/central/{pid}",
        }
    rec = get_protocol(conn, pid)
    if not rec:
        return None
    return {
        "source": "decentral",
        "id": pid,
        "laufende_nr": rec["laufende_nr"] or f"#dEH{pid}",
        "type_label": "Dezentrale Erste Hilfe (DGUV-1)",
        "event_date": rec["eh_datum_uhrzeit"] or rec["unfall_datum_uhrzeit"]
                      or rec["created_at"],
        "what": (rec["unfallhergang"] or rec["art_umfang_verletzung"] or "").strip(),
        "diagnose": (rec["art_umfang_verletzung"] or "").strip(),
        "massnahmen": (rec["art_weise_massnahmen"] or "").strip(),
        "ausgang": (rec["verbrauchtes_material"] or "").strip(),
        "behandler": (rec["name_ersthelfer"] or "").strip(),
        "detail_url": f"/protocols/{pid}",
    }


def list_triage_waiting(conn, event_id: Optional[int] = None) -> list[sqlite3.Row]:
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
          AND (? IS NULL OR t.event_id = ?)
        ORDER BY
            CASE t.category
                WHEN 'SK1' THEN 1
                WHEN 'SK2' THEN 2
                WHEN 'SK3' THEN 3
                ELSE 4
            END ASC,
            datetime(t.arrival_at) ASC
        """
        ,
        (event_id, event_id),
    ).fetchall()


def list_triage_active(conn, limit: int = 50,
                       event_id: Optional[int] = None) -> list[sqlite3.Row]:
    """Aktuell in Behandlung — abgeschlossene Einträge tauchen hier nicht mehr auf."""
    return conn.execute(
        """
        SELECT t.*, p.name AS patient_name_resolved,
               cp.laufende_nr AS protocol_laufende_nr,
               u.username AS started_by_username,
               u.full_name AS started_by_full_name
        FROM triage_entries t
        LEFT JOIN patients p ON p.id = t.patient_id
        LEFT JOIN central_protocols cp ON cp.id = t.treatment_protocol_id
        LEFT JOIN users u ON u.id = t.treatment_started_by
        WHERE t.status = 'in_behandlung'
          AND (? IS NULL OR t.event_id = ?)
        ORDER BY datetime(t.arrival_at) DESC
        LIMIT ?
        """,
        (event_id, event_id, limit),
    ).fetchall()


def finish_triage_treatment(conn, tid: int) -> bool:
    """Behandlung abschließen — Eintrag verschwindet aus Triage-Listen."""
    cur = conn.execute(
        """
        UPDATE triage_entries
        SET status = 'abgeschlossen',
            treatment_finished_at = datetime('now')
        WHERE id = ? AND status = 'in_behandlung'
        """,
        (tid,),
    )
    return cur.rowcount > 0


def reopen_triage_treatment(conn, tid: int) -> bool:
    """Versehentlich abgeschlossene Behandlung wieder öffnen."""
    cur = conn.execute(
        """
        UPDATE triage_entries
        SET status = 'in_behandlung',
            treatment_finished_at = NULL
        WHERE id = ? AND status = 'abgeschlossen'
        """,
        (tid,),
    )
    return cur.rowcount > 0


def list_triage_recently_finished(conn, limit: int = 10,
                                  event_id: Optional[int] = None) -> list[sqlite3.Row]:
    """Zuletzt abgeschlossene Behandlungen — für die "Wieder öffnen"-Liste."""
    return conn.execute(
        """
        SELECT t.*, p.name AS patient_name_resolved,
               cp.laufende_nr AS protocol_laufende_nr,
               u.username AS started_by_username,
               u.full_name AS started_by_full_name
        FROM triage_entries t
        LEFT JOIN patients p ON p.id = t.patient_id
        LEFT JOIN central_protocols cp ON cp.id = t.treatment_protocol_id
        LEFT JOIN users u ON u.id = t.treatment_started_by
        WHERE t.status = 'abgeschlossen'
          AND (? IS NULL OR t.event_id = ?)
        ORDER BY datetime(COALESCE(t.treatment_finished_at, t.arrival_at)) DESC
        LIMIT ?
        """,
        (event_id, event_id, limit),
    ).fetchall()


def start_triage_treatment(conn, tid: int,
                            protocol_id: Optional[int] = None,
                            started_by: Optional[int] = None) -> bool:
    cur = conn.execute(
        """
        UPDATE triage_entries
        SET status = 'in_behandlung',
            treatment_started_at = COALESCE(treatment_started_at,
                                             datetime('now')),
            treatment_started_by = CASE WHEN ? IS NOT NULL
                                        THEN ? ELSE treatment_started_by END,
            treatment_protocol_id = COALESCE(?, treatment_protocol_id)
        WHERE id = ? AND status IN ('wartend', 'in_behandlung')
        """,
        (started_by, started_by, protocol_id, tid),
    )
    return cur.rowcount > 0


def link_triage_to_central_protocol(conn, tid: int, protocol_id: int,
                                    started_by: Optional[int] = None) -> None:
    conn.execute(
        """
        UPDATE triage_entries
        SET treatment_protocol_id = ?,
            status = CASE WHEN status = 'wartend'
                          THEN 'in_behandlung' ELSE status END,
            treatment_started_at = COALESCE(treatment_started_at,
                                             datetime('now')),
            treatment_started_by = COALESCE(?, treatment_started_by)
        WHERE id = ?
        """,
        (protocol_id, started_by, tid),
    )


def cancel_triage_entry(conn, tid: int) -> bool:
    cur = conn.execute(
        "UPDATE triage_entries SET status = 'abgebrochen' "
        "WHERE id = ? AND status = 'wartend'",
        (tid,),
    )
    return cur.rowcount > 0


# ---------- Medikamenten-Plan ----------

MEDICATION_SLOTS = ("morgens", "mittags", "abends", "nachts", "bedarf")
MEDICATION_SLOT_LABELS = {
    "morgens": "Morgens",
    "mittags": "Mittags",
    "abends": "Abends",
    "nachts": "Nachts",
    "bedarf": "B.B.",  # Bei Bedarf
}


def create_medication(conn, *, patient_id: int, name: str,
                      dosage: Optional[str] = None,
                      morgens: bool = False, mittags: bool = False,
                      abends: bool = False, nachts: bool = False,
                      bei_bedarf: bool = False,
                      lagerung: Optional[str] = None,
                      notes: Optional[str] = None,
                      start_date: Optional[str] = None,
                      end_date: Optional[str] = None,
                      created_by: Optional[int] = None) -> int:
    cur = conn.execute(
        """
        INSERT INTO medications
          (patient_id, name, dosage, morgens, mittags, abends, nachts,
           bei_bedarf, lagerung, notes, start_date, end_date, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (patient_id, name.strip(), (dosage or "").strip() or None,
         1 if morgens else 0, 1 if mittags else 0,
         1 if abends else 0, 1 if nachts else 0,
         1 if bei_bedarf else 0,
         (lagerung or "").strip() or None,
         (notes or "").strip() or None,
         start_date or None, end_date or None, created_by),
    )
    return cur.lastrowid


def list_medications_for_patient(conn, patient_id: int,
                                  include_inactive: bool = False
                                  ) -> list[sqlite3.Row]:
    sql = "SELECT * FROM medications WHERE patient_id = ?"
    if not include_inactive:
        sql += " AND active = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    return conn.execute(sql, (patient_id,)).fetchall()


def get_medication(conn, mid: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM medications WHERE id = ?",
                        (mid,)).fetchone()


def delete_medication(conn, mid: int) -> bool:
    cur = conn.execute("DELETE FROM medications WHERE id = ?", (mid,))
    return cur.rowcount > 0


def set_medication_active(conn, mid: int, active: bool) -> bool:
    cur = conn.execute(
        "UPDATE medications SET active = ? WHERE id = ?",
        (1 if active else 0, mid),
    )
    return cur.rowcount > 0


def record_medication_administration(conn, *, medication_id: int,
                                      day_date: str, slot: str,
                                      administered_by: Optional[int],
                                      notes: Optional[str] = None) -> bool:
    """Trägt eine Vergabe ein. Idempotent über UNIQUE(med,day,slot) —
    erneuter Eintrag schlägt fehl und wird gemeldet."""
    try:
        conn.execute(
            """
            INSERT INTO medication_administrations
              (medication_id, day_date, slot, administered_by, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (medication_id, day_date, slot, administered_by,
             (notes or "").strip() or None),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def remove_medication_administration(conn, *, medication_id: int,
                                      day_date: str, slot: str) -> bool:
    cur = conn.execute(
        """
        DELETE FROM medication_administrations
        WHERE medication_id = ? AND day_date = ? AND slot = ?
        """,
        (medication_id, day_date, slot),
    )
    return cur.rowcount > 0


def list_administrations_for_patient(conn, patient_id: int,
                                      date_from: str, date_to: str
                                      ) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.*, u.username AS by_username, u.full_name AS by_full_name
        FROM medication_administrations a
        JOIN medications m ON m.id = a.medication_id
        LEFT JOIN users u ON u.id = a.administered_by
        WHERE m.patient_id = ?
          AND a.day_date BETWEEN ? AND ?
        ORDER BY a.day_date, a.slot, a.administered_at
        """,
        (patient_id, date_from, date_to),
    ).fetchall()


def list_patients_with_medications(conn) -> list[sqlite3.Row]:
    """Patienten, die einen aktiven Medikationsplan haben — für die
    Admin-Übersicht /medications."""
    return conn.execute(
        """
        SELECT p.id, p.name, p.geburtsdatum, p.stammnummer,
               COUNT(m.id) AS med_count,
               MAX(m.created_at) AS last_added
        FROM patients p
        JOIN medications m ON m.patient_id = p.id AND m.active = 1
        GROUP BY p.id
        ORDER BY p.name COLLATE NOCASE
        """,
    ).fetchall()
