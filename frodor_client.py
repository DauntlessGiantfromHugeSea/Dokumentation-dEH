"""Client für die frodor-Anmeldeplattform (Supabase).

Verbindet das Dokumentations-Tool mit den Camp-Anmeldungen eines Events:
  - Registrierungen live lesen (RPC get_registrations_by_event, RLS-gefiltert)
  - Protokoll-PDFs an eine Anmeldung hängen (registration_files + Storage)

Auth: dedizierter Service-Account (Supabase E-Mail/Passwort). Der Account ist
serverseitig auf ein Event, eine Feld-Whitelist und 'medical.protocol'-Dateien
beschränkt — dieses Modul kann also nie mehr sehen oder schreiben, als die
frodor-Rolle 'Sanitätsdokumentation' erlaubt.

Bewusst nur Standard-Bibliothek (urllib), kein neues Dependency.

Konfiguration über Umgebungsvariablen (ohne diese ist die Integration aus
und die App verhält sich wie bisher):
    FRODOR_SUPABASE_URL       z.B. http://localhost:54321
    FRODOR_SUPABASE_ANON_KEY  anon/publishable Key des Projekts
    FRODOR_EMAIL              Service-Account, z.B. sanidoku@frodor.de
    FRODOR_PASSWORD           Passwort des Service-Accounts
    FRODOR_EVENT_SLUG         Event-Slug (Default: dc-ost-2026)
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

REQUEST_TIMEOUT = 15          # Sekunden pro HTTP-Request
REGISTRATION_CACHE_TTL = 60   # Sekunden — "live genug" fürs Camp, schont die API


class FrodorError(Exception):
    """Fehler in der frodor-Anbindung (Netz, Auth, Berechtigung)."""


def is_configured() -> bool:
    return bool(
        os.environ.get("FRODOR_SUPABASE_URL")
        and os.environ.get("FRODOR_SUPABASE_ANON_KEY")
        and os.environ.get("FRODOR_EMAIL")
        and os.environ.get("FRODOR_PASSWORD")
    )


def _base_url() -> str:
    return os.environ["FRODOR_SUPABASE_URL"].rstrip("/")


def _anon_key() -> str:
    return os.environ["FRODOR_SUPABASE_ANON_KEY"]


def event_slug() -> str:
    return os.environ.get("FRODOR_EVENT_SLUG", "dc-ost-2026")


# ---------------------------------------------------------------------------
# HTTP + Auth
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_token: dict = {}        # {"access_token": str, "expires_at": float}
_event_uuid: str | None = None
_medical_module_id: int | None = None
_reg_cache: dict = {}    # {"fetched_at": float, "registrations": list}


def _http(method: str, url: str, *, headers: dict, body: bytes | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FrodorError(f"frodor nicht erreichbar: {e}") from e


def _login() -> str:
    """Passwort-Login gegen Supabase Auth; Token wird gecacht."""
    url = f"{_base_url()}/auth/v1/token?grant_type=password"
    payload = json.dumps({
        "email": os.environ["FRODOR_EMAIL"],
        "password": os.environ["FRODOR_PASSWORD"],
    }).encode("utf-8")
    status, raw = _http("POST", url, headers={
        "apikey": _anon_key(),
        "Content-Type": "application/json",
    }, body=payload)
    if status != 200:
        raise FrodorError(
            f"frodor-Login fehlgeschlagen (HTTP {status}): {raw[:200]!r}")
    data = json.loads(raw)
    _token["access_token"] = data["access_token"]
    # 60s Puffer vor Ablauf neu einloggen
    _token["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 60
    return _token["access_token"]


def _access_token() -> str:
    with _lock:
        if _token.get("access_token") and time.time() < _token.get("expires_at", 0):
            return _token["access_token"]
        return _login()


def _api(method: str, path: str, *, body: dict | list | None = None,
         content: bytes | None = None, content_type: str = "application/json",
         extra_headers: dict | None = None) -> tuple[int, bytes]:
    """Authentifizierter Request; bei 401 einmal neu einloggen und wiederholen."""
    if not is_configured():
        raise FrodorError(
            "frodor ist nicht konfiguriert (FRODOR_* Umgebungsvariablen).")
    payload = content if content is not None else (
        json.dumps(body).encode("utf-8") if body is not None else None
    )
    for attempt in (1, 2):
        headers = {
            "apikey": _anon_key(),
            "Authorization": f"Bearer {_access_token()}",
        }
        if payload is not None:
            headers["Content-Type"] = content_type
        if extra_headers:
            headers.update(extra_headers)
        status, raw = _http(
            method, f"{_base_url()}{path}", headers=headers, body=payload)
        if status == 401 and attempt == 1:
            with _lock:
                _token.clear()
            continue
        return status, raw
    return status, raw  # pragma: no cover


# ---------------------------------------------------------------------------
# Stammdaten (Event, Modul)
# ---------------------------------------------------------------------------

def _get_event_uuid() -> str:
    global _event_uuid
    if _event_uuid:
        return _event_uuid
    slug = urllib.parse.quote(event_slug())
    status, raw = _api("GET", f"/rest/v1/events?slug=eq.{slug}&select=uuid")
    if status != 200:
        raise FrodorError(
            f"Event-Lookup fehlgeschlagen (HTTP {status}): {raw[:200]!r}")
    rows = json.loads(raw)
    if not rows:
        raise FrodorError(
            f"Event '{event_slug()}' nicht sichtbar — hat der Service-Account "
            "einen People-Record im Event?"
        )
    _event_uuid = rows[0]["uuid"]
    return _event_uuid


def _get_medical_module_id() -> int:
    global _medical_module_id
    if _medical_module_id:
        return _medical_module_id
    status, raw = _api(
        "GET", "/rest/v1/modules?identifier=eq.medical&select=id")
    if status != 200:
        raise FrodorError(
            f"Modul-Lookup fehlgeschlagen (HTTP {status}): {raw[:200]!r}")
    rows = json.loads(raw)
    if not rows:
        raise FrodorError("Medical-Modul in frodor nicht gefunden/sichtbar.")
    _medical_module_id = rows[0]["id"]
    return _medical_module_id


# ---------------------------------------------------------------------------
# Registrierungen lesen
# ---------------------------------------------------------------------------

GENDER_MAP = {
    "m": "männlich", "male": "männlich", "männlich": "männlich",
    "w": "weiblich", "f": "weiblich", "female": "weiblich",
    "weiblich": "weiblich",
    "d": "divers", "divers": "divers", "diverse": "divers",
    "other": "divers",
}


def _extract_personal(data: dict) -> dict:
    """Personalia aus data.personal (Whitelist-Pfade der Rolle
    'Sanitätsdokumentation'): street, streetNr, zip, city, mobile,
    gender. Fehlt der Block (ältere Whitelist), bleiben die Felder
    leer — kein Verhaltenswechsel."""
    personal = data.get("personal") or {}
    if not isinstance(personal, dict):
        personal = {}
    street = str(personal.get("street") or "").strip()
    street_nr = str(personal.get("streetNr") or "").strip()
    if street_nr and street_nr not in street:
        street = f"{street} {street_nr}".strip()
    gender_raw = str(personal.get("gender") or "").strip().lower()
    return {
        "strasse": street,
        "plz": str(personal.get("zip") or "").strip(),
        "stadt": str(personal.get("city") or "").strip(),
        "telefon": str(personal.get("mobile") or "").strip(),
        "geschlecht": GENDER_MAP.get(gender_raw, ""),
    }


def _normalize(reg: dict) -> dict:
    """RPC-JSONB → flaches Dict fürs UI. Nur Whitelist-Felder vorhanden."""
    data = reg.get("data") or {}
    health = data.get("health") or {}
    contacts = data.get("contact") or []
    if isinstance(contacts, dict):  # ältere Datensätze: Objekt statt Liste
        contacts = [contacts]
    first = (reg.get("first_name") or "").strip()
    last = (reg.get("last_name") or "").strip()
    personal = _extract_personal(data)
    return {
        "strasse": personal["strasse"],
        "plz": personal["plz"],
        "stadt": personal["stadt"],
        "telefon": personal["telefon"],
        "geschlecht": personal["geschlecht"],
        "uuid": reg.get("uuid"),
        "first_name": first,
        "last_name": last,
        "name": " ".join(p for p in (first, last) if p),
        # ISO, wie patients.geburtsdatum
        "geburtsdatum": reg.get("bday") or "",
        "stamm": data.get("stamm") or "",
        "team": data.get("team") or "",
        "allergies": (health.get("allergies") or "").strip(),
        "restrictions": (health.get("restrictions") or "").strip(),
        # Interne Erste-Hilfe-Notizen aus der Anmeldung ('note') —
        # je nach Whitelist unter health oder direkt in data.
        "note": (str(health.get("note") or data.get("note") or "")).strip(),
        "has_medications": health.get("hasMedications"),
        "medications": (health.get("medications") or "").strip(),
        "tetanus": health.get("tetanus") or "",
        "insurance_provider": health.get("insuranceProvider") or "",
        "insurance_number": health.get("insuranceNumber") or "",
        "agree_first_aid": (data.get("agree") or {}).get("medicalFirstAid"),
        "agree_treatment": (data.get("agree") or {}).get("medicalTreatment"),
        "contacts": [
            {
                "name": " ".join(p for p in ((c.get("firstName") or "").strip(),
                                             (c.get("lastName") or "").strip()) if p),
                "relation": c.get("relation") or "",
                "mobile": c.get("mobile") or "",
                "phone": c.get("phone") or "",
            }
            for c in contacts if isinstance(c, dict)
        ],
    }


def list_registrations(force: bool = False) -> list[dict]:
    """Alle sichtbaren Registrierungen des Events (kurz gecacht)."""
    with _lock:
        fresh = time.time() - _reg_cache.get("fetched_at", 0) < REGISTRATION_CACHE_TTL
        if not force and fresh and "registrations" in _reg_cache:
            return _reg_cache["registrations"]
    status, raw = _api("POST", "/rest/v1/rpc/get_registrations_by_event",
                       body={"requested_event_uuid": _get_event_uuid()})
    if status != 200:
        raise FrodorError(
            f"Registrierungen laden fehlgeschlagen (HTTP {status}): {raw[:200]!r}")
    regs = sorted(
        (_normalize(r) for r in json.loads(raw)),
        key=lambda r: (r["name"].lower(), r["geburtsdatum"]),
    )
    with _lock:
        _reg_cache["fetched_at"] = time.time()
        _reg_cache["registrations"] = regs
    return regs


def search_registrations(query: str, limit: int = 12) -> list[dict]:
    """Case-insensitive Substring-Suche über Name (+ Stamm)."""
    query = (query or "").strip().lower()
    if len(query) < 2:
        return []
    hits = [
        r for r in list_registrations()
        if query in r["name"].lower() or query in r["stamm"].lower()
    ]
    return hits[:limit]


def get_registration(registration_uuid: str) -> dict | None:
    for r in list_registrations():
        if r["uuid"] == registration_uuid:
            return r
    # Cache könnte veraltet sein → einmal frisch versuchen
    for r in list_registrations(force=True):
        if r["uuid"] == registration_uuid:
            return r
    return None


# ---------------------------------------------------------------------------
# Protokoll-PDF hochladen
# ---------------------------------------------------------------------------

def build_protocol_path(registration_uuid: str) -> str:
    """Pfad-Konvention wie in frodor: {uuid}/medical/protocol-{timestamp}.pdf"""
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3] + "Z"
    return f"{registration_uuid}/medical/protocol-{ts}.pdf"


def create_file_entry(registration_uuid: str, path: str, display_name: str) -> None:
    """Schritt 1: registration_files-Zeile anlegen (RLS prüft die
    'registration.file.medical.protocol.create'-Berechtigung)."""
    status, raw = _api(
        "POST", "/rest/v1/registration_files",
        body={
            "registration_uuid": registration_uuid,
            "module_id": _get_medical_module_id(),
            "type": "protocol",
            "bucket": "registrations",
            "name": display_name[:100],
            "path": path,
        },
        extra_headers={"Prefer": "return=minimal"},
    )
    if status not in (200, 201):
        raise FrodorError(
            f"Datei-Eintrag anlegen fehlgeschlagen (HTTP {status}): {raw[:300]!r}"
        )


def upload_file_content(path: str, pdf_bytes: bytes) -> None:
    """Schritt 2: PDF in den Storage-Bucket laden (Storage-RLS prüft gegen
    die Zeile aus Schritt 1). 409 = Objekt existiert schon → früherer
    Upload war erfolgreich, zählt als Erfolg."""
    quoted = urllib.parse.quote(path)
    status, raw = _api(
        "POST", f"/storage/v1/object/registrations/{quoted}",
        content=pdf_bytes, content_type="application/pdf",
    )
    if status == 409:
        return
    if status not in (200, 201):
        raise FrodorError(
            f"PDF-Upload fehlgeschlagen (HTTP {status}): {raw[:300]!r}")


def check_connection() -> dict:
    """Für die Status-Seite: Login + Event + Anzahl Registrierungen."""
    regs = list_registrations(force=True)
    return {
        "event_slug": event_slug(),
        "event_uuid": _get_event_uuid(),
        "registration_count": len(regs),
    }
