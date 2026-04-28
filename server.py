#!/usr/bin/env python3
"""
PatProtokoll — lokaler Server für Patientenprotokolle.
Zero dependencies: Python 3 stdlib + SQLite.

Start:  python3 server.py
Browser: http://localhost:8765
"""
import json
import sqlite3
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, quote

from pdf_fill import render_pdf

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "protokolle.db"
INDEX_PATH = ROOT / "index.html"
PORT = 8765


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS protokolle (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                vorname     TEXT,
                nachname    TEXT,
                einsatznummer TEXT,
                datum       TEXT,
                data        TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        db.commit()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def list_protokolle():
    with get_db() as db:
        rows = db.execute("""
            SELECT id, vorname, nachname, einsatznummer, datum, created_at, updated_at
            FROM protokolle
            ORDER BY datetime(updated_at) DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_protokoll(pid):
    with get_db() as db:
        row = db.execute("SELECT * FROM protokolle WHERE id = ?", (pid,)).fetchone()
        if not row:
            return None
        record = dict(row)
        record["data"] = json.loads(record["data"] or "{}")
        return record


def create_protokoll(data):
    now = now_iso()
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO protokolle
               (vorname, nachname, einsatznummer, datum, data, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                _first(data.get("vorname")),
                _first(data.get("nachname")),
                _first(data.get("einsatznummer")),
                _first(data.get("datum")),
                json.dumps(data, ensure_ascii=False),
                now, now,
            ),
        )
        db.commit()
        return cur.lastrowid


def update_protokoll(pid, data):
    now = now_iso()
    with get_db() as db:
        cur = db.execute(
            """UPDATE protokolle SET
                 vorname=?, nachname=?, einsatznummer=?, datum=?,
                 data=?, updated_at=?
               WHERE id=?""",
            (
                _first(data.get("vorname")),
                _first(data.get("nachname")),
                _first(data.get("einsatznummer")),
                _first(data.get("datum")),
                json.dumps(data, ensure_ascii=False),
                now,
                pid,
            ),
        )
        db.commit()
        return cur.rowcount > 0


def delete_protokoll(pid):
    with get_db() as db:
        cur = db.execute("DELETE FROM protokolle WHERE id = ?", (pid,))
        db.commit()
        return cur.rowcount > 0


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self._send_file(INDEX_PATH, "text/html; charset=utf-8")
            return
        if path == "/api/protokolle":
            self._send_json(200, list_protokolle())
            return
        # /api/protokolle/{id}/pdf
        if path.startswith("/api/protokolle/") and path.endswith("/pdf"):
            try:
                pid = int(path.split("/")[3])
            except (ValueError, IndexError):
                self._send_json(400, {"error": "invalid id"}); return
            rec = get_protokoll(pid)
            if rec is None:
                self._send_json(404, {"error": "not found"}); return
            try:
                pdf_bytes = render_pdf(rec["data"])
            except Exception as e:
                self._send_json(500, {"error": f"PDF-Erzeugung fehlgeschlagen: {e}"}); return
            name = (rec["data"].get("nachname") or "Protokoll")
            if isinstance(name, list): name = name[0] if name else "Protokoll"
            datum = rec["data"].get("datum") or rec["updated_at"][:10]
            if isinstance(datum, list): datum = datum[0] if datum else ""
            filename = f"Protokoll_{name}_{datum}.pdf".replace(" ", "_")
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'inline; filename*=UTF-8\'\'{quote(filename)}')
            self.send_header("Content-Length", str(len(pdf_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(pdf_bytes)
            return

        if path.startswith("/api/protokolle/"):
            try:
                pid = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._send_json(400, {"error": "invalid id"}); return
            rec = get_protokoll(pid)
            if rec is None:
                self._send_json(404, {"error": "not found"}); return
            self._send_json(200, rec)
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/protokolle":
            data = self._read_json()
            pid = create_protokoll(data)
            self._send_json(201, {"id": pid})
            return
        self.send_error(404)

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/api/protokolle/"):
            try:
                pid = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._send_json(400, {"error": "invalid id"}); return
            data = self._read_json()
            ok = update_protokoll(pid, data)
            if not ok:
                self._send_json(404, {"error": "not found"}); return
            self._send_json(200, {"id": pid})
            return
        self.send_error(404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/protokolle/"):
            try:
                pid = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._send_json(400, {"error": "invalid id"}); return
            ok = delete_protokoll(pid)
            if not ok:
                self._send_json(404, {"error": "not found"}); return
            self._send_json(200, {"deleted": pid})
            return
        self.send_error(404)


def main():
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"PatProtokoll läuft auf {url}")
    print(f"Datenbank: {DB_PATH}")
    print("Beenden mit Strg+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer gestoppt.")
        server.server_close()


if __name__ == "__main__":
    main()
