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
    totp_required   INTEGER NOT NULL DEFAULT 0,
    perm_view_contact INTEGER NOT NULL DEFAULT 0,
    perm_export_pdf   INTEGER NOT NULL DEFAULT 0,
    perm_export_akte  INTEGER NOT NULL DEFAULT 0,
    perm_edit_patient INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    prefix       TEXT NOT NULL DEFAULT 'EH',
    start_date   TEXT,
    end_date     TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
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
    created_at            TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_protocols_patient ON protocols(patient_id);
CREATE INDEX IF NOT EXISTS idx_protocols_eh_datum ON protocols(eh_datum_uhrzeit);

CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id INTEGER NOT NULL REFERENCES protocols(id) ON DELETE CASCADE,
    author_id   INTEGER REFERENCES users(id),
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
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
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_central_patient ON central_protocols(patient_id);
CREATE INDEX IF NOT EXISTS idx_central_datum ON central_protocols(datum);

CREATE TABLE IF NOT EXISTS central_comments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    central_protocol_id INTEGER NOT NULL REFERENCES central_protocols(id) ON DELETE CASCADE,
    author_id           INTEGER REFERENCES users(id),
    text                TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
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
    changed_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_patient_changes_patient ON patient_changes(patient_id);

-- Audit-Log für Entschlüsselungs-Zugriffe auf Notfall-/Med-Daten
CREATE TABLE IF NOT EXISTS emergency_unlocks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id    INTEGER NOT NULL,
    requested_by  INTEGER REFERENCES users(id),
    approved_by   INTEGER REFERENCES users(id),
    unlocked_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
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
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
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
    arrival_at               TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
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
    created_at               TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
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
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_medications_patient ON medications(patient_id);

CREATE TABLE IF NOT EXISTS medication_administrations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    medication_id    INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
    day_date         TEXT NOT NULL,   -- YYYY-MM-DD
    slot             TEXT NOT NULL,   -- 'morgens' | 'mittags' | 'abends' | 'nachts' | 'bedarf'
    administered_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    administered_by  INTEGER REFERENCES users(id),
    notes            TEXT,
    UNIQUE(medication_id, day_date, slot)
);
CREATE INDEX IF NOT EXISTS idx_med_admin_med ON medication_administrations(medication_id);
CREATE INDEX IF NOT EXISTS idx_med_admin_day ON medication_administrations(day_date);

CREATE TABLE IF NOT EXISTS manv_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    card_prefix   TEXT NOT NULL,           -- e.g. "MANV-3"
    status        TEXT NOT NULL DEFAULT 'aktiv',  -- 'aktiv' | 'abgeschlossen'
    -- Alarmierung: Vorfall wird vorab angelegt, ist aber für User unsichtbar
    -- bis is_alarmiert=1. Beim Alarmieren sehen alle User das Event.
    is_alarmiert  INTEGER NOT NULL DEFAULT 0,
    alarm_at      TEXT,
    alarm_by      INTEGER REFERENCES users(id),
    -- Operative Felder (admin-editierbar, kann jederzeit ergänzt werden)
    situation     TEXT,        -- was ist passiert
    einsatzort    TEXT,        -- wo
    lage_bild     TEXT,        -- aktuelles Lagebild
    started_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    closed_at     TEXT,
    notes         TEXT,
    created_by    INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS manv_cards (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    manv_event_id     INTEGER REFERENCES manv_events(id) ON DELETE SET NULL,
        -- NULL = Pool-Karte (gedruckt + geklebt, aber noch nicht im Einsatz)
    card_no           TEXT NOT NULL UNIQUE,      -- "EH-000042"
    qr_token          TEXT NOT NULL UNIQUE,      -- random hex, in QR enkodiert
    status            TEXT NOT NULL DEFAULT 'blank',
        -- 'blank' | 'gesichtet' | 'in_behandlung' | 'transportiert' | 'abgeschlossen'
    patient_id        INTEGER REFERENCES patients(id) ON DELETE SET NULL,
    central_protocol_id INTEGER REFERENCES central_protocols(id) ON DELETE SET NULL,
    -- Personalia (von der Karte abgetippt oder beim Scan eingegeben)
    name              TEXT,
    vorname           TEXT,
    geburtsdatum      TEXT,
    alter_jahre       INTEGER,
    geschlecht        TEXT,
    nationalitaet     TEXT,
    -- Sichtung
    sichtung_kategorie TEXT,            -- 'I' | 'II' | 'III' | 'IV' | 'tot'
    sichtungen_json   TEXT,             -- [{time, name, kategorie}, ...]
    -- Kurzdiagnose-Flags
    diag_verletzung   INTEGER NOT NULL DEFAULT 0,
    diag_verbrennung  INTEGER NOT NULL DEFAULT 0,
    diag_erkrankung   INTEGER NOT NULL DEFAULT 0,
    diag_vergiftung   INTEGER NOT NULL DEFAULT 0,
    diag_verstrahlung INTEGER NOT NULL DEFAULT 0,
    diag_psyche       INTEGER NOT NULL DEFAULT 0,
    diag_lokalisation TEXT,
    -- Zustand
    bewusstsein       TEXT,             -- 'oB' | 'reduziert'
    atmung            TEXT,
    kreislauf         TEXT,
    zustand_zeit      TEXT,
    -- Erst-Therapie
    th_infusion       INTEGER NOT NULL DEFAULT 0,
    th_analgetika     INTEGER NOT NULL DEFAULT 0,
    th_antidote       INTEGER NOT NULL DEFAULT 0,
    th_sonstige       INTEGER NOT NULL DEFAULT 0,
    th_sonstige_text  TEXT,
    -- Transport
    transport_mittel  TEXT,
    transport_ziel    TEXT,
    transport_art     TEXT,             -- 'liegend' | 'sitzend'
    transport_mit_arzt INTEGER NOT NULL DEFAULT 0,
    transport_isoliert INTEGER NOT NULL DEFAULT 0,
    transport_prio    TEXT,             -- 'a' | 'b'
    -- Notes
    bemerkungen       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_manv_cards_event ON manv_cards(manv_event_id);
CREATE INDEX IF NOT EXISTS idx_manv_cards_status ON manv_cards(status);
CREATE INDEX IF NOT EXISTS idx_manv_cards_kategorie ON manv_cards(sichtung_kategorie);

CREATE TABLE IF NOT EXISTS einsatzbefehle (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          INTEGER REFERENCES events(id) ON DELETE SET NULL,
    eindeutige_id     TEXT NOT NULL UNIQUE,    -- z.B. EB-20260531-A4F1B2C8
    befehlende_stelle TEXT,
    takt_zeit         TEXT,                    -- Datum/Uhrzeit Freitext
    befehl_fuer       TEXT,
    lage              TEXT,
    auftrag           TEXT,
    auftragsort       TEXT,
    ansprechpartner   TEXT,
    kontaktnummer     TEXT,
    durchfuehrung     TEXT,
    versorgung        TEXT,
    verbindung        TEXT,
    rueck_bezeichnung TEXT,
    rueck_rufname     TEXT,
    rueck_funkgruppe  TEXT,
    rueck_telefon     TEXT,
    erstellt_von_text TEXT,                    -- freie Text-Eingabe ("Name, Funktion")
    created_by        INTEGER REFERENCES users(id),
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_einsatzbefehle_event ON einsatzbefehle(event_id);

CREATE TABLE IF NOT EXISTS einsatztagebuch (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    einsatzbefehl_id    INTEGER NOT NULL REFERENCES einsatzbefehle(id) ON DELETE CASCADE,
    einrichtung_einheit TEXT,
    einsatz_anlass      TEXT,
    blatt_nr            INTEGER NOT NULL DEFAULT 1,
    blatt_von           INTEGER NOT NULL DEFAULT 1,
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_einsatztagebuch_befehl ON einsatztagebuch(einsatzbefehl_id);

CREATE TABLE IF NOT EXISTS einsatztagebuch_eintraege (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tagebuch_id     INTEGER NOT NULL REFERENCES einsatztagebuch(id) ON DELETE CASCADE,
    lfd_nr          INTEGER NOT NULL,
    ea              TEXT,                       -- E (Eingang) | A (Ausgang)
    taktische_zeit  TEXT,
    darstellung     TEXT NOT NULL,
    vollzug         TEXT,
    anlage          TEXT,
    created_by      INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_eintraege_tagebuch ON einsatztagebuch_eintraege(tagebuch_id);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Foto-/Datei-Anhänge an zentralen Protokollen (BLOB in SQLite —
-- bleibt im Single-File-Backup enthalten)
CREATE TABLE IF NOT EXISTS central_attachments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    central_protocol_id INTEGER NOT NULL REFERENCES central_protocols(id)
                        ON DELETE CASCADE,
    filename            TEXT NOT NULL,
    mime                TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL,
    content             BLOB NOT NULL,
    uploaded_by         INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_central_attachments_protocol
    ON central_attachments(central_protocol_id);

-- Übertragungen von Protokoll-PDFs an frodor (Camp-Anmeldungs-Plattform).
-- Zwei-Schritt-Upload: erst Datei-Eintrag in frodor ('row_created'), dann
-- PDF in den Storage ('uploaded'). Bleibt ein Upload hängen (Camp-WLAN),
-- kann er über den gespeicherten path idempotent wiederholt werden.
CREATE TABLE IF NOT EXISTS frodor_uploads (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type       TEXT NOT NULL,       -- 'decentral' | 'central'
    source_id         INTEGER NOT NULL,    -- protocols.id oder central_protocols.id
    registration_uuid TEXT NOT NULL,
    path              TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending|row_created|uploaded|failed
    error             TEXT,
    uploaded_by       INTEGER REFERENCES users(id),
    created_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(source_type, source_id)
);
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
                "INTEGER NOT NULL DEFAULT 0"
            )
        # Einmalige Umstellung: 2FA ist ab jetzt opt-in (Admin schaltet
        # pro User frei). Bestehende User, die 2FA noch NICHT
        # eingerichtet haben, werden auf totp_required=0 gestellt —
        # wer 2FA bereits aktiv nutzt (totp_confirmed=1), behält es.
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'totp_optin_migrated'"
        ).fetchone()
        if not row:
            conn.execute(
                "UPDATE users SET totp_required = 0 "
                "WHERE totp_confirmed = 0"
            )
            conn.execute(
                "INSERT INTO app_settings (key, value) "
                "VALUES ('totp_optin_migrated', '1')"
            )
        if "admin_pin_hash" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN admin_pin_hash TEXT")
        if "login_pin_hash" not in cols:
            # Anmelde-PIN als Alternative zum Passwort (z. B. Tablet im Feld)
            conn.execute("ALTER TABLE users ADD COLUMN login_pin_hash TEXT")
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
            # Verknüpfung zur frodor-Anmeldung (registrations.uuid)
            ("frodor_registration_uuid", "TEXT"),
        ]:
            if col not in pat_cols:
                conn.execute(f"ALTER TABLE patients ADD COLUMN {col} {decl}")

        # Migration: Verknüpfung zu frodor registration_medications.id —
        # Upsert-Anker für den Abgleich der strukturierten Medikamente.
        med_cols = {row[1] for row in conn.execute("PRAGMA table_info(medications)")}
        if "frodor_medication_id" not in med_cols:
            conn.execute(
                "ALTER TABLE medications ADD COLUMN frodor_medication_id INTEGER")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_medications_frodor_id "
            "ON medications(frodor_medication_id) "
            "WHERE frodor_medication_id IS NOT NULL")

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

        # Migration: manv_events um Alarmierungs- + Lage-Felder erweitern
        try:
            me_cols = {row[1] for row in
                       conn.execute("PRAGMA table_info(manv_events)")}
        except Exception:
            me_cols = set()
        added_is_alarmiert = False
        for col, decl in [
            ("is_alarmiert", "INTEGER NOT NULL DEFAULT 0"),
            ("alarm_at", "TEXT"),
            ("alarm_by", "INTEGER"),
            ("situation", "TEXT"),
            ("einsatzort", "TEXT"),
            ("lage_bild", "TEXT"),
        ]:
            if me_cols and col not in me_cols:
                conn.execute(
                    f"ALTER TABLE manv_events ADD COLUMN {col} {decl}")
                if col == "is_alarmiert":
                    added_is_alarmiert = True
        # Beim ersten Migrationslauf: bestehende aktive Events auch als
        # alarmiert markieren — sonst würden sie für User schlagartig
        # verschwinden.
        if added_is_alarmiert:
            conn.execute(
                "UPDATE manv_events SET is_alarmiert=1, "
                "alarm_at=COALESCE(alarm_at, started_at) "
                "WHERE status='aktiv'")

        # Migration: manv_cards.manv_event_id soll nullable sein
        # (Pool-Karten = Sticker auf der DRK-Karte, aber noch nicht im Einsatz).
        # SQLite kann NOT NULL nicht direkt entfernen — wir bauen die Tabelle
        # neu wenn die Spalte noch NOT NULL ist.
        try:
            mcols = conn.execute("PRAGMA table_info(manv_cards)").fetchall()
        except Exception:
            mcols = []
        ev_col = next((r for r in mcols if r[1] == "manv_event_id"), None)
        if ev_col is not None and ev_col[3] == 1:  # row[3] = notnull
            conn.execute("ALTER TABLE manv_cards RENAME TO manv_cards_old_notnull")
            # SCHEMA-Block wurde oben schon ausgeführt; CREATE TABLE IF NOT
            # EXISTS hat aber wegen umbenennung nichts angelegt — neu erstellen
            conn.executescript("""
                CREATE TABLE manv_cards (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    manv_event_id     INTEGER REFERENCES manv_events(id)
                                       ON DELETE SET NULL,
                    card_no           TEXT NOT NULL UNIQUE,
                    qr_token          TEXT NOT NULL UNIQUE,
                    status            TEXT NOT NULL DEFAULT 'blank',
                    patient_id        INTEGER REFERENCES patients(id) ON DELETE SET NULL,
                    central_protocol_id INTEGER REFERENCES central_protocols(id) ON DELETE SET NULL,
                    name              TEXT, vorname TEXT, geburtsdatum TEXT,
                    alter_jahre       INTEGER, geschlecht TEXT, nationalitaet TEXT,
                    sichtung_kategorie TEXT, sichtungen_json TEXT,
                    diag_verletzung INTEGER NOT NULL DEFAULT 0,
                    diag_verbrennung INTEGER NOT NULL DEFAULT 0,
                    diag_erkrankung INTEGER NOT NULL DEFAULT 0,
                    diag_vergiftung INTEGER NOT NULL DEFAULT 0,
                    diag_verstrahlung INTEGER NOT NULL DEFAULT 0,
                    diag_psyche INTEGER NOT NULL DEFAULT 0,
                    diag_lokalisation TEXT,
                    bewusstsein TEXT, atmung TEXT, kreislauf TEXT, zustand_zeit TEXT,
                    th_infusion INTEGER NOT NULL DEFAULT 0,
                    th_analgetika INTEGER NOT NULL DEFAULT 0,
                    th_antidote INTEGER NOT NULL DEFAULT 0,
                    th_sonstige INTEGER NOT NULL DEFAULT 0,
                    th_sonstige_text TEXT,
                    transport_mittel TEXT, transport_ziel TEXT, transport_art TEXT,
                    transport_mit_arzt INTEGER NOT NULL DEFAULT 0,
                    transport_isoliert INTEGER NOT NULL DEFAULT 0,
                    transport_prio TEXT, bemerkungen TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                );
            """)
            # Daten kopieren (alte Spaltenliste aus Migration)
            old_cols = [r[1] for r in mcols]
            col_list = ", ".join(old_cols)
            conn.execute(
                f"INSERT INTO manv_cards ({col_list}) "
                f"SELECT {col_list} FROM manv_cards_old_notnull"
            )
            conn.execute("DROP TABLE manv_cards_old_notnull")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_manv_cards_event "
                "ON manv_cards(manv_event_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_manv_cards_status "
                "ON manv_cards(status)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_manv_cards_kategorie "
                "ON manv_cards(sichtung_kategorie)")

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


def verify_login_credential(user_row: sqlite3.Row, credential: str) -> bool:
    """Login-Prüfung: Passwort ODER (falls gesetzt) Anmelde-PIN.
    Beides läuft über dasselbe Eingabefeld auf der Login-Seite."""
    if not credential:
        return False
    if check_password_hash(user_row["password_hash"], credential):
        return True
    try:
        pin_hash = user_row["login_pin_hash"]
    except (IndexError, KeyError):
        pin_hash = None
    if pin_hash and check_password_hash(pin_hash, credential):
        return True
    return False


def set_login_pin(conn: sqlite3.Connection, user_id: int,
                  pin: Optional[str]) -> None:
    """Anmelde-PIN setzen (gehasht) oder mit None entfernen."""
    if pin is None:
        conn.execute("UPDATE users SET login_pin_hash = NULL WHERE id = ?",
                     (user_id,))
    else:
        conn.execute(
            "UPDATE users SET login_pin_hash = ? WHERE id = ?",
            (generate_password_hash(pin), user_id),
        )


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
        f"       (login_pin_hash IS NOT NULL) AS has_login_pin, "
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


# ---------------------------------------------------------------------------
# frodor-Anbindung (Camp-Anmeldungen)
# ---------------------------------------------------------------------------

def adopt_frodor_registration(conn: sqlite3.Connection, reg: dict) -> int:
    """Lokalen Patienten aus einer frodor-Registrierung anlegen/verknüpfen.

    Upsert über (name, geburtsdatum) wie überall sonst. Die frodor-UUID wird
    immer gesetzt; Stammnummer/Notfallkontakt/Allergien/Medikamente werden
    nur übernommen, wenn lokal noch leer (lokale Eingaben gewinnen).
    """
    patient_id = upsert_patient(conn, reg["name"], reg["geburtsdatum"],
                                reg.get("stamm") or None)
    row = get_patient(conn, patient_id)

    updates: dict = {"frodor_registration_uuid": reg["uuid"]}

    contact = (reg.get("contacts") or [{}])[0]
    if not row["emergency_contact_name"] and contact.get("name"):
        updates["emergency_contact_name"] = contact["name"]
        phone = contact.get("mobile") or contact.get("phone") or None
        if not row["emergency_contact_phone"] and phone:
            updates["emergency_contact_phone"] = phone
        if not row["emergency_contact_relation"] and contact.get("relation"):
            updates["emergency_contact_relation"] = contact["relation"]

    if row["has_allergies"] is None:
        allergies = (reg.get("allergies") or "").strip()
        updates["has_allergies"] = 1 if allergies else 0
        if allergies and not row["allergies_text"]:
            updates["allergies_text"] = allergies

    if row["has_medications"] is None and reg.get("has_medications") is not None:
        updates["has_medications"] = 1 if reg["has_medications"] else 0
        medications = (reg.get("medications") or "").strip()
        if medications and not row["medications_text"]:
            updates["medications_text"] = medications

    # Einschränkungen + interne EH-Notizen aus der Anmeldung → Sonstige
    # Hinweise (nur wenn lokal noch leer; lokale Eingaben gewinnen).
    if not row["extras_notes"]:
        note_parts = []
        if (reg.get("restrictions") or "").strip():
            note_parts.append(
                f"Einschränkungen (Anmeldung): {reg['restrictions'].strip()}")
        if (reg.get("note") or "").strip():
            note_parts.append(
                f"EH-Notiz (Anmeldung): {reg['note'].strip()}")
        if note_parts:
            updates["extras_notes"] = "\n".join(note_parts)

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    conn.execute(f"UPDATE patients SET {set_clause} WHERE id = ?",
                 [*updates.values(), patient_id])
    return patient_id


def get_frodor_upload(conn: sqlite3.Connection, source_type: str,
                      source_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM frodor_uploads WHERE source_type = ? AND source_id = ?",
        (source_type, source_id),
    ).fetchone()


def start_frodor_upload(conn: sqlite3.Connection, source_type: str,
                        source_id: int, registration_uuid: str,
                        uploaded_by: Optional[int]) -> sqlite3.Row:
    """Upload-Zeile anlegen bzw. für einen erneuten Versuch übernehmen.

    Ein bereits erfolgreicher Upload ('uploaded') wird nicht neu gestartet —
    der Aufrufer entscheidet, ob er mit neuem Pfad erneut senden will.
    """
    conn.execute(
        """
        INSERT INTO frodor_uploads (source_type, source_id, registration_uuid, uploaded_by)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            registration_uuid = excluded.registration_uuid,
            uploaded_by = excluded.uploaded_by,
            updated_at = datetime('now', 'localtime')
        """,
        (source_type, source_id, registration_uuid, uploaded_by),
    )
    return get_frodor_upload(conn, source_type, source_id)


def set_frodor_upload_state(conn: sqlite3.Connection, upload_id: int,
                            status: str, *, path: Optional[str] = None,
                            error: Optional[str] = None) -> None:
    conn.execute(
        """
        UPDATE frodor_uploads
        SET status = ?, path = COALESCE(?, path), error = ?,
            updated_at = datetime('now', 'localtime')
        WHERE id = ?
        """,
        (status, path, error, upload_id),
    )


def list_frodor_uploads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Alle Übertragungen, neueste zuerst — für die Status-Seite."""
    return conn.execute(
        """
        SELECT fu.*, p.name AS patient_name
        FROM frodor_uploads fu
        LEFT JOIN patients p ON p.frodor_registration_uuid = fu.registration_uuid
        ORDER BY fu.updated_at DESC
        """,
    ).fetchall()


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


def peek_next_laufende_nr(conn: sqlite3.Connection,
                            event_id: Optional[int] = None) -> str:
    """Liefert die NÄCHSTE laufende Nr, ohne sie zu reservieren.
    Wird beim Anlegen-Form vorab angezeigt. Bei einem Concurrency-
    Konflikt kann die echte Nummer beim Speichern ±1 abweichen."""
    event_id = event_id or get_default_event_id(conn)
    row = conn.execute(
        "SELECT COALESCE(MAX(seq_no), 0) + 1 AS next_no "
        "FROM protocol_sequence WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    seq_no = row["next_no"] if row else 1
    return f"#{get_event_prefix(conn, event_id)}{seq_no}"


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
    """Format an ISO datetime/date string for display.

    Wendet TZ-Konversion an wenn die App-Anzeige-Zeitzone (in
    flask.g.display_tz oder app_settings) abweicht von der
    Container-Zeitzone (env TZ). DB-Datetimes sind als naive in
    Container-TZ gespeichert (siehe datetime('now', 'localtime')).
    """
    if not value:
        return ""
    import os
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        # TZ-aware machen wenn möglich
        try:
            from zoneinfo import ZoneInfo
            container_tz_name = os.environ.get("TZ") or "UTC"
            target_tz_name = container_tz_name
            try:
                from flask import g, has_request_context
                if has_request_context():
                    target_tz_name = getattr(g, "display_tz",
                                              container_tz_name) or container_tz_name
            except Exception:
                pass
            if fmt != "%Y-%m-%d" and target_tz_name != container_tz_name:
                container_tz = ZoneInfo(container_tz_name)
                target_tz = ZoneInfo(target_tz_name)
                dt = dt.replace(tzinfo=container_tz).astimezone(target_tz)
        except Exception:
            pass
        return dt.strftime(
            "%d.%m.%Y" if fmt == "%Y-%m-%d" else "%d.%m.%Y %H:%M"
        )
    return value


# ---------- App-Settings (Key-Value-Store für TZ etc.) ----------

def get_app_setting(conn, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_app_setting(conn, key: str, value: Optional[str]) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# Verfügbare Zeitzonen — Auswahl für das Admin-UI
APP_TIMEZONES = (
    "Europe/Berlin", "Europe/Vienna", "Europe/Zurich", "Europe/Amsterdam",
    "Europe/Paris", "Europe/London", "Europe/Madrid", "Europe/Warsaw",
    "Europe/Prague", "Europe/Stockholm", "Europe/Helsinki",
    "Europe/Athens", "Europe/Istanbul", "UTC",
)


def get_server_time_info(conn) -> dict:
    """Liefert aktuelle Server-Zeit + konfigurierte App-TZ für die
    Anzeige in der Admin-UI."""
    import os
    from datetime import datetime as _dt
    container_tz = os.environ.get("TZ", "(nicht gesetzt)")
    app_tz = get_app_setting(conn, "app_timezone", container_tz)
    now_local = _dt.now()
    return {
        "container_tz": container_tz,
        "app_tz": app_tz,
        "now": now_local.strftime("%d.%m.%Y %H:%M:%S"),
        "available": list(APP_TIMEZONES),
    }


# ---------- Regionen (editierbare Zuordnung Stamm → Region) ----------
#
# "Region" ist kein eigenes Datenfeld am Patienten, sondern eine
# jederzeit änderbare Zuordnung Stamm(bezeichnung) → Region. Sie liegt
# als JSON im app_settings-Store und wird über die Admin-Oberfläche
# gepflegt. Solange nichts gespeichert wurde, gilt die Vorbelegung unten.

REGION_DEFAULT_ORDER = ["O1", "O2", "O3", "O4", "O5", "O6"]
REGION_UNASSIGNED = "Ohne Region"

# Erst-Vorbelegung Stamm → Region (bekannte Aufstellung O1–O6).
REGION_MAP_DEFAULT = {
    # O1
    "100 Berlin 1": "O1", "211 Cottbus": "O1", "260 Berlin 6": "O1",
    "355 Potsdam": "O1", "461 Berlin 9": "O1", "565 Berlin 13": "O1",
    "590 Berlin 14": "O1", "592 Berlin 15": "O1", "594 Brandenburg": "O1",
    "619 Hennigsdorf": "O1", "622 Berlin 16": "O1", "Delegation": "O1",
    # O2
    "138 Chemnitz": "O2", "219 Zwickau": "O2", "244 Leipzig 2": "O2",
    "266 Grimma": "O2", "359 Leipzig 3": "O2", "390 Lichtenstein": "O2",
    "390 Lichtenstein-Neuplanitz": "O2", "420 Rodewisch": "O2",
    "472 Plauen": "O2", "535 Freiberg": "O2", "543 Chemnitz 2": "O2",
    "606 Reichenbach": "O2",
    # O3
    "117 Heiligenstadt": "O3", "264 Eisenach": "O3", "344 Schmalkalden": "O3",
    "366 Gotha": "O3", "427 Ellrich-Sülzhayn": "O3", "458 Tanna": "O3",
    "497 Ilmenau": "O3", "603 Jena": "O3", "609 Eisenberg": "O3",
    "642 Erfurt 2": "O3",
    # O4
    "375 Rostock": "O4", "539 Barth": "O4", "546 Neustrelitz": "O4",
    "556 Rostock 2": "O4", "561 Stralsund 2": "O4", "567 Greifswald": "O4",
    "591 Schwerin 2": "O4", "591 Schwerin 2-Ludwigslust": "O4",
    "591 Schwerin 2-Neu Kaliß": "O4", "608 Wismar": "O4",
    # O5
    "220 Wittenberg": "O5", "225 Magdeburg": "O5", "371 Halle": "O5",
    "530 Naumburg": "O5", "566 Magdeburg 2": "O5", "573 Zeitz": "O5",
    "605 Wernigerode": "O5",
    # O6
    "240 Dresden 1": "O6", "240 Dresden 1-Gorbitz": "O6",
    "277 Großenhain": "O6", "280 Dresden 2": "O6", "351 Dohna": "O6",
    "483 Herrnhut": "O6", "620 Dippoldiswalde": "O6", "637 Meißen": "O6",
    "640 Pulsnitz": "O6",
}


def get_regions(conn) -> list:
    """Geordnete Liste der Regionsnamen (editierbar). Fällt auf die
    Standard-Reihenfolge zurück, solange nichts gespeichert wurde."""
    import json
    raw = get_app_setting(conn, "regions")
    if raw:
        try:
            val = json.loads(raw)
            if isinstance(val, list):
                clean = [str(x).strip() for x in val if str(x).strip()]
                if clean:
                    return clean
        except (ValueError, TypeError):
            pass
    return list(REGION_DEFAULT_ORDER)


def set_regions(conn, regions) -> None:
    import json
    clean = [str(r).strip() for r in regions if str(r).strip()]
    set_app_setting(conn, "regions", json.dumps(clean, ensure_ascii=False))


def get_region_map(conn) -> dict:
    """Zuordnung Stamm(bezeichnung) → Region. Nur eine NICHT-leere
    gespeicherte Zuordnung überschreibt die Vorbelegung — eine leere
    (bzw. fehlende/ungültige) fällt auf REGION_MAP_DEFAULT zurück."""
    import json
    raw = get_app_setting(conn, "region_map")
    if raw:
        try:
            val = json.loads(raw)
            if isinstance(val, dict):
                clean = {str(k).strip(): str(v).strip()
                         for k, v in val.items()
                         if str(k).strip() and str(v).strip()}
                if clean:
                    return clean
        except (ValueError, TypeError):
            pass
    return dict(REGION_MAP_DEFAULT)


def _normalize_stamm_key(s) -> str:
    """Kanonische Form eines Stamm-Strings für den Vergleich —
    unabhängig von Groß/Klein, Leerzeichen vs. Bindestrichen und
    Umlaut-Schreibweise. '100 Berlin 1' und '100-berlin-1' → gleich."""
    import re
    s = str(s or "").strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def set_region_map(conn, mapping) -> None:
    import json
    clean = {str(k).strip(): str(v).strip()
             for k, v in mapping.items()
             if str(k).strip() and str(v).strip()}
    set_app_setting(conn, "region_map", json.dumps(clean, ensure_ascii=False))


def region_for_stamm(region_map: dict, stamm) -> Optional[str]:
    """Region zu einem Stamm-String; None, wenn nicht zugeordnet.
    Tolerant gegenüber Schreibweise (Leerzeichen/Bindestriche/Umlaute)."""
    if not stamm:
        return None
    s = stamm.strip()
    if s in region_map:
        return region_map[s]
    key = _normalize_stamm_key(s)
    if not key:
        return None
    for k, v in region_map.items():
        if _normalize_stamm_key(k) == key:
            return v
    return None


def region_admin_data(conn) -> dict:
    """Daten für die Admin-Oberfläche: Regionsliste, aktuelle Zuordnung
    und alle bekannten Stämme (aus den Patientendaten + aus der
    gespeicherten/vorbelegten Zuordnung), damit jeder Stamm einer Region
    zugewiesen werden kann."""
    regions = get_regions(conn)
    region_map = get_region_map(conn)
    known = set(region_map.keys())
    rows = conn.execute(
        "SELECT DISTINCT TRIM(stammnummer) AS s FROM patients "
        "WHERE TRIM(COALESCE(stammnummer, '')) != ''"
    ).fetchall()
    for r in rows:
        if r["s"]:
            known.add(r["s"])

    def _key(name: str):
        head = name.split(None, 1)[0]
        try:
            return (0, int(head), name.lower())
        except ValueError:
            return (1, 0, name.lower())

    staemme = sorted(known, key=_key)
    # Aktuell aufgelöste Region je Stamm (schreibweise-tolerant) — damit die
    # Dropdowns korrekt vorausgewählt sind, auch wenn die gespeicherten
    # Schlüssel anders geschrieben sind als die tatsächlichen Stammnamen.
    resolved = {s: (region_for_stamm(region_map, s) or "") for s in staemme}
    return {
        "regions": regions,
        "region_map": region_map,
        "resolved": resolved,
        "staemme": staemme,
    }


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


def _signature_audit_value(data: dict, n: int) -> Optional[str]:
    """Beschreibung für den Audit-Log-Eintrag einer EK-Unterschrift:
    Name der Einsatzkraft (aus einsatzkraft1/2) + erfassender User."""
    ek_name = _scalar(data.get(f"einsatzkraft{n}")) or f"Einsatzkraft {n}"
    by = _scalar(data.get(f"signature_einsatzkraft{n}_by"))
    at = _scalar(data.get(f"signature_einsatzkraft{n}_at"))
    parts = [str(ek_name)]
    if by:
        parts.append(f"erfasst von {by}")
    if at:
        parts.append(f"am {at}")
    return " · ".join(parts)


def _log_signature_changes(conn: sqlite3.Connection,
                           patient_id: Optional[int],
                           old_data: Optional[dict],
                           new_data: dict,
                           changed_by: Optional[int]) -> None:
    """Schreibt patient_changes-Einträge für hinzugefügte/geänderte
    EK-Unterschriften. Wird nur ausgeführt wenn das Protokoll an einen
    Patienten gebunden ist (sonst kein Audit-Ziel)."""
    if patient_id is None:
        return
    for n in (1, 2):
        field = f"signature_einsatzkraft{n}"
        old_sig = (old_data or {}).get(field) if old_data else None
        new_sig = new_data.get(field)
        old_present = bool(_scalar(old_sig))
        new_present = bool(_scalar(new_sig))
        # Nur loggen, wenn sich etwas ändert: hinzugefügt, geändert oder entfernt
        if old_present == new_present and old_sig == new_sig:
            continue
        if new_present:
            new_val = _signature_audit_value(new_data, n)
        else:
            new_val = None
        if old_present:
            old_val = _signature_audit_value(old_data or {}, n)
        else:
            old_val = None
        conn.execute(
            "INSERT INTO patient_changes "
            "(patient_id, changed_by, field_name, old_value, new_value) "
            "VALUES (?, ?, ?, ?, ?)",
            (patient_id, changed_by, field, old_val, new_val),
        )


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
    # Audit-Log: ggf. mitgegebene Unterschriften gleich verbuchen
    _log_signature_changes(conn, patient_id, None, data, created_by)
    return new_id


def update_central_protocol(conn: sqlite3.Connection, pid: int,
                            data: dict,
                            changed_by: Optional[int] = None) -> bool:
    # Vorzustand für Audit holen, bevor wir überschreiben
    old_row = conn.execute(
        "SELECT data FROM central_protocols WHERE id = ?", (pid,)
    ).fetchone()
    old_data = {}
    if old_row and old_row["data"]:
        try:
            old_data = _json.loads(old_row["data"]) or {}
        except Exception:
            old_data = {}
    patient_id = _link_central_to_patient(conn, data)
    cur = conn.execute(
        """
        UPDATE central_protocols
           SET patient_id = ?, einsatznummer = ?, datum = ?,
               name_summary = ?, data = ?, updated_at = datetime('now', 'localtime')
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
    if cur.rowcount > 0:
        _log_signature_changes(conn, patient_id, old_data, data, changed_by)
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
    "signature_einsatzkraft1": "Unterschrift Einsatzkraft 1",
    "signature_einsatzkraft2": "Unterschrift Einsatzkraft 2",
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


# ==================== mSTaRT-Algorithmus ====================
# Fragen in fester Reihenfolge; erstes „ja" endet die Prüfung und
# vergibt die Kategorie. Reihenfolge = Priorität.

MSTART_QUESTIONS = [
    # key,        Frage,                                              yes_cat, hint
    ("gehfaehig",   "Patient gehfähig?",                              "SK3",   None),
    ("toedlich",    "Tödliche Verletzung? Atemstillstand auch nach "
                    "Freimachen der Atemwege?",                       "TOT",   None),
    ("atmung",      "Atemfrequenz >30 oder <10/min · Atmung nur mit "
                    "Guedeltubus? (Untersuchungsdauer 10 s)",         "SK1",   None),
    ("blutung",     "Unstillbare, spritzende Blutung?",               "SK1",
                    "Blutstillung versuchen: Tourniquet / Druckverband anlegen."),
    ("radialis",    "Fehlender Radialispuls? (Untersuchungsdauer 10 s)",
                    "SK1",                                            None),
    ("befehle",     "Folgt einfachen Befehlen NICHT?",                "SK1",   None),
]

MSTART_CATEGORY_LABEL = {
    "SK1": "SK I · rot — sofort",
    "SK2": "SK II · gelb — dringend",
    "SK3": "SK III · grün — kann warten",
    "TOT": "SK IV · schwarz — verstorben",
}

VALID_TRIAGE_CATEGORIES = ("SK1", "SK2", "SK3", "TOT")


def classify_mstart(answers: dict) -> tuple[str, Optional[str]]:
    """Wendet den mSTaRT-Baum an. `answers` ist ein Dict wie
    {"gehfaehig": "ja", "toedlich": "nein", ...}. Gibt (kategorie,
    trigger_key) zurück — trigger_key ist der Fragen-Schlüssel, der
    zuerst mit „ja" beantwortet wurde (None wenn keine)."""
    for key, _q, yes_cat, _hint in MSTART_QUESTIONS:
        if (answers or {}).get(key) == "ja":
            return yes_cat, key
    # Keine Frage mit „ja" beantwortet → gelb / SK II
    return "SK2", None


def create_triage_entry(conn, *, name=None, geburtsdatum=None,
                         indicators=None, notes=None,
                         category=None,
                         created_by=None, event_id=None) -> int:
    """Legt Triage-Eintrag an. Kategorie kommt aus dem mSTaRT-Baum
    (Aufrufer übergibt sie explizit); ohne gültige Kategorie wird SK3
    angenommen. Die Zusatzindikatoren sind reine Dokumentation und
    beeinflussen die Kategorie NICHT. Bei TOT wird der Eintrag direkt
    auf status='abgeschlossen' gesetzt, damit Verstorbene nicht in der
    Warteliste erscheinen."""
    indicators = indicators or []
    if category not in VALID_TRIAGE_CATEGORIES:
        category = "SK3"
    # Patienten matchen, falls Name + Geburtsdatum ausreichend sind
    patient_id = None
    if name and geburtsdatum:
        patient_id = upsert_patient(conn, name, geburtsdatum, None)
    status = "abgeschlossen" if category == "TOT" else "wartend"
    cur = conn.execute(
        """
        INSERT INTO triage_entries
          (event_id, patient_id, name, geburtsdatum, category, indicators, notes,
           status, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id or get_default_event_id(conn), patient_id, (name or "").strip() or None,
         (geburtsdatum or "").strip() or None,
         category, _json.dumps(indicators), (notes or "").strip() or None,
         status, created_by),
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
            treatment_finished_at = datetime('now', 'localtime')
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
                                             datetime('now', 'localtime')),
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
                                             datetime('now', 'localtime')),
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


# ---------- Anhänge an zentralen Protokollen ----------

ATTACHMENT_MAX_BYTES = 15 * 1024 * 1024   # 15 MB pro Datei
ATTACHMENT_MAX_COUNT = 20                 # pro Protokoll


def add_central_attachment(conn, protocol_id: int, *, filename: str,
                            mime: str, content: bytes,
                            uploaded_by: Optional[int]) -> int:
    cur = conn.execute(
        """
        INSERT INTO central_attachments
          (central_protocol_id, filename, mime, size_bytes, content,
           uploaded_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (protocol_id, filename[:200], mime[:100], len(content),
         content, uploaded_by),
    )
    return cur.lastrowid


def list_central_attachments(conn, protocol_id: int) -> list[sqlite3.Row]:
    """Metadaten (ohne BLOB) — für Listen-Anzeigen."""
    return conn.execute(
        """
        SELECT a.id, a.filename, a.mime, a.size_bytes, a.created_at,
               u.full_name AS uploaded_by_name, u.username AS uploaded_by_username
        FROM central_attachments a
        LEFT JOIN users u ON u.id = a.uploaded_by
        WHERE a.central_protocol_id = ?
        ORDER BY a.id
        """,
        (protocol_id,),
    ).fetchall()


def get_central_attachment(conn, attachment_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM central_attachments WHERE id = ?", (attachment_id,)
    ).fetchone()


def delete_central_attachment(conn, attachment_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM central_attachments WHERE id = ?", (attachment_id,))
    return cur.rowcount > 0


def count_central_attachments(conn, protocol_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM central_attachments "
        "WHERE central_protocol_id = ?", (protocol_id,)
    ).fetchone()["n"]


def list_medication_stamm_values(conn) -> list[str]:
    """Alle Stämme/Regionen, für die es Medikamente gibt — aus aktiven
    Plänen ODER aus Anmeldungs-Medikamenten (medications_text). Für das
    Export-Dropdown. Leerer Stamm wird als '' geführt."""
    rows = conn.execute(
        """
        SELECT DISTINCT COALESCE(TRIM(p.stammnummer), '') AS stamm
        FROM patients p
        JOIN medications m ON m.patient_id = p.id AND m.active = 1
        UNION
        SELECT DISTINCT COALESCE(TRIM(stammnummer), '') AS stamm
        FROM patients
        WHERE TRIM(COALESCE(medications_text, '')) != ''
        ORDER BY stamm COLLATE NOCASE
        """
    ).fetchall()
    return [r["stamm"] for r in rows]


def sync_frodor_medications(conn, regs: list[dict]) -> dict:
    """Bulk-Import/-Abgleich der Medikamente aus frodor-Anmeldungen.

    Übernimmt alle Registrierungen, die eine Medikamenten-Angabe haben
    (hasMedications oder Freitext). Neue Personen werden angelegt
    (adopt inkl. Notfallkontakt etc.); bei bestehenden gilt für die
    ANMELDUNGS-Medikamente frodor als Quelle der Wahrheit — der
    Freitext wird bei Änderung aktualisiert. Lokal gepflegte
    strukturierte Medikationspläne (medications-Tabelle) bleiben
    unangetastet. Gibt Zähler zurück."""
    created = 0
    updated = 0
    unchanged = 0
    skipped = 0
    for reg in regs:
        med_text = (reg.get("medications") or "").strip()
        has_meds = reg.get("has_medications")
        if not med_text and not has_meds:
            skipped += 1
            continue
        if not reg.get("name") or not reg.get("geburtsdatum"):
            skipped += 1
            continue
        existing = conn.execute(
            "SELECT id FROM patients WHERE name = ? AND geburtsdatum = ?",
            (reg["name"], reg["geburtsdatum"]),
        ).fetchone()
        pid = adopt_frodor_registration(conn, reg)
        if not existing:
            created += 1
            continue
        # Bestehender Patient: Anmeldungs-Medikamente live nachziehen
        row = get_patient(conn, pid)
        if med_text and (row["medications_text"] or "").strip() != med_text:
            conn.execute(
                "UPDATE patients SET medications_text = ?, "
                "has_medications = 1 WHERE id = ?",
                (med_text, pid),
            )
            updated += 1
        else:
            unchanged += 1
    return {"created": created, "updated": updated,
            "unchanged": unchanged, "skipped": skipped}


def sync_frodor_structured_medications(conn, meds: list[dict],
                                       regs: list[dict]) -> dict:
    """Abgleich der strukturierten frodor-Medikamente
    (registration_medications) mit der lokalen medications-Tabelle.

    Upsert über medications.frodor_medication_id. frodor ist Quelle der
    Wahrheit für Name/Dosierung/Lagerung/Hinweise; Einnahme-Slots
    (morgens/mittags/…) und Start/Ende werden lokal gepflegt und nie
    überschrieben. In frodor gelöschte Einträge werden lokal deaktiviert
    (active=0), taucht ein Eintrag wieder auf, wird er reaktiviert."""
    regs_by_uuid = {r["uuid"]: r for r in regs}
    stats = {"created": 0, "updated": 0, "unchanged": 0,
             "deactivated": 0, "skipped": 0}
    seen_frodor_ids: set = set()
    pid_by_reg: dict = {}

    for med in meds:
        reg_uuid = med["registration_uuid"]
        seen_frodor_ids.add(med["frodor_id"])

        pid = pid_by_reg.get(reg_uuid)
        if pid is None:
            row = conn.execute(
                "SELECT id FROM patients WHERE frodor_registration_uuid = ?",
                (reg_uuid,),
            ).fetchone()
            if row:
                pid = row["id"]
            else:
                reg = regs_by_uuid.get(reg_uuid)
                if not reg or not reg.get("name") or not reg.get("geburtsdatum"):
                    stats["skipped"] += 1
                    continue
                pid = adopt_frodor_registration(conn, reg)
            pid_by_reg[reg_uuid] = pid

        existing = conn.execute(
            "SELECT * FROM medications WHERE frodor_medication_id = ?",
            (med["frodor_id"],),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO medications
                  (patient_id, name, dosage, lagerung, notes, frodor_medication_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (pid, med["name"], med["dosage"] or None,
                 med["lagerung"] or None, med["notes"] or None,
                 med["frodor_id"]),
            )
            stats["created"] += 1
        else:
            same = (existing["name"] == med["name"]
                    and (existing["dosage"] or "") == med["dosage"]
                    and (existing["lagerung"] or "") == med["lagerung"]
                    and (existing["notes"] or "") == med["notes"]
                    and existing["patient_id"] == pid
                    and existing["active"] == 1)
            if same:
                stats["unchanged"] += 1
            else:
                conn.execute(
                    """
                    UPDATE medications
                    SET name = ?, dosage = ?, lagerung = ?, notes = ?,
                        patient_id = ?, active = 1
                    WHERE id = ?
                    """,
                    (med["name"], med["dosage"] or None,
                     med["lagerung"] or None, med["notes"] or None,
                     pid, existing["id"]),
                )
                stats["updated"] += 1

    # In frodor entfernte Einträge deaktivieren (nicht löschen — lokale
    # Slots/Vergabe-Historie bleiben erhalten)
    for row in conn.execute(
        "SELECT id, frodor_medication_id FROM medications "
        "WHERE frodor_medication_id IS NOT NULL AND active = 1"
    ).fetchall():
        if row["frodor_medication_id"] not in seen_frodor_ids:
            conn.execute("UPDATE medications SET active = 0 WHERE id = ?",
                         (row["id"],))
            stats["deactivated"] += 1

    return stats


def medication_sheet_patients(conn, stamm: Optional[str] = None) -> list[dict]:
    """Daten für den Medikamentenschein: pro Person die strukturierten
    Plan-Medikamente UND der Anmeldungs-Freitext (frodor). Gruppierbar
    nach Stamm. Personen erscheinen, wenn sie mindestens eines von
    beidem haben."""
    med_rows = list_medications_by_stamm(conn, stamm=stamm)
    where = ""
    params: tuple = ()
    if stamm is not None:
        where = "AND COALESCE(TRIM(stammnummer), '') = ?"
        params = (stamm.strip(),)
    text_rows = conn.execute(
        f"""
        SELECT id AS patient_id, name, geburtsdatum,
               COALESCE(TRIM(stammnummer), '') AS stamm,
               TRIM(medications_text) AS anmeldung_text
        FROM patients
        WHERE TRIM(COALESCE(medications_text, '')) != '' {where}
        """,
        params,
    ).fetchall()

    patients: dict = {}
    def _entry(pid, name, geb, stamm_v):
        if pid not in patients:
            patients[pid] = {"patient_id": pid, "name": name,
                             "geburtsdatum": geb, "stamm": stamm_v,
                             "meds": [], "anmeldung_text": ""}
        return patients[pid]

    for r in med_rows:
        e = _entry(r["patient_id"], r["name"], r["geburtsdatum"], r["stamm"])
        e["meds"].append(dict(r))
    for r in text_rows:
        e = _entry(r["patient_id"], r["name"], r["geburtsdatum"], r["stamm"])
        e["anmeldung_text"] = r["anmeldung_text"]

    return sorted(patients.values(),
                  key=lambda e: (e["stamm"].lower(), e["name"].lower()))


def list_medications_by_stamm(conn, stamm: Optional[str] = None) -> list[sqlite3.Row]:
    """Aktive Medikationen inkl. Patient — optional auf einen Stamm
    gefiltert (stamm='' → Patienten ohne Stamm). None = alle, sortiert
    nach Stamm → Name → Medikament (fürs gruppierte PDF)."""
    where = ""
    params: tuple = ()
    if stamm is not None:
        where = "WHERE COALESCE(TRIM(p.stammnummer), '') = ?"
        params = (stamm.strip(),)
    return conn.execute(
        f"""
        SELECT p.id AS patient_id, p.name, p.geburtsdatum,
               COALESCE(TRIM(p.stammnummer), '') AS stamm,
               m.name AS med_name, m.dosage,
               m.morgens, m.mittags, m.abends, m.nachts, m.bei_bedarf,
               m.lagerung, m.notes
        FROM patients p
        JOIN medications m ON m.patient_id = p.id AND m.active = 1
        {where}
        ORDER BY stamm COLLATE NOCASE,
                 p.name COLLATE NOCASE,
                 m.name COLLATE NOCASE
        """,
        params,
    ).fetchall()


# ---------- MANV (Massenanfall von Verletzten) ----------

MANV_KATEGORIEN = ("I", "II", "III", "IV", "tot")
MANV_KATEGORIE_LABEL = {
    "I":   "I · akute Lebensgefahr (rot)",
    "II":  "II · schwer verletzt (gelb)",
    "III": "III · leicht verletzt (grün)",
    "IV":  "IV · ohne Überlebenschance (blau)",
    "tot": "Tot (schwarz)",
}
MANV_KATEGORIE_COLOR = {
    "I": "#b3261e", "II": "#d49a00", "III": "#1f6b3a",
    "IV": "#2c5b8a", "tot": "#1a1a1a",
}
MANV_STATUS_VALUES = (
    "blank", "gesichtet", "in_behandlung", "transportiert", "abgeschlossen"
)


def create_manv_event(conn, *, name: str, card_prefix: str,
                       notes: Optional[str] = None,
                       situation: Optional[str] = None,
                       einsatzort: Optional[str] = None,
                       lage_bild: Optional[str] = None,
                       created_by: Optional[int] = None) -> int:
    """Legt einen MANV-Vorfall an, der zunächst NICHT alarmiert ist
    (status='aktiv', is_alarmiert=0). Erst wenn ein Admin alarmiert,
    sehen normale User das Event."""
    cur = conn.execute(
        """
        INSERT INTO manv_events
          (name, card_prefix, notes, situation, einsatzort, lage_bild,
           created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name.strip(), card_prefix.strip(),
         (notes or "").strip() or None,
         (situation or "").strip() or None,
         (einsatzort or "").strip() or None,
         (lage_bild or "").strip() or None,
         created_by),
    )
    return cur.lastrowid


MANV_EVENT_EDIT_FIELDS = (
    "name", "card_prefix", "notes", "situation", "einsatzort", "lage_bild"
)


def update_manv_event(conn, eid: int, fields: dict) -> bool:
    """Editiert die operativen Felder eines MANV-Events.
    Kann jederzeit aufgerufen werden, auch nach Alarmierung."""
    cols = [f for f in fields if f in MANV_EVENT_EDIT_FIELDS]
    if not cols:
        return False
    set_sql = ", ".join(f"{c}=?" for c in cols)
    params = [(fields[c] or "").strip() or None if isinstance(fields[c], str)
               else fields[c] for c in cols]
    params.append(eid)
    conn.execute(f"UPDATE manv_events SET {set_sql} WHERE id=?", params)
    return True


def alarm_manv_event(conn, eid: int, *,
                      by_user_id: Optional[int] = None) -> bool:
    """Setzt is_alarmiert=1. Bestehende alarmierte Events werden
    abgeschlossen (nur eins kann gleichzeitig alarmiert sein)."""
    # Alle anderen alarmierten Events auf 'abgeschlossen' setzen
    conn.execute(
        "UPDATE manv_events SET status='abgeschlossen', "
        "closed_at=datetime('now', 'localtime'), is_alarmiert=0 "
        "WHERE is_alarmiert=1 AND id != ?", (eid,),
    )
    cur = conn.execute(
        "UPDATE manv_events SET is_alarmiert=1, status='aktiv', "
        "alarm_at=COALESCE(alarm_at, datetime('now', 'localtime')), "
        "alarm_by=COALESCE(alarm_by, ?), closed_at=NULL "
        "WHERE id=?", (by_user_id, eid),
    )
    return cur.rowcount > 0


def dealarm_manv_event(conn, eid: int) -> bool:
    """Versehentlich alarmiert → zurück auf 'geplant' (is_alarmiert=0).
    Daten + Status='aktiv' bleiben erhalten, Event ist für User wieder
    unsichtbar."""
    cur = conn.execute(
        "UPDATE manv_events SET is_alarmiert=0, alarm_at=NULL, alarm_by=NULL "
        "WHERE id=?", (eid,),
    )
    return cur.rowcount > 0


def get_alarmiertes_manv_event(conn) -> Optional[sqlite3.Row]:
    """Das EINE Event, das aktuell für User sichtbar ist."""
    return conn.execute(
        "SELECT * FROM manv_events WHERE is_alarmiert=1 AND status='aktiv' "
        "ORDER BY datetime(alarm_at) DESC LIMIT 1"
    ).fetchone()


def list_manv_waiting_for_treatment(conn) -> list[sqlite3.Row]:
    """MANV-Karten des aktuell alarmierten Vorfalls, die gesichtet sind
    (SK1/SK2/SK3 — also behandlungspflichtig) aber noch kein zentrales
    Notfallprotokoll haben. Erscheinen in der Triage-Liste damit das
    Behandlerteam sie wie normale Wartepatienten aufrufen kann."""
    event = get_alarmiertes_manv_event(conn)
    if not event:
        return []
    return conn.execute(
        """
        SELECT c.*, e.name AS event_name, e.id AS manv_event_id
        FROM manv_cards c
        JOIN manv_events e ON e.id = c.manv_event_id
        WHERE c.manv_event_id = ?
          AND c.central_protocol_id IS NULL
          AND c.sichtung_kategorie IN ('I', 'II', 'III')
          AND c.status != 'abgeschlossen'
        ORDER BY
            CASE c.sichtung_kategorie
                WHEN 'I' THEN 1
                WHEN 'II' THEN 2
                WHEN 'III' THEN 3
                ELSE 4
            END ASC,
            datetime(c.created_at) ASC
        """,
        (event["id"],),
    ).fetchall()


def get_manv_event(conn, eid: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM manv_events WHERE id = ?", (eid,)
    ).fetchone()


def list_manv_events(conn) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT e.*,
               (SELECT COUNT(*) FROM manv_cards WHERE manv_event_id = e.id)
                   AS card_count,
               (SELECT COUNT(*) FROM manv_cards
                WHERE manv_event_id = e.id AND status != 'blank')
                   AS used_count
        FROM manv_events e
        ORDER BY datetime(e.started_at) DESC
        """
    ).fetchall()


def close_manv_event(conn, eid: int) -> bool:
    cur = conn.execute(
        "UPDATE manv_events SET status='abgeschlossen', "
        "closed_at=datetime('now', 'localtime') WHERE id=? AND status='aktiv'",
        (eid,),
    )
    return cur.rowcount > 0


def reopen_manv_event(conn, eid: int) -> bool:
    cur = conn.execute(
        "UPDATE manv_events SET status='aktiv', closed_at=NULL WHERE id=?",
        (eid,),
    )
    return cur.rowcount > 0


def delete_manv_event(conn, eid: int) -> bool:
    cur = conn.execute("DELETE FROM manv_events WHERE id=?", (eid,))
    return cur.rowcount > 0


def _next_card_no(conn, event_id: int, prefix: str) -> int:
    """Höchste bereits vergebene Karten-Nr für dieses Event +1."""
    row = conn.execute(
        """
        SELECT card_no FROM manv_cards WHERE manv_event_id = ?
        ORDER BY id DESC LIMIT 1
        """, (event_id,)
    ).fetchone()
    if not row:
        return 1
    # card_no = "<prefix>-NNN"
    try:
        return int(row["card_no"].rsplit("-", 1)[-1]) + 1
    except Exception:
        return conn.execute(
            "SELECT COUNT(*)+1 FROM manv_cards WHERE manv_event_id = ?",
            (event_id,),
        ).fetchone()[0]


def allocate_manv_cards(conn, *, event_id: int, count: int) -> list[sqlite3.Row]:
    """Legt `count` neue Blanko-Karten an. Liefert die neuen Rows."""
    import secrets as _secrets
    event = get_manv_event(conn, event_id)
    if not event:
        return []
    prefix = event["card_prefix"]
    new_ids = []
    start = _next_card_no(conn, event_id, prefix)
    for i in range(count):
        n = start + i
        card_no = f"{prefix}-{n:03d}"
        # Eindeutigen QR-Token — 12 Hex-Zeichen reicht (2^48 ~280 Trillion)
        token = _secrets.token_hex(6)
        # Falls zufällig doch eine Kollision: nochmal versuchen
        for _ in range(3):
            existing = conn.execute(
                "SELECT 1 FROM manv_cards WHERE qr_token=?", (token,)
            ).fetchone()
            if not existing: break
            token = _secrets.token_hex(6)
        cur = conn.execute(
            """
            INSERT INTO manv_cards (manv_event_id, card_no, qr_token)
            VALUES (?, ?, ?)
            """,
            (event_id, card_no, token),
        )
        new_ids.append(cur.lastrowid)
    if not new_ids:
        return []
    placeholders = ",".join("?" for _ in new_ids)
    return conn.execute(
        f"SELECT * FROM manv_cards WHERE id IN ({placeholders}) ORDER BY id",
        new_ids,
    ).fetchall()


def list_manv_cards(conn, event_id: int,
                     status: Optional[str] = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM manv_cards WHERE manv_event_id = ?"
    params: list = [event_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id"
    return conn.execute(sql, params).fetchall()


def get_manv_card(conn, cid: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM manv_cards WHERE id = ?", (cid,)
    ).fetchone()


def get_manv_card_by_token(conn, token: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM manv_cards WHERE qr_token = ?", (token,)
    ).fetchone()


MANV_CARD_EDIT_FIELDS = (
    "name", "vorname", "geburtsdatum", "alter_jahre", "geschlecht",
    "nationalitaet",
    "sichtung_kategorie",
    "diag_verletzung", "diag_verbrennung", "diag_erkrankung",
    "diag_vergiftung", "diag_verstrahlung", "diag_psyche",
    "diag_lokalisation",
    "bewusstsein", "atmung", "kreislauf", "zustand_zeit",
    "th_infusion", "th_analgetika", "th_antidote", "th_sonstige",
    "th_sonstige_text",
    "transport_mittel", "transport_ziel", "transport_art",
    "transport_mit_arzt", "transport_isoliert", "transport_prio",
    "bemerkungen",
)


def update_manv_card(conn, cid: int, fields: dict) -> bool:
    if not fields:
        return False
    cols = [f for f in fields if f in MANV_CARD_EDIT_FIELDS]
    if not cols:
        return False
    set_sql = ", ".join(f"{c}=?" for c in cols)
    params = [fields[c] for c in cols]
    params.append(cid)
    conn.execute(
        f"UPDATE manv_cards SET {set_sql}, updated_at=datetime('now', 'localtime') "
        f"WHERE id=?", params)
    return True


def manv_card_add_sichtung(conn, cid: int, *, kategorie: str,
                            sichter_name: str) -> bool:
    """Trägt eine neue Sichtung in sichtungen_json ein (Liste anhängen) und
    aktualisiert den aktuellen Status der Karte."""
    if kategorie not in MANV_KATEGORIEN:
        return False
    card = get_manv_card(conn, cid)
    if not card:
        return False
    try:
        sichtungen = _json.loads(card["sichtungen_json"] or "[]")
    except Exception:
        sichtungen = []
    from datetime import datetime as _dt
    sichtungen.append({
        "time": _dt.now().isoformat(timespec="seconds"),
        "name": (sichter_name or "").strip() or None,
        "kategorie": kategorie,
    })
    conn.execute(
        """
        UPDATE manv_cards
        SET sichtungen_json=?,
            sichtung_kategorie=?,
            status = CASE WHEN status = 'blank' THEN 'gesichtet'
                          ELSE status END,
            updated_at=datetime('now', 'localtime')
        WHERE id=?
        """,
        (_json.dumps(sichtungen, ensure_ascii=False), kategorie, cid),
    )
    return True


def manv_card_set_status(conn, cid: int, status: str) -> bool:
    if status not in MANV_STATUS_VALUES:
        return False
    conn.execute(
        "UPDATE manv_cards SET status=?, updated_at=datetime('now', 'localtime') WHERE id=?",
        (status, cid),
    )
    return True


def manv_card_link_patient(conn, cid: int, patient_id: int) -> bool:
    conn.execute(
        "UPDATE manv_cards SET patient_id=?, updated_at=datetime('now', 'localtime') WHERE id=?",
        (patient_id, cid),
    )
    return True


def manv_card_link_protocol(conn, cid: int, protocol_id: int) -> bool:
    conn.execute(
        """
        UPDATE manv_cards
        SET central_protocol_id=?,
            status = CASE WHEN status IN ('blank', 'gesichtet')
                          THEN 'in_behandlung' ELSE status END,
            updated_at=datetime('now', 'localtime')
        WHERE id=?
        """,
        (protocol_id, cid),
    )
    return True


def delete_manv_card_if_unused(conn, cid: int) -> tuple[bool, str]:
    """Löscht eine MANV-Karte nur wenn sie noch nichts „enthält":
    keine Sichtung, kein verknüpftes Protokoll, kein Patient, keine
    eingetragenen Personalia/Diagnose/Bemerkungen. Gibt (ok, grund)
    zurück; bei ok=False steht in grund warum nicht."""
    card = conn.execute(
        "SELECT * FROM manv_cards WHERE id=?", (cid,)
    ).fetchone()
    if not card:
        return False, "Karte nicht gefunden"
    if card["central_protocol_id"]:
        return False, "Notfallprotokoll bereits verknüpft"
    if card["patient_id"]:
        return False, "Patient bereits verknüpft"
    if card["status"] not in ("blank", "gesichtet"):
        return False, (f"Status '{card['status']}' "
                       "- Karte wird bereits behandelt")
    # Hat die Karte Inhalt? Sichtung / Personalia / Diagnose / Bemerkung
    has_content = any(card[c] for c in (
        "sichtung_kategorie", "vorname", "name", "geburtsdatum",
        "alter_jahre", "geschlecht", "nationalitaet",
        "diag_lokalisation", "bemerkungen",
    ) if c in card.keys())
    try:
        sichtungen = _json.loads(card["sichtungen_json"] or "[]")
    except Exception:
        sichtungen = []
    if has_content or sichtungen:
        return False, ("Karte enthaelt bereits Daten - bitte "
                       "stattdessen Status auf 'abgeschlossen' setzen")
    conn.execute("DELETE FROM manv_cards WHERE id=?", (cid,))
    return True, ""


def get_active_manv_event(conn) -> Optional[sqlite3.Row]:
    """Das jüngste ALARMIERTE Event. Wenn ein Pool-Sticker gescannt
    wird, landet die Karte automatisch hier. Vor Alarmierung: None."""
    return get_alarmiertes_manv_event(conn)


def close_all_active_manv_events(conn, *, except_id: Optional[int] = None) -> int:
    """Alle anderen aktiven Events auf 'abgeschlossen' setzen.
    Wird beim Anlegen eines neuen aktiven Events aufgerufen."""
    sql = "UPDATE manv_events SET status='abgeschlossen', closed_at=datetime('now', 'localtime') WHERE status='aktiv'"
    params: list = []
    if except_id is not None:
        sql += " AND id != ?"
        params.append(except_id)
    cur = conn.execute(sql, params)
    return cur.rowcount


# --- Sticker-Pool (Vorbereitung vor dem Einsatz) ---

# Alphabet für zufällige Pool-Codes: Kleinbuchstaben + Ziffern, ohne
# leicht verwechselbare Zeichen (0/o/O, 1/l/I).
POOL_CODE_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
POOL_CODE_LEN = 10


def _gen_pool_code(conn) -> str:
    """Generiert einen zufälligen 10-Zeichen Code (z. B. 'c382j2i8aq')
    und stellt sicher, dass er noch nicht vergeben ist."""
    import secrets as _secrets
    for _ in range(20):
        code = "".join(_secrets.choice(POOL_CODE_ALPHABET)
                       for _ in range(POOL_CODE_LEN))
        if not conn.execute(
            "SELECT 1 FROM manv_cards WHERE card_no = ?", (code,),
        ).fetchone():
            return code
    raise RuntimeError("Could not generate unique pool card code")


def create_sticker_pool(conn, *, count: int,
                         created_by: Optional[int] = None
                         ) -> list[sqlite3.Row]:
    """Legt N Pool-Karten (ohne Event-Bindung) an mit zufälligen
    10-Zeichen Codes (Kleinbuchstaben + Ziffern, ohne 0/o/1/l) und je
    zufälligem QR-Token. Liefert die Rows."""
    import secrets as _secrets
    new_ids = []
    for _ in range(count):
        code = _gen_pool_code(conn)
        token = _secrets.token_hex(8)  # 16 hex chars = 2^64 möglich
        for _ in range(3):
            if not conn.execute(
                "SELECT 1 FROM manv_cards WHERE qr_token=?", (token,)
            ).fetchone():
                break
            token = _secrets.token_hex(8)
        cur = conn.execute(
            "INSERT INTO manv_cards (manv_event_id, card_no, qr_token) "
            "VALUES (NULL, ?, ?)", (code, token),
        )
        new_ids.append(cur.lastrowid)
    if not new_ids:
        return []
    placeholders = ",".join("?" for _ in new_ids)
    return conn.execute(
        f"SELECT * FROM manv_cards WHERE id IN ({placeholders}) ORDER BY id",
        new_ids,
    ).fetchall()


def delete_all_unused_pool_cards(conn) -> int:
    """Löscht alle Pool-Sticker, die garantiert unbenutzt sind: keinem
    Event zugeordnet, kein Protokoll, kein Patient, kein Inhalt, Status
    blank. Gibt die Anzahl gelöschter Zeilen zurück."""
    cur = conn.execute(
        """
        DELETE FROM manv_cards
        WHERE manv_event_id IS NULL
          AND central_protocol_id IS NULL
          AND patient_id IS NULL
          AND status = 'blank'
          AND sichtung_kategorie IS NULL
          AND vorname IS NULL AND name IS NULL
          AND geburtsdatum IS NULL
          AND diag_lokalisation IS NULL
          AND bemerkungen IS NULL
          AND (sichtungen_json IS NULL OR sichtungen_json IN ('', '[]'))
        """
    )
    return cur.rowcount


def list_pool_cards(conn, *, limit: int = 1000) -> list[sqlite3.Row]:
    """Pool-Karten = noch keinem Event zugeordnet, status='blank'."""
    return conn.execute(
        "SELECT * FROM manv_cards "
        "WHERE manv_event_id IS NULL AND status='blank' "
        "ORDER BY id LIMIT ?", (limit,),
    ).fetchall()


def pool_stats(conn) -> dict:
    """Counts: im Pool / im aktuellen MANV / aus früheren Events."""
    pool = conn.execute(
        "SELECT COUNT(*) AS n FROM manv_cards "
        "WHERE manv_event_id IS NULL AND status='blank'"
    ).fetchone()["n"]
    active = get_active_manv_event(conn)
    in_active = 0
    if active:
        in_active = conn.execute(
            "SELECT COUNT(*) AS n FROM manv_cards WHERE manv_event_id=?",
            (active["id"],),
        ).fetchone()["n"]
    archive = conn.execute(
        "SELECT COUNT(*) AS n FROM manv_cards "
        "WHERE manv_event_id IS NOT NULL AND manv_event_id != COALESCE(?, -1)",
        (active["id"] if active else None,),
    ).fetchone()["n"]
    return {
        "pool": pool,
        "in_active": in_active,
        "active_event": dict(active) if active else None,
        "archive": archive,
    }


def claim_pool_card(conn, *, card_id: int, event_id: int) -> bool:
    """Pool-Karte für ein MANV-Event beanspruchen. Funktioniert nur
    wenn die Karte aktuell event_id=NULL hat."""
    cur = conn.execute(
        "UPDATE manv_cards SET manv_event_id=?, "
        "updated_at=datetime('now', 'localtime') "
        "WHERE id=? AND manv_event_id IS NULL", (event_id, card_id),
    )
    return cur.rowcount > 0


def manv_event_stats(conn, event_id: int) -> dict:
    """Liefert Counts pro Kategorie + pro Status für das Live-Dashboard."""
    rows = conn.execute(
        "SELECT sichtung_kategorie, COUNT(*) AS n FROM manv_cards "
        "WHERE manv_event_id=? GROUP BY sichtung_kategorie", (event_id,)
    ).fetchall()
    by_cat = {k: 0 for k in MANV_KATEGORIEN}
    by_cat["ungesichtet"] = 0
    for r in rows:
        k = r["sichtung_kategorie"] or "ungesichtet"
        by_cat[k] = by_cat.get(k, 0) + r["n"]
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM manv_cards "
        "WHERE manv_event_id=? GROUP BY status", (event_id,)
    ).fetchall()
    by_status = {s: 0 for s in MANV_STATUS_VALUES}
    for r in rows:
        by_status[r["status"]] = r["n"]
    total = sum(r["n"] for r in rows)
    return {"by_kategorie": by_cat, "by_status": by_status, "total": total}


# ---------- Einsatzbefehle + Einsatztagebuch ----------

EINSATZBEFEHL_FIELDS = (
    "befehlende_stelle", "takt_zeit", "befehl_fuer",
    "lage", "auftrag", "auftragsort", "ansprechpartner", "kontaktnummer",
    "durchfuehrung", "versorgung", "verbindung",
    "rueck_bezeichnung", "rueck_rufname", "rueck_funkgruppe", "rueck_telefon",
    "erstellt_von_text",
)


def _generate_einsatzbefehl_id() -> str:
    """Eindeutige lange ID — Datum + 8 Hex-Zeichen.
    Format: EB-YYYYMMDD-XXXXXXXX (z.B. EB-20260531-A4F1B2C8)."""
    import secrets as _secrets
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y%m%d")
    suffix = _secrets.token_hex(4).upper()  # 8 Hex
    return f"EB-{today}-{suffix}"


def create_einsatzbefehl(conn, *, event_id: Optional[int],
                          created_by: Optional[int] = None,
                          **fields) -> tuple[int, str]:
    """Legt einen Einsatzbefehl an. fields = beliebige Auswahl aus
    EINSATZBEFEHL_FIELDS. Liefert (id, eindeutige_id)."""
    # Eindeutige ID — bei (sehr unwahrscheinlicher) Kollision 3 Versuche
    for _ in range(3):
        unique = _generate_einsatzbefehl_id()
        existing = conn.execute(
            "SELECT 1 FROM einsatzbefehle WHERE eindeutige_id=?", (unique,)
        ).fetchone()
        if not existing:
            break
    cols = ["event_id", "eindeutige_id", "created_by"]
    vals = [event_id, unique, created_by]
    for f in EINSATZBEFEHL_FIELDS:
        if f in fields:
            cols.append(f)
            v = fields[f]
            vals.append((v or "").strip() or None if isinstance(v, str) else v)
    placeholders = ",".join("?" for _ in cols)
    col_sql = ",".join(cols)
    cur = conn.execute(
        f"INSERT INTO einsatzbefehle ({col_sql}) VALUES ({placeholders})",
        vals,
    )
    return cur.lastrowid, unique


def get_einsatzbefehl(conn, eid: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT e.*, ev.name AS event_name, ev.prefix AS event_prefix,
               u.username AS creator_username, u.full_name AS creator_full
        FROM einsatzbefehle e
        LEFT JOIN events ev ON ev.id = e.event_id
        LEFT JOIN users u ON u.id = e.created_by
        WHERE e.id = ?
        """, (eid,),
    ).fetchone()


def list_einsatzbefehle(conn, *,
                         event_id: Optional[int] = None
                         ) -> list[sqlite3.Row]:
    sql = """
        SELECT e.*, ev.name AS event_name, ev.prefix AS event_prefix,
               u.username AS creator_username, u.full_name AS creator_full,
               (SELECT COUNT(*) FROM einsatztagebuch
                WHERE einsatzbefehl_id = e.id) AS tagebuch_count
        FROM einsatzbefehle e
        LEFT JOIN events ev ON ev.id = e.event_id
        LEFT JOIN users u ON u.id = e.created_by
        WHERE 1=1
    """
    params: list = []
    if event_id is not None:
        sql += " AND e.event_id = ?"
        params.append(event_id)
    sql += " ORDER BY datetime(e.created_at) DESC"
    return conn.execute(sql, params).fetchall()


def update_einsatzbefehl(conn, eid: int, fields: dict) -> bool:
    cols = [f for f in fields if f in EINSATZBEFEHL_FIELDS]
    if not cols:
        return False
    set_sql = ", ".join(f"{c}=?" for c in cols)
    params = []
    for c in cols:
        v = fields[c]
        params.append((v or "").strip() or None if isinstance(v, str) else v)
    params.append(eid)
    conn.execute(
        f"UPDATE einsatzbefehle SET {set_sql}, "
        f"updated_at=datetime('now', 'localtime') WHERE id=?", params,
    )
    return True


def delete_einsatzbefehl(conn, eid: int) -> bool:
    cur = conn.execute("DELETE FROM einsatzbefehle WHERE id=?", (eid,))
    return cur.rowcount > 0


# --- Einsatztagebuch ---

def create_einsatztagebuch(conn, *, einsatzbefehl_id: int,
                            einrichtung_einheit: Optional[str] = None,
                            einsatz_anlass: Optional[str] = None,
                            created_by: Optional[int] = None) -> int:
    cur = conn.execute(
        """
        INSERT INTO einsatztagebuch
          (einsatzbefehl_id, einrichtung_einheit, einsatz_anlass, created_by)
        VALUES (?, ?, ?, ?)
        """,
        (einsatzbefehl_id,
         (einrichtung_einheit or "").strip() or None,
         (einsatz_anlass or "").strip() or None,
         created_by),
    )
    return cur.lastrowid


def get_einsatztagebuch(conn, tid: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT t.*, e.eindeutige_id AS befehl_uid,
               e.befehl_fuer AS befehl_fuer,
               u.username AS creator_username,
               u.full_name AS creator_full
        FROM einsatztagebuch t
        LEFT JOIN einsatzbefehle e ON e.id = t.einsatzbefehl_id
        LEFT JOIN users u ON u.id = t.created_by
        WHERE t.id = ?
        """, (tid,),
    ).fetchone()


def list_einsatztagebuch_for_befehl(conn, einsatzbefehl_id: int
                                     ) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT t.*,
               (SELECT COUNT(*) FROM einsatztagebuch_eintraege
                WHERE tagebuch_id = t.id) AS eintrag_count
        FROM einsatztagebuch t
        WHERE einsatzbefehl_id = ?
        ORDER BY datetime(created_at) DESC
        """, (einsatzbefehl_id,),
    ).fetchall()


def update_einsatztagebuch(conn, tid: int, *,
                            einrichtung_einheit: Optional[str] = None,
                            einsatz_anlass: Optional[str] = None) -> bool:
    conn.execute(
        "UPDATE einsatztagebuch SET einrichtung_einheit=?, einsatz_anlass=? "
        "WHERE id=?",
        ((einrichtung_einheit or "").strip() or None,
         (einsatz_anlass or "").strip() or None, tid),
    )
    return True


def delete_einsatztagebuch(conn, tid: int) -> bool:
    cur = conn.execute("DELETE FROM einsatztagebuch WHERE id=?", (tid,))
    return cur.rowcount > 0


def add_tagebuch_eintrag(conn, *, tagebuch_id: int, ea: str,
                          taktische_zeit: Optional[str],
                          darstellung: str,
                          vollzug: Optional[str] = None,
                          anlage: Optional[str] = None,
                          created_by: Optional[int] = None) -> int:
    # Nächste lfd_nr berechnen
    row = conn.execute(
        "SELECT COALESCE(MAX(lfd_nr), 0) AS m "
        "FROM einsatztagebuch_eintraege WHERE tagebuch_id=?",
        (tagebuch_id,),
    ).fetchone()
    next_nr = (row["m"] if row else 0) + 1
    cur = conn.execute(
        """
        INSERT INTO einsatztagebuch_eintraege
          (tagebuch_id, lfd_nr, ea, taktische_zeit, darstellung,
           vollzug, anlage, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (tagebuch_id, next_nr, (ea or "").strip().upper()[:1] or None,
         (taktische_zeit or "").strip() or None,
         (darstellung or "").strip(),
         (vollzug or "").strip() or None,
         (anlage or "").strip() or None,
         created_by),
    )
    return cur.lastrowid


def list_tagebuch_eintraege(conn, tagebuch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM einsatztagebuch_eintraege
        WHERE tagebuch_id = ? ORDER BY lfd_nr
        """, (tagebuch_id,),
    ).fetchall()


def delete_tagebuch_eintrag(conn, eintrag_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM einsatztagebuch_eintraege WHERE id=?", (eintrag_id,))
    return cur.rowcount > 0
