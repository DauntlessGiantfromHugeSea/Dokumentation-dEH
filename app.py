"""Erste-Hilfe Camp-Dokumentation — Flask web app."""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from functools import wraps
from pathlib import Path

import click
import io as _io
import pyotp
import segno
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)

import models
from pdf_export import render_protocol_pdf
from pdf_fill import render_pdf as render_central_pdf
from event_report_pdf import render_event_report_pdf


TOTP_ISSUER = "Erste-Hilfe-Camp"


def _qr_svg_for_uri(uri: str) -> str:
    qr = segno.make(uri, error="m")
    buf = _io.BytesIO()
    qr.save(buf, kind="svg", xmldecl=False, scale=5, border=2)
    return buf.getvalue().decode("utf-8")


def _login_landing(user) -> str:
    """Where a user lands right after a successful (full) login."""
    if user.is_triage_intake:
        return url_for("triage_new")
    if user.is_zentral_only:
        return url_for("central_index")
    return url_for("index")


def _current_event_id() -> int | None:
    db = models.get_db()
    if not current_user.is_authenticated:
        return None
    raw = session.get("event_id")
    try:
        event_id = int(raw) if raw else None
    except (TypeError, ValueError):
        event_id = None
    available = models.list_user_events(
        db, current_user.id, is_admin=current_user.is_admin
    )
    available_ids = {e["id"] for e in available}
    if event_id in available_ids:
        return event_id
    if available:
        event_id = available[0]["id"]
        session["event_id"] = event_id
        return event_id
    if current_user.is_admin:
        event_id = models.get_default_event_id(db)
        session["event_id"] = event_id
        return event_id
    return None


def _current_event():
    event_id = _current_event_id()
    return models.get_event(models.get_db(), event_id) if event_id else None


def _require_event_view(event_id: int | None = None) -> int:
    db = models.get_db()
    event_id = event_id or _current_event_id()
    if not event_id or not models.user_can_view_event(
            db, current_user.id, event_id, current_user.is_admin):
        abort(403)
    return event_id


def _require_event_create() -> int:
    db = models.get_db()
    event_id = _current_event_id()
    if not event_id or not models.user_can_create_in_event(
            db, current_user.id, event_id, current_user.is_admin):
        abort(403)
    return event_id


# Triage-intake users are restricted to the kiosk flow.
TRIAGE_INTAKE_ALLOWED = (
    "/triage/new", "/triage", "/account/password",
    "/login", "/logout", "/setup-totp", "/two-factor",
    "/events/switch", "/static/", "/api/triage/",
)


def _is_path_allowed_for_intake(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path.startswith(p)
               for p in TRIAGE_INTAKE_ALLOWED)


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.url))
        if not getattr(current_user, "is_admin", False):
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def decentral_view_required(view):
    """For routes that show decentral / patient / export data — blocked
    for users with role 'zentral_writer'."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.url))
        if not current_user.can_view_decentral:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DB_PATH=Path(os.environ.get("DB_PATH", "data/app.db")),
    )
    if test_config:
        app.config.update(test_config)

    app.teardown_appcontext(models.close_db)

    @app.template_filter("from_json")
    def _from_json(value):
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            return value
        try:
            import json as _j
            return _j.loads(value)
        except Exception:
            return None

    # Auto-init schema on startup so the first request never hits a missing table.
    with app.app_context():
        models.init_db(Path(app.config["DB_PATH"]))

    # Vor jedem Request: App-Anzeige-Zeitzone aus DB lesen und in g
    # ablegen, damit format_dt sie nutzen kann.
    @app.before_request
    def _set_display_tz():
        try:
            from flask import g
            tz = models.get_app_setting(models.get_db(), "app_timezone")
            if tz:
                g.display_tz = tz
        except Exception:
            pass

    # Triage-Kiosk-Schutz: 'triage_intake' Konten dürfen nur den
    # Anmelde-Flow nutzen — alle anderen URLs liefern 403.
    @app.before_request
    def _restrict_triage_intake():
        if not current_user.is_authenticated:
            return  # login_required handles auth
        if not getattr(current_user, "is_triage_intake", False):
            return
        if _is_path_allowed_for_intake(request.path):
            return
        abort(403)

    @app.context_processor
    def _inject_events():
        if not current_user.is_authenticated:
            return {}
        db = models.get_db()
        event_id = _current_event_id()
        return {
            "available_events": models.list_user_events(
                db, current_user.id, is_admin=current_user.is_admin
            ),
            "current_event": models.get_event(db, event_id) if event_id else None,
            "can_create_in_current_event": (
                models.user_can_create_in_event(
                    db, current_user.id, event_id, current_user.is_admin
                ) if event_id else False
            ),
        }

    # ----- Auth -----
    login_manager = LoginManager(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Bitte zuerst anmelden."

    class User(UserMixin):
        def __init__(self, row):
            self.id = row["id"]
            self.username = row["username"]
            self.full_name = row["full_name"]
            self.is_admin = bool(row["is_admin"])
            # role is "full" or "zentral_writer"; admins always get full access.
            self.role = (row["role"] or "full") if "role" in row.keys() else "full"
            # Per-User-Berechtigungen (additiv zu Rolle/Admin)
            keys = row.keys() if hasattr(row, "keys") else []
            for perm in ("perm_view_contact", "perm_export_pdf",
                         "perm_export_akte", "perm_edit_patient"):
                setattr(self, perm,
                        bool(row[perm]) if perm in keys else False)

        @property
        def is_zentral_only(self) -> bool:
            return (not self.is_admin) and self.role == "zentral_writer"

        @property
        def is_triage_intake(self) -> bool:
            """Kiosk-Account: darf nur Patienten anmelden, sonst nichts."""
            return (not self.is_admin) and self.role == "triage_intake"

        @property
        def can_view_decentral(self) -> bool:
            return not self.is_zentral_only

        @property
        def can_view_others_central(self) -> bool:
            return not self.is_zentral_only

        # ---- Per-User-Berechtigungen (Admin hat immer alles) ----
        @property
        def can_view_contact(self) -> bool:
            return self.is_admin or self.perm_view_contact

        @property
        def can_export_pdf(self) -> bool:
            return self.is_admin or self.perm_export_pdf

        @property
        def can_export_akte(self) -> bool:
            return self.is_admin or self.perm_export_akte

        @property
        def can_edit_patient(self) -> bool:
            return self.is_admin or self.perm_edit_patient

    @login_manager.user_loader
    def load_user(user_id: str):
        row = models.get_user_by_id(models.get_db(), int(user_id))
        return User(row) if row else None

    # ----- Routes -----

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(_login_landing(current_user))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            row = models.get_user_by_username(models.get_db(), username)
            if row and models.verify_password(row, password):
                # Per-user opt-out: if 2FA is disabled for this user, skip
                # both setup and challenge — log them in directly.
                if not row["totp_required"]:
                    user = User(row)
                    login_user(user)
                    return redirect(
                        request.args.get("next") or _login_landing(user)
                    )
                # Otherwise: 2FA mandatory. Don't login_user yet.
                session.clear()
                session["pending_user_id"] = row["id"]
                if request.args.get("next"):
                    session["pending_next"] = request.args["next"]
                if not row["totp_secret"] or not row["totp_confirmed"]:
                    return redirect(url_for("setup_totp"))
                return redirect(url_for("two_factor"))
            flash("Benutzername oder Passwort falsch.", "error")
        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        session.clear()
        return redirect(url_for("login"))

    @app.route("/events/switch", methods=["POST"])
    @login_required
    def event_switch():
        try:
            event_id = int(request.form.get("event_id") or 0)
        except (TypeError, ValueError):
            event_id = 0
        if not models.user_can_view_event(
                models.get_db(), current_user.id, event_id, current_user.is_admin):
            abort(403)
        session["event_id"] = event_id
        next_url = request.form.get("next") or request.referrer or url_for("dashboard")
        return redirect(next_url)

    @app.route("/setup-totp", methods=["GET", "POST"])
    def setup_totp():
        user_id = session.get("pending_user_id")
        if not user_id:
            return redirect(url_for("login"))
        db = models.get_db()
        user_row = models.get_user_by_id(db, user_id)
        if not user_row:
            session.clear()
            return redirect(url_for("login"))

        # Generate a fresh secret if none yet, or if the previous one was
        # confirmed (shouldn't happen for an unconfirmed user, but defensive).
        secret = user_row["totp_secret"]
        if not secret or user_row["totp_confirmed"]:
            secret = pyotp.random_base32()
            models.set_totp_secret(db, user_id, secret, confirmed=False)
            db.commit()

        if request.method == "POST":
            code = (request.form.get("code") or "").strip().replace(" ", "")
            if pyotp.TOTP(secret).verify(code, valid_window=1):
                models.set_totp_confirmed(db, user_id, True)
                db.commit()
                # Complete login.
                next_url = session.pop("pending_next", None)
                session.pop("pending_user_id", None)
                user = User(models.get_user_by_id(db, user_id))
                login_user(user)
                flash("2FA erfolgreich eingerichtet.", "success")
                return redirect(next_url or _login_landing(user))
            flash("Code falsch — bitte noch einmal versuchen.", "error")

        uri = pyotp.TOTP(secret).provisioning_uri(
            name=user_row["username"], issuer_name=TOTP_ISSUER,
        )
        return render_template(
            "setup_totp.html",
            qr_svg=_qr_svg_for_uri(uri),
            secret=secret,
            username=user_row["username"],
        )

    @app.route("/two-factor", methods=["GET", "POST"])
    def two_factor():
        user_id = session.get("pending_user_id")
        if not user_id:
            return redirect(url_for("login"))
        db = models.get_db()
        user_row = models.get_user_by_id(db, user_id)
        if (not user_row or not user_row["totp_secret"]
                or not user_row["totp_confirmed"]):
            # Nothing to verify against — back to login (or setup).
            session.clear()
            return redirect(url_for("login"))

        if request.method == "POST":
            code = (request.form.get("code") or "").strip().replace(" ", "")
            if pyotp.TOTP(user_row["totp_secret"]).verify(code, valid_window=1):
                next_url = session.pop("pending_next", None)
                session.pop("pending_user_id", None)
                user = User(user_row)
                login_user(user)
                return redirect(next_url or _login_landing(user))
            flash("Code falsch.", "error")

        return render_template("two_factor.html",
                               username=user_row["username"])

    # ----- Protocols -----

    @app.route("/")
    @decentral_view_required
    def index():
        event_id = _require_event_view()
        filters = _read_filters(request.args)
        source_filter = (request.args.get("type") or "").strip() or None
        if source_filter not in ("decentral", "central"):
            source_filter = None
        unified = models.list_unified_protocols(
            models.get_db(),
            event_id=event_id,
            **filters,
            source_filter=source_filter,
        )
        return render_template(
            "index.html",
            protocols=unified,
            filters=filters,
            source_filter=source_filter,
            format_dt=models.format_dt,
        )

    @app.route("/protocols/new")
    @login_required
    def protocol_new_chooser():
        _require_event_create()
        """Step 1: choose between decentral or central first aid.

        zentral_writer hat nur eine Option ("Zentrale EH") — direkt
        weiterleiten, sonst die Auswahl zeigen.
        """
        prefill = {
            "name": request.args.get("name", ""),
            "geburtsdatum": request.args.get("geburtsdatum", ""),
            "stammnummer": request.args.get("stammnummer", ""),
        }
        if current_user.is_zentral_only:
            # Direkt zur zentralen Patientenprüfung
            vorname = prefill["name"].split(" ")[0] if prefill["name"] else ""
            nachname = (prefill["name"].rsplit(" ", 1)[-1]
                        if prefill["name"] and " " in prefill["name"] else "")
            return redirect(url_for("central_new",
                                    vorname=vorname, nachname=nachname,
                                    geburtsdatum=prefill["geburtsdatum"]))
        return render_template("protocol_chooser.html", prefill=prefill)

    @app.route("/protocols/new/dezentral", methods=["GET", "POST"])
    @decentral_view_required
    def protocol_new():
        _require_event_create()
        if request.method == "POST":
            return _save_protocol(None)
        prefill = {
            "patient_name": request.args.get("name", ""),
            "patient_geburtsdatum": request.args.get("geburtsdatum", ""),
            "patient_stammnummer": request.args.get("stammnummer", ""),
        }
        return render_template(
            "protocol_form.html", protocol=prefill,
            existing_patient=None, mode="new",
            responder_options=models.list_decentral_responders(models.get_db()),
        )

    @app.route("/protocols/<int:protocol_id>")
    @login_required
    def protocol_detail(protocol_id: int):
        db = models.get_db()
        protocol = models.get_protocol(db, protocol_id)
        if not protocol:
            abort(404)
        _require_event_view(protocol["event_id"])
        comments = models.list_comments(db, protocol_id)
        siblings = models.list_patient_protocols(
            db, protocol["patient_id"], event_id=protocol["event_id"])
        return render_template("protocol_detail.html", protocol=protocol,
                               comments=comments, siblings=siblings,
                               format_dt=models.format_dt)

    @app.route("/protocols/<int:protocol_id>/edit", methods=["GET", "POST"])
    @decentral_view_required
    def protocol_edit(protocol_id: int):
        db = models.get_db()
        protocol = models.get_protocol(db, protocol_id)
        if not protocol:
            abort(404)
        _require_event_view(protocol["event_id"])
        if request.method == "POST":
            return _save_protocol(protocol_id)
        return render_template(
            "protocol_form.html", protocol=protocol,
            existing_patient=None, mode="edit",
            responder_options=models.list_decentral_responders(db),
        )

    @app.route("/protocols/<int:protocol_id>/delete", methods=["POST"])
    @decentral_view_required
    def protocol_delete(protocol_id: int):
        db = models.get_db()
        protocol = models.get_protocol(db, protocol_id)
        if not protocol:
            abort(404)
        _require_event_view(protocol["event_id"])
        password = request.form.get("password", "")
        user_row = models.get_user_by_id(db, current_user.id)
        if not user_row or not models.verify_password(user_row, password):
            flash("Passwort falsch — Bericht wurde nicht gelöscht.", "error")
            return redirect(url_for("protocol_detail", protocol_id=protocol_id))
        label = protocol["laufende_nr"] or f"#dEH{protocol_id}"
        models.delete_protocol(db, protocol_id)
        db.commit()
        flash(f"Bericht {label} gelöscht.", "success")
        return redirect(url_for("index"))

    @app.route("/protocols/<int:protocol_id>/comments", methods=["POST"])
    @login_required
    def protocol_add_comment(protocol_id: int):
        db = models.get_db()
        protocol = models.get_protocol(db, protocol_id)
        if not protocol:
            abort(404)
        _require_event_view(protocol["event_id"])
        text = request.form.get("text", "").strip()
        if text:
            models.add_comment(db, protocol_id, text, current_user.id)
            db.commit()
            flash("Kommentar hinzugefügt.", "success")
        return redirect(url_for("protocol_detail", protocol_id=protocol_id))

    @app.route("/protocols/<int:protocol_id>/pdf")
    @login_required
    def protocol_pdf(protocol_id: int):
        db = models.get_db()
        protocol = models.get_protocol(db, protocol_id)
        if not protocol:
            abort(404)
        _require_event_view(protocol["event_id"])
        comments = models.list_comments(db, protocol_id)
        pdf_bytes = render_protocol_pdf(protocol, comments)
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"Einsatzbericht_{protocol_id}.pdf",
        )

    # ----- Patients -----

    @app.route("/patients")
    @decentral_view_required
    def patient_list():
        patients = models.list_patients(models.get_db(),
                                        event_id=_require_event_view())
        return render_template("patient_list.html", patients=patients,
                               format_dt=models.format_dt)

    @app.route("/patients/<int:patient_id>")
    @login_required
    def patient_detail(patient_id: int):
        event_id = _require_event_view()
        db = models.get_db()
        patient = models.get_patient(db, patient_id)
        if not patient:
            abort(404)
        protocols = models.list_patient_protocols(db, patient_id, event_id=event_id)
        central_protocols = models.list_central_protocols(db, patient_id=patient_id,
                                                          event_id=event_id)
        change_log = models.list_patient_changes(db, patient_id)
        sensitive = (
            models.patient_sensitive_full(patient)
            if current_user.can_view_contact
            else models.patient_sensitive_summary(patient)
        )
        return render_template("patient_detail.html", patient=patient,
                               protocols=protocols,
                               central_protocols=central_protocols,
                               change_log=change_log,
                               sensitive=sensitive,
                               sensitive_full=current_user.can_view_contact,
                               field_label=models.PATIENT_FIELD_LABELS,
                               sensitive_fields=set(models.SENSITIVE_PATIENT_FIELDS),
                               format_dt=models.format_dt)

    @app.route("/patients/<int:patient_id>/akte")
    @login_required
    def patient_akte(patient_id: int):
        if not current_user.can_export_akte:
            abort(403)
        event_id = _require_event_view()
        """Vorschauseite für den vollständigen Akten-Export.
        Zeigt, was im PDF landet, plus den Download-Button.
        Sensible Daten erscheinen nur für Admins; Voll-User sehen
        gemaskt und können den Export trotzdem auslösen — die PDF
        enthält dann nur die nicht-vertraulichen Teile."""
        db = models.get_db()
        patient = models.get_patient(db, patient_id)
        if not patient:
            abort(404)
        decentral = models.list_patient_protocols(db, patient_id, event_id=event_id)
        central = models.list_central_protocols(db, patient_id=patient_id,
                                                event_id=event_id)
        change_log = models.list_patient_changes(db, patient_id)
        unlocks = db.execute(
            """
            SELECT eu.*, u1.username AS req_user, u1.full_name AS req_full,
                   u2.username AS app_user, u2.full_name AS app_full
            FROM emergency_unlocks eu
            LEFT JOIN users u1 ON u1.id = eu.requested_by
            LEFT JOIN users u2 ON u2.id = eu.approved_by
            WHERE eu.patient_id = ?
            ORDER BY eu.unlocked_at DESC, eu.id DESC
            """,
            (patient_id,),
        ).fetchall()
        return render_template(
            "patient_akte.html",
            patient=patient,
            decentral=decentral,
            central=central,
            change_log=change_log,
            unlocks=unlocks,
            sensitive_visible=current_user.can_view_contact,
            format_dt=models.format_dt,
            current_event=_current_event(),
        )

    @app.route("/patients/<int:patient_id>/akte.pdf")
    @login_required
    def patient_akte_pdf(patient_id: int):
        if not current_user.can_export_akte:
            abort(403)
        event_id = _require_event_view()
        db = models.get_db()
        patient = models.get_patient(db, patient_id)
        if not patient:
            abort(404)
        # Sammle alles ein
        decentral = []
        for p in models.list_patient_protocols(db, patient_id, event_id=event_id):
            full = models.get_protocol(db, p["id"])
            full_dict = dict(full)
            full_dict["comments"] = [dict(c) for c in models.list_comments(db, p["id"])]
            decentral.append(full_dict)
        central = []
        for c in models.list_central_protocols(db, patient_id=patient_id,
                                               event_id=event_id):
            rec = models.get_central_protocol(db, c["id"])
            rec["comments"] = [dict(cm) for cm in
                               models.list_central_comments(db, c["id"])]
            rec["triage"] = models.get_triage_for_protocol(db, c["id"])
            # Behandler-Name auflösen (created_by → user)
            if rec.get("created_by"):
                u = models.get_user_by_id(db, rec["created_by"])
                if u:
                    rec["author_full_name"] = u["full_name"]
                    rec["author_username"] = u["username"]
            central.append(rec)
        change_log = [dict(r) for r in models.list_patient_changes(db, patient_id)]
        # Sensitive Daten landen nur dann im PDF, wenn der Exporter
        # can_view_contact hat (Admin oder explizit per perm_view_contact)
        include_sensitive = current_user.can_view_contact
        from akte_export import render_patient_akte_pdf
        pdf_bytes = render_patient_akte_pdf(
            patient=dict(patient),
            decentral=decentral,
            central=central,
            change_log=change_log,
            include_sensitive=include_sensitive,
            exporter_label=(current_user.full_name or current_user.username),
            field_label=models.PATIENT_FIELD_LABELS,
            sensitive_fields=set(models.SENSITIVE_PATIENT_FIELDS),
        )
        # Audit-Eintrag, dass exportiert wurde — als spezieller Pseudo-Feldname
        db.execute(
            "INSERT INTO patient_changes "
            "(patient_id, changed_by, field_name, old_value, new_value) "
            "VALUES (?, ?, ?, ?, ?)",
            (patient_id, current_user.id, "__akte_export__", None,
             "Admin-Sicht" if include_sensitive else "Standard-Sicht"),
        )
        db.commit()
        safe_name = (patient["name"] or "Patient").replace(" ", "_")
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"Akte_{safe_name}.pdf",
        )

    @app.route("/patients/<int:patient_id>/edit", methods=["GET", "POST"])
    @login_required
    def patient_edit(patient_id: int):
        if not current_user.can_edit_patient:
            abort(403)
        db = models.get_db()
        patient = models.get_patient(db, patient_id)
        if not patient:
            abort(404)
        # Sensible Felder dürfen nur User mit can_view_contact ändern
        # (Admin oder explizit per perm_view_contact freigeschaltet) — ohne
        # diese Berechtigung sehen sie die Felder gar nicht erst.
        if request.method == "POST":
            updates = {}
            for f in models.PATIENT_EDIT_FIELDS:
                # Sensitive Felder nur bei View-Contact-Berechtigung akzeptieren
                if (f in models.SENSITIVE_PATIENT_FIELDS
                        and not current_user.can_view_contact):
                    continue
                if f in ("has_allergies", "has_medications"):
                    raw = request.form.get(f, "").strip()
                    updates[f] = (1 if raw == "1"
                                  else 0 if raw == "0"
                                  else None if raw in ("", "unknown")
                                  else None)
                else:
                    updates[f] = request.form.get(f, "")
            if not updates.get("name") or not updates.get("geburtsdatum"):
                flash("Name und Geburtsdatum sind Pflichtfelder.", "error")
                return redirect(url_for("patient_edit", patient_id=patient_id))
            try:
                n = models.update_patient(db, patient_id, updates,
                                          changed_by=current_user.id)
                db.commit()
                if n == 0:
                    flash("Keine Änderungen.", "info")
                else:
                    flash(f"Gespeichert ({n} Änderung{'en' if n != 1 else ''}).",
                          "success")
            except sqlite3.IntegrityError:
                flash("Ein anderer Patient mit Name + Geburtsdatum existiert bereits.",
                      "error")
            return redirect(url_for("patient_detail", patient_id=patient_id))
        return render_template("patient_edit.html", patient=patient,
                               can_edit_sensitive=current_user.is_admin,
                               format_dt=models.format_dt)

    @app.route("/patients/<int:patient_id>/delete", methods=["POST"])
    @login_required
    def patient_delete(patient_id: int):
        # Patient-Löschen ist Admin-only — geht durch Stammdaten +
        # alle dezentralen Berichte. Zentrale Berichte bleiben erhalten,
        # verlieren aber die Verknüpfung (patient_id=NULL).
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        patient = models.get_patient(db, patient_id)
        if not patient:
            abort(404)
        # Passwort-Bestätigung gegen den eingeloggten User
        password = request.form.get("password", "")
        user_row = models.get_user_by_id(db, current_user.id)
        if not user_row or not models.verify_password(user_row, password):
            flash("Passwort falsch — Patient wurde nicht gelöscht.", "error")
            return redirect(url_for("patient_detail", patient_id=patient_id))
        name = patient["name"]
        models.delete_patient(db, patient_id)
        db.commit()
        flash(f"Patient „{name}“ wurde gelöscht.", "success")
        return redirect(url_for("patient_list"))

    # ----- Lookup for the new-protocol form (so the UI can announce
    # "Folgebehandlung" before submit) -----

    @app.route("/api/patient-lookup")
    @decentral_view_required
    def patient_lookup():
        name = request.args.get("name", "").strip()
        geburtsdatum = request.args.get("geburtsdatum", "").strip()
        if not name or not geburtsdatum:
            return {"found": False}
        row = models.get_db().execute(
            "SELECT id, stammnummer FROM patients WHERE name = ? AND geburtsdatum = ?",
            (name, geburtsdatum),
        ).fetchone()
        if not row:
            return {"found": False}
        counts = models.patient_protocol_counts(models.get_db(), row["id"],
                                                event_id=_current_event_id())
        return {
            "found": True,
            "patient_id": row["id"],
            "stammnummer": row["stammnummer"] or "",
            "previous_count": counts["decentral"] + counts["central"],
            "previous_decentral": counts["decentral"],
            "previous_central": counts["central"],
        }

    # ----- Export -----

    @app.route("/export")
    @decentral_view_required
    def export_form():
        return render_template("export.html")

    @app.route("/export/csv")
    @decentral_view_required
    def export_csv():
        event_id = _require_event_view()
        filters = _read_filters(request.args)
        rows = models.list_protocols(models.get_db(), event_id=event_id, **filters)

        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow([
            "Laufende Nr.", "ID", "Patient Name", "Geburtsdatum", "Stammnummer",
            "Unfall Datum/Uhrzeit", "Unfallort", "Unfallhergang",
            "Art/Umfang Verletzung", "Zeugen",
            "EH Datum/Uhrzeit", "Ersthelfer",
            "Art/Weise Maßnahmen", "Verbrauchtes Material",
            "Erstellt am",
        ])
        # The list view query omits the long fields — refetch full rows.
        db = models.get_db()
        for r in rows:
            full = models.get_protocol(db, r["id"])
            writer.writerow([
                full["laufende_nr"] or "",
                full["id"], full["patient_name"], full["patient_geburtsdatum"],
                full["patient_stammnummer"] or "",
                full["unfall_datum_uhrzeit"] or "", full["unfallort"] or "",
                full["unfallhergang"] or "",
                full["art_umfang_verletzung"] or "", full["name_zeugen"] or "",
                full["eh_datum_uhrzeit"] or "", full["name_ersthelfer"] or "",
                full["art_weise_massnahmen"] or "",
                full["verbrauchtes_material"] or "",
                full["created_at"] or "",
            ])

        # UTF-8 BOM so Excel opens umlauts correctly.
        data = "﻿" + buf.getvalue()
        return Response(
            data.encode("utf-8"),
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": "attachment; filename=einsatzberichte.csv",
            },
        )

    @app.route("/events/report", methods=["GET", "POST"])
    @decentral_view_required
    def event_report():
        if not current_user.can_export_akte:
            abort(403)
        db = models.get_db()
        requested_event = request.values.get("event_id")
        try:
            requested_event_id = int(requested_event) if requested_event else None
        except (TypeError, ValueError):
            requested_event_id = None
        event_id = _require_event_view(requested_event_id)
        event = models.get_event(db, event_id)
        if not event:
            abort(404)
        source = (request.values.get("source") or "").strip()
        back_url = (
            url_for("admin_users") + "#events"
            if source == "settings" and current_user.is_admin
            else url_for("dashboard")
        )
        users = models.list_users(db)
        helper_options = sorted({
            (u["full_name"] or u["username"])
            for u in users
            if (u["full_name"] or u["username"])
        }, key=str.casefold)
        defaults = {
            "title": f"Veranstaltungs-Report {event['name']}",
            "event_name": event["name"],
            "start_date": event["start_date"] or "",
            "end_date": event["end_date"] or event["start_date"] or "",
            "location": "",
            "organizer": "",
            "medical_lead": "",
            "incident_lead": "",
            "notes": "",
        }
        if request.method == "POST":
            form = {k: (request.form.get(k) or "").strip()
                    for k in defaults.keys()}
            helpers = [
                h.strip() for h in request.form.getlist("helpers")
                if h and h.strip()
            ]
            stats = _event_report_stats(db, event_id)
            pdf_bytes = render_event_report_pdf(
                event=dict(event),
                form=form,
                helpers=helpers,
                stats=stats,
                exporter_label=current_user.full_name or current_user.username,
                format_dt=models.format_dt,
            )
            safe_name = (event["name"] or "Veranstaltung").replace(" ", "_")
            return send_file(
                io.BytesIO(pdf_bytes),
                mimetype="application/pdf",
                as_attachment=True,
                download_name=f"Veranstaltungsreport_{safe_name}.pdf",
            )
        return render_template(
            "event_report.html",
            event=event,
            defaults=defaults,
            helper_options=helper_options,
            source=source,
            back_url=back_url,
        )

    # ----- Dashboard -----

    @app.route("/dashboard")
    @decentral_view_required
    def dashboard():
        from datetime import date as _date
        db = models.get_db()
        event_id = _require_event_view()
        # Selected day for the day-view; default heute.
        sel = (request.args.get("date") or "").strip()
        try:
            ref = _date.fromisoformat(sel) if sel else _date.today()
        except ValueError:
            ref = _date.today()

        stats = models.dashboard_stats(db, ref, event_id=event_id)
        chart = models.dashboard_daily_counts(db, end_date=ref, days=14,
                                              event_id=event_id)
        chart_max = max((d["decentral"] + d["central"] for d in chart),
                        default=0)
        # Berichte des ausgewählten Tages
        day_protocols = models.list_unified_protocols(
            db, event_id=event_id,
            date_from=ref.isoformat(), date_to=ref.isoformat()
        )
        top = models.top_decentral_responders(db, limit=5, event_id=event_id)
        return render_template(
            "dashboard.html",
            stats=stats,
            chart=chart,
            chart_max=max(chart_max, 4),  # mind. 4er-Achse, sonst sieht's leer aus
            day_protocols=day_protocols,
            top_responders=top,
            ref_date=ref,
            today=_date.today(),
            format_dt=models.format_dt,
        )

    @app.route("/api/responders")
    @decentral_view_required
    def api_responders():
        return {"responders": models.list_decentral_responders(models.get_db())}

    # ----- PRIOR-Triage (Anmeldung) -----

    @app.route("/triage")
    @login_required
    def triage_list():
        db = models.get_db()
        event_id = _require_event_view()
        waiting = models.list_triage_waiting(db, event_id=event_id)
        active = models.list_triage_active(db, limit=20, event_id=event_id)
        recently_finished = models.list_triage_recently_finished(
            db, limit=10, event_id=event_id)
        # Höchste vorhandene Triage-ID — als Anker fürs Polling
        row = db.execute(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM triage_entries "
            "WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        max_triage_id = row["max_id"] if row else 0
        return render_template(
            "triage_list.html",
            waiting=waiting,
            active=active,
            recently_finished=recently_finished,
            indicator_lookup=models.PRIOR_INDICATOR_BY_KEY,
            format_dt=models.format_dt,
            max_triage_id=max_triage_id,
        )

    @app.route("/triage/new", methods=["GET", "POST"])
    @login_required
    def triage_new():
        db = models.get_db()
        event_id = _require_event_create()
        if request.method == "POST":
            indicators = request.form.getlist("indicators")
            name = (request.form.get("name") or "").strip()
            geburtsdatum = (request.form.get("geburtsdatum") or "").strip()
            notes = (request.form.get("notes") or "").strip()
            # Schnell-Auswahl per Knopf — übersteuert die berechnete
            # Kategorie (z. B. wenn der Aufnehmende ohne PRIOR-Klick
            # direkt SK I rot meldet).
            quick_cat = (request.form.get("quick_category") or "").strip()
            tid = models.create_triage_entry(
                db, name=name, geburtsdatum=geburtsdatum,
                indicators=indicators, notes=notes,
                created_by=current_user.id, event_id=event_id,
            )
            if quick_cat in ("SK1", "SK2", "SK3"):
                db.execute(
                    "UPDATE triage_entries SET category = ? WHERE id = ?",
                    (quick_cat, tid),
                )
            db.commit()
            flash(f"Triage-Eintrag #{tid} angelegt.", "success")
            # Kiosk-Mode: nach dem Anlegen sofort zurück zum leeren Formular
            if current_user.is_triage_intake:
                return redirect(url_for("triage_new"))
            return redirect(url_for("triage_list"))
        return render_template(
            "triage_new.html",
            indicators=models.PRIOR_INDICATORS,
        )

    @app.route("/api/triage/recent")
    @login_required
    def api_triage_recent():
        """Polling-API: liefert Triage-Einträge mit id > since_id, damit der
        Wartebereich live über neue Anmeldungen informieren kann."""
        try:
            since_id = int(request.args.get("since_id") or 0)
        except (TypeError, ValueError):
            since_id = 0
        event_id = _require_event_view()
        rows = models.get_db().execute(
            """
            SELECT id, category, name, arrival_at, status
            FROM triage_entries
            WHERE id > ? AND event_id = ?
            ORDER BY id DESC
            LIMIT 50
            """,
            (since_id, event_id),
        ).fetchall()
        return {
            "entries": [{
                "id": r["id"],
                "category": r["category"],
                "name": r["name"],
                "arrival_at": r["arrival_at"],
                "status": r["status"],
            } for r in rows]
        }

    @app.route("/api/triage/lookup")
    @login_required
    def api_triage_lookup():
        """Personenabgleich für /triage/new — erreichbar auch für den
        Anmelde-Kiosk (`triage_intake`) wegen /api/triage/-Prefix."""
        db = models.get_db()
        name = (request.args.get("name") or "").strip()
        geburtsdatum = (request.args.get("geburtsdatum") or "").strip()
        if not name and not geburtsdatum:
            return {"matches": []}
        rows = models.search_patients(
            db, name_query=name, geburtsdatum=geburtsdatum
        )
        matches = []
        for r in rows[:10]:
            counts = models.patient_protocol_counts(db, r["id"],
                                                    event_id=_current_event_id())
            last = models.patient_last_treatment(db, r["id"],
                                                 event_id=_current_event_id())
            matches.append({
                "patient_id": r["id"],
                "name": r["name"],
                "geburtsdatum": r["geburtsdatum"],
                "stammnummer": r["stammnummer"] or "",
                "previous_decentral": counts["decentral"],
                "previous_central": counts["central"],
                "last_treatment": last,
            })
        return {"matches": matches}

    @app.route("/triage/<int:tid>/start", methods=["POST"])
    @login_required
    def triage_start(tid: int):
        return _open_triage_protocol(tid)

    @app.route("/triage/<int:tid>/open", methods=["POST"])
    @login_required
    def triage_open(tid: int):
        return _open_triage_protocol(tid)

    def _open_triage_protocol(tid: int):
        db = models.get_db()
        entry = models.get_triage_entry(db, tid)
        if not entry:
            abort(404)
        _require_event_view(entry.get("event_id"))
        if current_user.is_triage_intake:
            abort(403)
        if entry.get("status") not in ("wartend", "in_behandlung"):
            flash("Dieser Triage-Eintrag ist nicht mehr in Behandlung.", "error")
            return redirect(url_for("triage_list"))
        if not entry.get("treatment_protocol_id") and not models.user_can_create_in_event(
                db, current_user.id, entry.get("event_id"), current_user.is_admin):
            abort(403)
        # Status auf in_behandlung setzen und den aktuellen Bearbeiter merken.
        models.start_triage_treatment(db, tid, started_by=current_user.id)
        db.commit()
        if entry.get("treatment_protocol_id"):
            return redirect(url_for("central_index")
                            + f"#{entry['treatment_protocol_id']}")
        # Zur SPA mit Pre-fill der Identifikations-Felder
        vorname = ""
        nachname = ""
        full = (entry.get("name") or "").strip()
        if full:
            parts = full.split(" ", 1)
            vorname = parts[0]
            nachname = parts[1] if len(parts) > 1 else ""
        return redirect(url_for(
            "central_index",
            vorname=vorname,
            nachname=nachname,
            geburtsdatum=entry.get("geburtsdatum") or "",
            triage_id=tid,
        ))

    @app.route("/triage/<int:tid>/cancel", methods=["POST"])
    @login_required
    def triage_cancel(tid: int):
        db = models.get_db()
        if not models.cancel_triage_entry(db, tid):
            flash("Eintrag konnte nicht abgebrochen werden.", "error")
        else:
            db.commit()
            flash(f"Triage-Eintrag #{tid} abgebrochen.", "success")
        return redirect(url_for("triage_list"))

    @app.route("/triage/<int:tid>/finish", methods=["POST"])
    @login_required
    def triage_finish(tid: int):
        db = models.get_db()
        entry = models.get_triage_entry(db, tid)
        if not entry:
            abort(404)
        if not models.finish_triage_treatment(db, tid):
            flash("Behandlung konnte nicht abgeschlossen werden "
                  "(eventuell schon abgeschlossen).", "error")
        else:
            db.commit()
            who = entry.get("name") or "Patient"
            flash(f"Behandlung von „{who}“ abgeschlossen.", "success")
        return redirect(url_for("triage_list"))

    @app.route("/triage/<int:tid>/reopen", methods=["POST"])
    @login_required
    def triage_reopen(tid: int):
        db = models.get_db()
        entry = models.get_triage_entry(db, tid)
        if not entry:
            abort(404)
        if not models.reopen_triage_treatment(db, tid):
            flash("Behandlung konnte nicht wieder geöffnet werden "
                  "(nur abgeschlossene Einträge sind reaktivierbar).",
                  "error")
        else:
            db.commit()
            who = entry.get("name") or "Patient"
            flash(f"Behandlung von „{who}“ wieder geöffnet.", "success")
        return redirect(url_for("triage_list"))

    @app.route("/api/central/protokolle/<int:pid>/triage-status")
    @login_required
    def api_central_triage_status(pid: int):
        """Liefert den Triage-Status, der zu diesem zentralen Bericht
        gehört — Datenquelle für den „Behandlung abschließen"-Button
        am Ende der SPA."""
        db = models.get_db()
        rec = models.get_central_protocol(db, pid)
        if not rec:
            return {"error": "not found"}, 404
        _require_event_view(rec.get("event_id"))
        triage = models.get_triage_for_protocol(db, pid)
        if not triage:
            return {"has_triage": False}
        finished_at = triage.get("treatment_finished_at")
        started_by_label = (
            triage.get("started_by_full_name")
            or triage.get("started_by_username")
            or triage.get("behandler_full_name")
            or triage.get("behandler_username")
            or ""
        )
        return {
            "has_triage": True,
            "triage_id": triage["id"],
            "status": triage.get("status"),
            "category": triage.get("category"),
            "started_at": triage.get("treatment_started_at"),
            "started_at_label": (
                models.format_dt(triage.get("treatment_started_at"))
                if triage.get("treatment_started_at") else ""
            ),
            "started_by": started_by_label,
            "started_by_is_current_user": (
                bool(triage.get("treatment_started_by"))
                and triage.get("treatment_started_by") == current_user.id
            ),
            "finished_at": finished_at,
            "finished_at_label": models.format_dt(finished_at) if finished_at else "",
        }

    @app.route("/api/central/protokolle/<int:pid>/finish-treatment",
               methods=["POST"])
    @login_required
    def api_central_finish_treatment(pid: int):
        """Schließt aus dem zentralen Bericht heraus den verlinkten Triage-
        Eintrag ab — der Patient verschwindet damit aus der Triage-Liste.
        Wird vom „Behandlung abschließen"-Button am Ende der SPA aufgerufen.
        """
        db = models.get_db()
        rec = models.get_central_protocol(db, pid)
        if not rec:
            return {"error": "not found"}, 404
        _require_event_view(rec.get("event_id"))
        triage = models.get_triage_for_protocol(db, pid)
        if not triage:
            return {"error": "no triage entry linked"}, 400
        if triage.get("status") == "abgeschlossen":
            return {"ok": True, "already_finished": True,
                    "triage_id": triage["id"]}
        ok = models.finish_triage_treatment(db, triage["id"])
        if not ok:
            return {"error": "could not finish (status: "
                    + triage.get("status", "?") + ")"}, 400
        db.commit()
        return {"ok": True, "triage_id": triage["id"]}

    # ----- Central first-aid (Notfallprotokoll) -----

    @app.route("/central/new", methods=["GET", "POST"])
    @login_required
    def central_new():
        """Interstitial: Patient identifizieren, bevor die SPA geöffnet wird.

        Akzeptiert beliebige Kombination aus Vorname/Nachname/Geburtsdatum
        (mind. eins). Bei genau 1 Treffer → Bestätigung. Bei 0 Treffern
        und vollem Datensatz → Bestätigung "neue Person". Bei mehreren
        Treffern oder unvollständiger Eingabe → Trefferliste zur Auswahl.
        """
        _require_event_create()
        src = request.form if request.method == "POST" else request.args
        prefill = {
            "vorname": (src.get("vorname") or "").strip(),
            "nachname": (src.get("nachname") or "").strip(),
            "geburtsdatum": (src.get("geburtsdatum") or "").strip(),
        }
        full_name = " ".join(
            p for p in (prefill["vorname"], prefill["nachname"]) if p
        )

        result = None  # one of: None, "single", "multiple", "new"
        match = None
        matches: list[dict] = []

        if request.method == "POST" and (
                full_name or prefill["geburtsdatum"]):
            db = models.get_db()
            rows = models.search_patients(
                db, name_query=full_name, geburtsdatum=prefill["geburtsdatum"]
            )
            for r in rows:
                counts = models.patient_protocol_counts(db, r["id"],
                                                        event_id=_current_event_id())
                last = models.patient_last_treatment(db, r["id"],
                                                     event_id=_current_event_id())
                matches.append({
                    "id": r["id"],
                    "name": r["name"],
                    "geburtsdatum": r["geburtsdatum"],
                    "stammnummer": r["stammnummer"] or "",
                    "decentral_count": counts["decentral"],
                    "central_count": counts["central"],
                    "last_treatment": last,
                })

            if len(matches) == 1:
                result = "single"
                match = matches[0]
            elif len(matches) > 1:
                result = "multiple"
            elif full_name and prefill["geburtsdatum"]:
                # Name + Geburtsdatum eindeutig → wirklich neue Person
                result = "new"
            else:
                # Nur ein Feld eingegeben, nichts gefunden — User soll
                # mehr Info eingeben statt direkt anlegen.
                result = "no_partial_match"

        return render_template("central_new.html", prefill=prefill,
                               result=result,
                               match=match,
                               matches=matches,
                               full_name=full_name,
                               is_zentral_only=current_user.is_zentral_only,
                               format_dt=models.format_dt)

    @app.route("/central")
    @login_required
    def central_index():
        """Serve the SPA for central first-aid protocols.

        The HTML is served as-is from a template file. It uses /api/central/*
        endpoints. We render it via Flask so the @login_required check kicks
        in and so we can pass the current user to a small wrapping banner.
        """
        _require_event_view()
        return render_template(
            "central_index.html",
            current_user_label=current_user.full_name or current_user.username,
        )

    @app.route("/central/<int:pid>")
    @login_required
    def central_detail(pid: int):
        """Server-gerenderte Detailansicht eines zentralen Berichts —
        analog zu /protocols/<id> für dezentrale Berichte."""
        db = models.get_db()
        rec = models.get_central_protocol(db, pid)
        if not rec:
            abort(404)
        _require_event_view(rec.get("event_id"))
        # Lesen ist für alle eingeloggten User erlaubt (auch zentral_writer
        # darf Vorbehandlungen sehen). Schreiben/Löschen prüft weiter unten.
        comments = models.list_central_comments(db, pid)
        # Geschwister-Berichte (beide Typen) für diesen Patienten anzeigen,
        # sofern wir eine Patienten-Verknüpfung haben.
        decentral_siblings = []
        central_siblings = []
        if rec.get("patient_id"):
            decentral_siblings = models.list_patient_protocols(
                db, rec["patient_id"], event_id=rec.get("event_id")
            )
            central_siblings = [
                r for r in models.list_central_protocols(
                    db, patient_id=rec["patient_id"],
                    event_id=rec.get("event_id")
                ) if r["id"] != pid
            ]
        triage = models.get_triage_for_protocol(db, pid)
        return render_template(
            "central_detail.html",
            protocol=rec,
            comments=comments,
            decentral_siblings=decentral_siblings,
            central_siblings=central_siblings,
            triage=triage,
            indicator_lookup=models.PRIOR_INDICATOR_BY_KEY,
            format_dt=models.format_dt,
        )

    @app.route("/central/<int:pid>/comments", methods=["POST"])
    @login_required
    def central_add_comment(pid: int):
        db = models.get_db()
        rec = models.get_central_protocol(db, pid)
        if not rec:
            abort(404)
        _require_event_view(rec.get("event_id"))
        # Kommentare darf jeder eingeloggte User schreiben (Diskussion
        # über Vorbehandlungen). Bearbeiten/Löschen des Berichts bleibt
        # weiter eigentumsbasiert.
        text = request.form.get("text", "").strip()
        if text:
            models.add_central_comment(db, pid, text, current_user.id)
            db.commit()
            flash("Kommentar hinzugefügt.", "success")
        return redirect(url_for("central_detail", pid=pid))

    @app.route("/central/<int:pid>/delete", methods=["POST"])
    @login_required
    def central_delete(pid: int):
        db = models.get_db()
        rec = models.get_central_protocol(db, pid)
        if not rec:
            abort(404)
        _require_event_view(rec.get("event_id"))
        if (current_user.is_zentral_only
                and rec.get("created_by") != current_user.id):
            abort(403)
        password = request.form.get("password", "")
        user_row = models.get_user_by_id(db, current_user.id)
        if not user_row or not models.verify_password(user_row, password):
            flash("Passwort falsch — Bericht wurde nicht gelöscht.", "error")
            return redirect(url_for("central_detail", pid=pid))
        label = rec["laufende_nr"] or f"#zEH{pid}"
        models.delete_central_protocol(db, pid)
        db.commit()
        flash(f"Bericht {label} gelöscht.", "success")
        return redirect(url_for("central_index")
                        if current_user.is_zentral_only
                        else url_for("index"))

    @app.route("/api/central/protokolle", methods=["GET", "POST"])
    @login_required
    def api_central_list_or_create():
        db = models.get_db()
        event_id = _require_event_view()
        if request.method == "GET":
            # zentral_writer: only their own protocols.
            rows = models.list_central_protocols(
                db,
                event_id=event_id,
                created_by=(current_user.id
                            if current_user.is_zentral_only else None),
            )
            return [
                {
                    "id": r["id"],
                    "vorname": (r["name_summary"] or "").split(" ", 1)[0]
                                if r["name_summary"] else "",
                    "nachname": (r["name_summary"] or "").split(" ", 1)[1]
                                 if r["name_summary"] and " " in r["name_summary"]
                                 else "",
                    "einsatznummer": r["einsatznummer"],
                    "datum": r["datum"],
                    "laufende_nr": r["laufende_nr"],
                    "global_id": r["global_id"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "patient_id": r["patient_id"],
                    "previous_decentral": _decentral_count_for_patient(
                        db, r["patient_id"], event_id=event_id),
                }
                for r in rows
            ]
        # POST
        data = request.get_json(silent=True) or {}
        if not models.user_can_create_in_event(
                db, current_user.id, event_id, current_user.is_admin):
            return {"error": "forbidden"}, 403
        new_id = models.create_central_protocol(db, data, current_user.id,
                                                event_id=event_id)
        # Falls die SPA mit ?triage_id=... aufgerufen wurde, das Protokoll
        # mit dem Triage-Eintrag verknüpfen.
        try:
            tid = int(request.args.get("triage_id") or 0)
        except (ValueError, TypeError):
            tid = 0
        if tid:
            models.link_triage_to_central_protocol(
                db, tid, new_id, started_by=current_user.id)
        # MANV-Karten-Verknüpfung
        try:
            mcid = int(request.args.get("manv_card_id") or 0)
        except (ValueError, TypeError):
            mcid = 0
        if mcid:
            models.manv_card_link_protocol(db, mcid, new_id)
        db.commit()
        return {"id": new_id}, 201

    @app.route("/api/central/protokolle/<int:pid>", methods=["GET", "PUT", "DELETE"])
    @login_required
    def api_central_one(pid: int):
        db = models.get_db()
        rec = models.get_central_protocol(db, pid)
        if not rec:
            return {"error": "not found"}, 404
        if not models.user_can_view_event(
                db, current_user.id, rec.get("event_id"), current_user.is_admin):
            return {"error": "forbidden"}, 403
        # Lesen ist für alle eingeloggten User offen (Vorbehandlungen
        # einsehen). Schreiben/Löschen prüft den Eigentümer für
        # zentral_writer weiter unten.
        if request.method == "GET":
            # Adresse/Telefon/Krankenkasse für Nicht-Admins entfernen — ein
            # Voll-User oder zentral_writer sieht die Felder im SPA-Formular
            # also leer — außer perm_view_contact ist gesetzt. Admin bekommt
            # die Werte unverändert.
            if not current_user.can_view_contact:
                rec = dict(rec)
                rec["data"] = models.strip_central_contact(rec.get("data") or {})
            return rec
        # PUT / DELETE: zentral_writer darf nur eigene Berichte ändern.
        if (current_user.is_zentral_only
                and rec.get("created_by") != current_user.id):
            return {"error": "forbidden"}, 403
        if request.method == "PUT":
            data = request.get_json(silent=True) or {}
            # Wenn der Speichernde keine Kontakt-Berechtigung hat, dürfen
            # die geschützten Felder nicht überschrieben werden — sie
            # waren beim Laden gestrippt und kommen entsprechend leer zurück.
            if not current_user.can_view_contact:
                data = models.merge_central_contact(data, rec.get("data") or {})
            ok = models.update_central_protocol(db, pid, data)
            db.commit()
            if not ok:
                return {"error": "not found"}, 404
            return {"id": pid}
        # DELETE — requires password verification.
        body = request.get_json(silent=True) or {}
        password = body.get("password") or request.args.get("password", "")
        user_row = models.get_user_by_id(db, current_user.id)
        if not user_row or not models.verify_password(user_row, password):
            return {"error": "password incorrect"}, 401
        ok = models.delete_central_protocol(db, pid)
        db.commit()
        if not ok:
            return {"error": "not found"}, 404
        return {"deleted": pid}

    @app.route("/api/central/protokolle/<int:pid>/pdf")
    @login_required
    def api_central_pdf(pid: int):
        # Notfallprotokoll-PDF: admin-only oder explizit per
        # perm_export_pdf freigeschaltet.
        if not current_user.can_export_pdf:
            abort(403)
        db = models.get_db()
        rec = models.get_central_protocol(db, pid)
        if not rec:
            abort(404)
        # Adresse / Telefon / Krankenkasse werden für User ohne
        # perm_view_contact aus dem PDF gestrippt.
        pdf_data = rec["data"]
        if not current_user.can_view_contact:
            pdf_data = models.strip_central_contact(pdf_data)
        # Patient-Medizinische Infos (Allergien, Medikamente, Notfallkontakt)
        # — landen nur dann im PDF, wenn der Exporter Adresse sehen darf
        # (gleiches Sensitivitäts-Niveau wie Telefon/Krankenkasse).
        medical_info = None
        if current_user.can_view_contact and rec.get("patient_id"):
            patient = models.get_patient(db, rec["patient_id"])
            if patient:
                medical_info = {
                    "has_allergies": patient["has_allergies"],
                    "allergies_text": patient["allergies_text"],
                    "has_medications": patient["has_medications"],
                    "medications_text": patient["medications_text"],
                    "emergency_contact_name": patient["emergency_contact_name"],
                    "emergency_contact_phone": patient["emergency_contact_phone"],
                    "emergency_contact_relation":
                        patient["emergency_contact_relation"],
                }
        try:
            pdf_bytes = render_central_pdf(
                pdf_data,
                exporter_label=(current_user.full_name
                                or current_user.username),
                medical_info=medical_info,
            )
        except FileNotFoundError as e:
            return {"error": str(e)}, 500
        name = rec["name_summary"] or "Protokoll"
        datum = (rec["data"].get("datum") or rec["updated_at"][:10] or "").replace(
            "/", "-"
        )
        if isinstance(datum, list):
            datum = datum[0] if datum else ""
        filename = f"Protokoll_{name}_{datum}.pdf".replace(" ", "_")
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=False,  # SPA opens in new tab → inline preview
            download_name=filename,
        )

    @app.route("/api/central/lookup")
    @login_required
    def api_central_lookup():
        """Patientenstamm-Suche für SPA und /central/new.

        Akzeptiert beliebige Kombination von vorname/nachname (zu name
        kombiniert) und/oder geburtsdatum. Liefert eine Liste aller
        Treffer (max. 20). Wenn >1 Treffer → der Aufrufer muss
        disambiguieren (Name eingeben oder Patient auswählen).
        """
        db = models.get_db()
        vorname = (request.args.get("vorname") or "").strip()
        nachname = (request.args.get("nachname") or "").strip()
        # `name` kann auch direkt als ganzer Suchbegriff übergeben werden.
        name_query = (
            (request.args.get("name") or "").strip()
            or " ".join(p for p in (vorname, nachname) if p)
        )
        geburtsdatum = (request.args.get("geburtsdatum") or "").strip()
        if not name_query and not geburtsdatum:
            return {"matches": []}

        rows = models.search_patients(
            db, name_query=name_query, geburtsdatum=geburtsdatum
        )
        matches = []
        for r in rows:
            counts = models.patient_protocol_counts(db, r["id"],
                                                    event_id=_current_event_id())
            last = models.patient_last_treatment(db, r["id"],
                                                 event_id=_current_event_id())
            matches.append({
                # patient_id wird auch an zentral_writer zurückgegeben, damit
                # das Notfall-Widget Indikatoren + Entschlüsseln-Button zeigen
                # kann. Der Akte-Link in der UI ist separat per Rolle gesperrt
                # und /patients/<id> bleibt durch decentral_view_required
                # geschützt (HTTP 403 für zentral_writer).
                "patient_id": r["id"],
                "name": r["name"],
                "geburtsdatum": r["geburtsdatum"],
                "stammnummer": r["stammnummer"] or "",
                "previous_decentral": counts["decentral"],
                "previous_central": counts["central"],
                "last_treatment": last,
            })
        return {"matches": matches}

    @app.route("/api/central/patient-history")
    @login_required
    def api_central_patient_history():
        """Liste aller Berichte (dezentral + zentral) eines Patienten —
        Datenquelle für die Vorbehandlungs-Sidebar. Für alle eingeloggten
        User erlaubt; sensitive Patient-Felder sind gestrippt (für
        Admin-Kram gibt es /api/patient/<id>/sensitive)."""
        db = models.get_db()
        try:
            pid = int(request.args.get("patient_id") or 0)
        except ValueError:
            return {"error": "invalid patient_id"}, 400
        if not pid:
            return {"error": "patient_id required"}, 400
        patient = models.get_patient(db, pid)
        if not patient:
            return {"error": "not found"}, 404
        return {
            "patient": {
                "id": patient["id"],
                "name": patient["name"],
                "geburtsdatum": patient["geburtsdatum"],
                "stammnummer": patient["stammnummer"],
            },
            "history": models.patient_history(db, pid, event_id=_current_event_id()),
        }

    @app.route("/api/protocol-summary")
    @login_required
    def api_protocol_summary():
        """Kompakte Zusammenfassung eines Berichts für das Vorbehandlungs-
        Popup im SPA. source ∈ {central, decentral}."""
        source = (request.args.get("source") or "").strip()
        try:
            pid = int(request.args.get("id") or 0)
        except ValueError:
            return {"error": "invalid id"}, 400
        if source not in ("central", "decentral") or not pid:
            return {"error": "source/id required"}, 400
        if source == "central":
            rec = models.get_central_protocol(models.get_db(), pid)
            if not rec:
                return {"error": "not found"}, 404
            if not models.user_can_view_event(
                    models.get_db(), current_user.id, rec.get("event_id"),
                    current_user.is_admin):
                return {"error": "forbidden"}, 403
        else:
            rec = models.get_protocol(models.get_db(), pid)
            if not rec:
                return {"error": "not found"}, 404
            if not models.user_can_view_event(
                    models.get_db(), current_user.id, rec["event_id"],
                    current_user.is_admin):
                return {"error": "forbidden"}, 403
        summary = models.protocol_summary(models.get_db(), source, pid)
        if not summary:
            return {"error": "not found"}, 404
        return summary

    # ----- Medikamentenplan (admin-only) -----

    @app.route("/medications")
    @login_required
    def medications_index():
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        patients = models.list_patients_with_medications(db)
        all_patients = models.list_patients(db, event_id=None)
        return render_template(
            "medications_index.html",
            patients_with_meds=patients,
            all_patients=all_patients,
            format_dt=models.format_dt,
        )

    @app.route("/medications/patient/<int:patient_id>")
    @login_required
    def medications_patient(patient_id: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        patient = models.get_patient(db, patient_id)
        if not patient:
            abort(404)
        meds = models.list_medications_for_patient(db, patient_id,
                                                    include_inactive=True)
        # 7-Tage-Fenster ab today (oder ab `start` querystring)
        from datetime import date, timedelta
        try:
            start = date.fromisoformat(
                request.args.get("start") or date.today().isoformat()
            )
        except ValueError:
            start = date.today()
        days = [start + timedelta(days=i) for i in range(7)]
        admins = models.list_administrations_for_patient(
            db, patient_id,
            date_from=days[0].isoformat(),
            date_to=days[-1].isoformat(),
        )
        # Map zur schnellen Lookup: (medication_id, day, slot) -> row
        admin_map = {(a["medication_id"], a["day_date"], a["slot"]): a
                     for a in admins}
        return render_template(
            "medications_patient.html",
            patient=patient,
            meds=meds,
            days=days,
            slots=models.MEDICATION_SLOTS,
            slot_labels=models.MEDICATION_SLOT_LABELS,
            admin_map=admin_map,
            start=start,
            format_dt=models.format_dt,
        )

    @app.route("/medications/patient/<int:patient_id>/add",
               methods=["POST"])
    @login_required
    def medications_add(patient_id: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        if not models.get_patient(db, patient_id):
            abort(404)
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Name des Medikaments fehlt.", "error")
            return redirect(url_for("medications_patient",
                                     patient_id=patient_id))
        models.create_medication(
            db,
            patient_id=patient_id,
            name=name,
            dosage=request.form.get("dosage"),
            morgens=bool(request.form.get("morgens")),
            mittags=bool(request.form.get("mittags")),
            abends=bool(request.form.get("abends")),
            nachts=bool(request.form.get("nachts")),
            bei_bedarf=bool(request.form.get("bei_bedarf")),
            lagerung=request.form.get("lagerung"),
            notes=request.form.get("notes"),
            start_date=(request.form.get("start_date") or "").strip() or None,
            end_date=(request.form.get("end_date") or "").strip() or None,
            created_by=current_user.id,
        )
        db.commit()
        flash(f"Medikament „{name}“ hinzugefügt.", "success")
        return redirect(url_for("medications_patient",
                                 patient_id=patient_id))

    @app.route("/medications/<int:mid>/delete", methods=["POST"])
    @login_required
    def medications_delete(mid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        med = models.get_medication(db, mid)
        if not med:
            abort(404)
        patient_id = med["patient_id"]
        models.delete_medication(db, mid)
        db.commit()
        flash(f"Medikament „{med['name']}“ entfernt.", "success")
        return redirect(url_for("medications_patient",
                                 patient_id=patient_id))

    @app.route("/medications/<int:mid>/toggle-active", methods=["POST"])
    @login_required
    def medications_toggle_active(mid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        med = models.get_medication(db, mid)
        if not med:
            abort(404)
        models.set_medication_active(db, mid, not med["active"])
        db.commit()
        return redirect(url_for("medications_patient",
                                 patient_id=med["patient_id"]))

    @app.route("/medications/<int:mid>/administer", methods=["POST"])
    @login_required
    def medications_administer(mid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        med = models.get_medication(db, mid)
        if not med:
            abort(404)
        day = (request.form.get("day") or "").strip()
        slot = (request.form.get("slot") or "").strip()
        action = (request.form.get("action") or "give").strip()
        if slot not in models.MEDICATION_SLOTS or not day:
            return {"error": "invalid slot or day"}, 400
        if action == "undo":
            models.remove_medication_administration(
                db, medication_id=mid, day_date=day, slot=slot)
        else:
            models.record_medication_administration(
                db, medication_id=mid, day_date=day, slot=slot,
                administered_by=current_user.id)
        db.commit()
        return {"ok": True}

    @app.route("/medications/patient/<int:patient_id>/plan.pdf")
    @login_required
    def medications_plan_pdf(patient_id: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        patient = models.get_patient(db, patient_id)
        if not patient:
            abort(404)
        from datetime import date, timedelta
        try:
            start = date.fromisoformat(
                request.args.get("start") or date.today().isoformat()
            )
        except ValueError:
            start = date.today()
        days = [start + timedelta(days=i) for i in range(7)]
        meds = models.list_medications_for_patient(db, patient_id,
                                                    include_inactive=False)
        admins = models.list_administrations_for_patient(
            db, patient_id,
            date_from=days[0].isoformat(),
            date_to=days[-1].isoformat(),
        )
        admin_map = {(a["medication_id"], a["day_date"], a["slot"]):
                     dict(a) for a in admins}
        from medication_plan_pdf import render_medication_plan_pdf
        pdf_bytes = render_medication_plan_pdf(
            patient=dict(patient),
            medications=[dict(m) for m in meds],
            days=days,
            admin_map=admin_map,
            exporter_label=(current_user.full_name
                            or current_user.username),
            patient_extra={
                "allergies_text": patient["allergies_text"],
                "emergency_contact_name":
                    patient["emergency_contact_name"],
                "emergency_contact_phone":
                    patient["emergency_contact_phone"],
                "emergency_contact_relation":
                    patient["emergency_contact_relation"],
            },
        )
        name_safe = (patient["name"] or "Patient").replace(" ", "_")
        filename = f"Medikamentenplan_{name_safe}_{start.isoformat()}.pdf"
        return Response(
            pdf_bytes, mimetype="application/pdf",
            headers={"Content-Disposition":
                     f'attachment; filename="{filename}"'},
        )

    @app.route("/medications/blanko.pdf")
    @login_required
    def medications_blanko_pdf():
        """Blanko-Plan ohne Patientendaten — eine Seite, eine Person.
        Querystring `name` und `geburtsdatum` werden optional übernommen,
        damit man dasselbe Endpoint auch mit vorgefülltem Header nutzen kann.
        """
        if not current_user.is_admin:
            abort(403)
        from datetime import date, timedelta
        try:
            start = date.fromisoformat(
                request.args.get("start") or date.today().isoformat()
            )
        except ValueError:
            start = date.today()
        days = [start + timedelta(days=i) for i in range(7)]
        from medication_plan_pdf import render_medication_plan_pdf
        pdf_bytes = render_medication_plan_pdf(
            patient={
                "name": (request.args.get("name") or "").strip(),
                "geburtsdatum":
                    (request.args.get("geburtsdatum") or "").strip(),
                "stammnummer": "",
            },
            medications=[],
            days=days,
            admin_map={},
            exporter_label=(current_user.full_name
                            or current_user.username),
            blanko=True,
        )
        return Response(
            pdf_bytes, mimetype="application/pdf",
            headers={"Content-Disposition":
                     f'attachment; filename="Medikamentenplan_Blanko_{start.isoformat()}.pdf"'},
        )

    # ----- MANV (Massenanfall von Verletzten) -----
    # Admin: anlegen/verwalten/drucken. Alle eingeloggten User: scannen + sichten.

    @app.route("/manv")
    @login_required
    def manv_index():
        db = models.get_db()
        if current_user.is_admin:
            # Admin sieht alle Events (geplant + alarmiert + abgeschlossen)
            events = models.list_manv_events(db)
            alarmiertes = models.get_alarmiertes_manv_event(db)
            return render_template(
                "manv_index.html", events=events,
                alarmiertes=alarmiertes,
                is_admin_view=True,
                format_dt=models.format_dt,
            )
        # Nicht-Admin: sieht nur das aktuell alarmierte Event (oder Empty-State)
        alarmiertes = models.get_alarmiertes_manv_event(db)
        return render_template(
            "manv_index.html",
            events=[alarmiertes] if alarmiertes else [],
            alarmiertes=alarmiertes,
            is_admin_view=False,
            format_dt=models.format_dt,
        )

    @app.route("/manv/new", methods=["POST"])
    @login_required
    def manv_new():
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        name = (request.form.get("name") or "").strip()
        prefix = (request.form.get("card_prefix") or "").strip() or "MANV"
        if not name:
            flash("Vorfall-Name ist Pflicht.", "error")
            return redirect(url_for("manv_index"))
        eid = models.create_manv_event(
            db, name=name, card_prefix=prefix,
            notes=request.form.get("notes"),
            situation=request.form.get("situation"),
            einsatzort=request.form.get("einsatzort"),
            lage_bild=request.form.get("lage_bild"),
            created_by=current_user.id,
        )
        db.commit()
        flash(f"MANV „{name}“ angelegt. Noch NICHT alarmiert — User "
              f"sehen das Event erst, wenn du es alarmierst.", "success")
        return redirect(url_for("manv_event", eid=eid))

    @app.route("/manv/event/<int:eid>/edit", methods=["POST"])
    @login_required
    def manv_event_edit(eid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        if not models.get_manv_event(db, eid):
            abort(404)
        fields = {f: request.form.get(f) for f in models.MANV_EVENT_EDIT_FIELDS}
        models.update_manv_event(db, eid, fields)
        db.commit()
        flash("Vorfall-Daten aktualisiert.", "success")
        return redirect(url_for("manv_event", eid=eid))

    @app.route("/manv/event/<int:eid>/alarm", methods=["POST"])
    @login_required
    def manv_event_alarm(eid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        ev = models.get_manv_event(db, eid)
        if not ev:
            abort(404)
        if models.alarm_manv_event(db, eid, by_user_id=current_user.id):
            db.commit()
            flash(f"🚨 MANV „{ev['name']}“ ist jetzt ALARMIERT. "
                  f"Alle User sehen das Event und können scannen.", "success")
        return redirect(url_for("manv_event", eid=eid))

    @app.route("/manv/event/<int:eid>/dealarm", methods=["POST"])
    @login_required
    def manv_event_dealarm(eid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        ev = models.get_manv_event(db, eid)
        if not ev:
            abort(404)
        if models.dealarm_manv_event(db, eid):
            db.commit()
            flash(f"Alarmierung für „{ev['name']}“ aufgehoben — "
                  f"Event ist wieder geplant (für User unsichtbar).",
                  "success")
        return redirect(url_for("manv_event", eid=eid))

    @app.route("/manv/event/<int:eid>")
    @login_required
    def manv_event(eid: int):
        db = models.get_db()
        event = models.get_manv_event(db, eid)
        if not event:
            abort(404)
        # Nicht-Admins dürfen nur ALARMIERTE Events sehen — sonst Redirect
        if not current_user.is_admin and not event["is_alarmiert"]:
            flash("Aktuell ist kein MANV alarmiert.", "info")
            return redirect(url_for("manv_index"))
        cards = models.list_manv_cards(db, eid)
        stats = models.manv_event_stats(db, eid)
        return render_template(
            "manv_event.html",
            event=event,
            cards=cards,
            stats=stats,
            kategorien=models.MANV_KATEGORIEN,
            kategorie_label=models.MANV_KATEGORIE_LABEL,
            kategorie_color=models.MANV_KATEGORIE_COLOR,
            format_dt=models.format_dt,
        )

    # ----- Sticker-Pool (vor dem Einsatz vorbereiten) -----

    @app.route("/manv/pool")
    @login_required
    def manv_pool():
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        stats = models.pool_stats(db)
        pool_cards = models.list_pool_cards(db, limit=2000)
        return render_template(
            "manv_pool.html",
            stats=stats,
            pool_cards=pool_cards,
            format_dt=models.format_dt,
        )

    @app.route("/manv/pool/create", methods=["POST"])
    @login_required
    def manv_pool_create():
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        try:
            count = int(request.form.get("count") or 0)
        except ValueError:
            count = 0
        count = max(1, min(count, 1000))
        cards = models.create_sticker_pool(
            db, count=count, created_by=current_user.id)
        db.commit()
        if cards:
            first = cards[0]["card_no"]
            last = cards[-1]["card_no"]
            flash(f"{len(cards)} neue Sticker erzeugt "
                  f"({first} – {last}). Direkt drucken über den Button unten.",
                  "success")
        return redirect(url_for("manv_pool"))

    @app.route("/manv/pool/print.pdf")
    @login_required
    def manv_pool_print():
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        try:
            id_from = int(request.args.get("from") or 0)
            id_to = int(request.args.get("to") or 0)
        except ValueError:
            id_from = id_to = 0
        if id_from and id_to:
            cards = db.execute(
                "SELECT * FROM manv_cards WHERE id BETWEEN ? AND ? "
                "AND manv_event_id IS NULL ORDER BY id",
                (id_from, id_to)).fetchall()
        else:
            cards = models.list_pool_cards(db, limit=2000)
        if not cards:
            flash("Keine Pool-Karten zum Drucken.", "error")
            return redirect(url_for("manv_pool"))
        try:
            cols = int(request.args.get("cols") or 2)
            rows = int(request.args.get("rows") or 2)
        except ValueError:
            cols, rows = 2, 2
        scheme = "https" if request.is_secure else "http"
        base_url = f"{scheme}://{request.host}"
        style = (request.args.get("style") or "sticker").strip()
        if style == "mini":
            # Kleine reine QR-Sticker (3×8 default)
            from manv_cards_pdf import render_qr_stickers_pdf
            pdf_bytes = render_qr_stickers_pdf(
                event={"name": "Sticker-Vorrat", "card_prefix": "EH"},
                cards=[dict(c) for c in cards],
                base_url=base_url, cols=cols or 3, rows=rows or 8,
                include_event_name=False)
            fname = "MANV_QR-Mini-Sticker.pdf"
        elif style == "full":
            # Komplette A5-Karten, vorne+hinten (volle DRK-Karte ohne
            # echte DRK-Vorlage). Pro Karte 2 PDF-Seiten.
            from manv_cards_pdf import render_manv_cards_pdf
            pdf_bytes = render_manv_cards_pdf(
                event={"name": "Sticker-Vorrat (Blanko-Karten)",
                       "card_prefix": "EH", "id": 0},
                cards=[dict(c) for c in cards],
                base_url=base_url)
            fname = "MANV_Blanko-Karten_komplett.pdf"
        else:
            # Default: A6-Anhängekarten-Sticker (4 pro A4)
            from manv_cards_pdf import render_anhaengekarte_stickers_pdf
            pdf_bytes = render_anhaengekarte_stickers_pdf(
                event={"name": "Sticker-Vorrat", "card_prefix": "EH"},
                cards=[dict(c) for c in cards],
                base_url=base_url, cols=cols, rows=rows)
            fname = "MANV_Anhaengekarte_Sticker.pdf"
        return Response(
            pdf_bytes, mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.route("/manv/event/<int:eid>/allocate", methods=["POST"])
    @login_required
    def manv_allocate(eid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        event = models.get_manv_event(db, eid)
        if not event:
            abort(404)
        try:
            count = int(request.form.get("count") or 0)
        except ValueError:
            count = 0
        count = max(1, min(count, 500))
        models.allocate_manv_cards(db, event_id=eid, count=count)
        db.commit()
        flash(f"{count} weitere Blanko-Karten erzeugt.", "success")
        return redirect(url_for("manv_event", eid=eid))

    @app.route("/manv/event/<int:eid>/close", methods=["POST"])
    @login_required
    def manv_event_close(eid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        action = (request.form.get("action") or "close").strip()
        if action == "reopen":
            models.reopen_manv_event(db, eid)
            flash("MANV-Vorfall wieder geöffnet.", "success")
        elif action == "delete":
            models.delete_manv_event(db, eid)
            db.commit()
            flash("MANV-Vorfall gelöscht (mit allen Karten).", "success")
            return redirect(url_for("manv_index"))
        else:
            models.close_manv_event(db, eid)
            flash("MANV-Vorfall abgeschlossen.", "success")
        db.commit()
        return redirect(url_for("manv_event", eid=eid))

    @app.route("/manv/event/<int:eid>/print.pdf")
    @login_required
    def manv_print_pdf(eid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        event = models.get_manv_event(db, eid)
        if not event:
            abort(404)
        # Parameter: ?from=&to= ODER ?status=blank (alle blanken)
        try:
            id_from = int(request.args.get("from") or 0)
            id_to = int(request.args.get("to") or 0)
        except ValueError:
            id_from = id_to = 0
        if request.args.get("status") == "blank":
            cards = models.list_manv_cards(db, eid, status="blank")
        elif id_from and id_to:
            cards = [c for c in models.list_manv_cards(db, eid)
                     if id_from <= c["id"] <= id_to]
        else:
            cards = models.list_manv_cards(db, eid)
        # Default-Limit: nicht mehr als 200 auf einmal
        cards = cards[:200]
        if not cards:
            flash("Keine Karten zum Drucken gefunden.", "error")
            return redirect(url_for("manv_event", eid=eid))
        from manv_cards_pdf import render_manv_cards_pdf
        scheme = "https" if request.is_secure else "http"
        base_url = f"{scheme}://{request.host}"
        pdf_bytes = render_manv_cards_pdf(
            event=dict(event),
            cards=[dict(c) for c in cards],
            base_url=base_url,
        )
        fname = f"MANV_{event['card_prefix']}_Karten_{cards[0]['card_no']}-{cards[-1]['card_no']}.pdf"
        return Response(
            pdf_bytes, mimetype="application/pdf",
            headers={"Content-Disposition":
                     f'attachment; filename="{fname}"'},
        )

    @app.route("/manv/event/<int:eid>/stickers.pdf")
    @login_required
    def manv_stickers_pdf(eid: int):
        """QR-Aufkleber-Bogen — wird auf Etiketten-Papier gedruckt und
        auf die echten DRK-Anhängekarten geklebt."""
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        event = models.get_manv_event(db, eid)
        if not event:
            abort(404)
        try:
            id_from = int(request.args.get("from") or 0)
            id_to = int(request.args.get("to") or 0)
        except ValueError:
            id_from = id_to = 0
        if request.args.get("status") == "blank":
            cards = models.list_manv_cards(db, eid, status="blank")
        elif id_from and id_to:
            cards = [c for c in models.list_manv_cards(db, eid)
                     if id_from <= c["id"] <= id_to]
        else:
            cards = models.list_manv_cards(db, eid)
        cards = cards[:200]
        if not cards:
            flash("Keine Karten zum Drucken gefunden.", "error")
            return redirect(url_for("manv_event", eid=eid))
        try:
            cols = int(request.args.get("cols") or 2)
            rows = int(request.args.get("rows") or 2)
        except ValueError:
            cols, rows = 2, 2
        scheme = "https" if request.is_secure else "http"
        base_url = f"{scheme}://{request.host}"
        if request.args.get("style") == "mini":
            from manv_cards_pdf import render_qr_stickers_pdf
            pdf_bytes = render_qr_stickers_pdf(
                event=dict(event),
                cards=[dict(c) for c in cards],
                base_url=base_url, cols=cols or 3, rows=rows or 8)
        else:
            from manv_cards_pdf import render_anhaengekarte_stickers_pdf
            pdf_bytes = render_anhaengekarte_stickers_pdf(
                event=dict(event),
                cards=[dict(c) for c in cards],
                base_url=base_url, cols=cols, rows=rows)
        fname = f"MANV_{event['card_prefix']}_Sticker.pdf"
        return Response(
            pdf_bytes, mimetype="application/pdf",
            headers={"Content-Disposition":
                     f'attachment; filename="{fname}"'},
        )

    @app.route("/manv/event/<int:eid>/uebersicht.pdf")
    @login_required
    def manv_uebersicht_pdf(eid: int):
        db = models.get_db()
        event = models.get_manv_event(db, eid)
        if not event:
            abort(404)
        cards = models.list_manv_cards(db, eid)
        # Optional: nur ab status != 'blank' für ein „echtes" Protokoll
        if request.args.get("only_used") == "1":
            cards = [c for c in cards if c["status"] != "blank"]
        event_d = dict(event)
        from manv_cards_pdf import render_uebersichtsprotokoll_pdf
        pdf_bytes = render_uebersichtsprotokoll_pdf(
            event=event_d,
            cards=[dict(c) for c in cards],
        )
        fname = (f"MANV_{event_d['card_prefix']}_Uebersichtsprotokoll_"
                 f"{(event_d.get('started_at') or '')[:10]}.pdf")
        return Response(
            pdf_bytes, mimetype="application/pdf",
            headers={"Content-Disposition":
                     f'attachment; filename="{fname}"'},
        )

    @app.route("/manv/scan/<token>", methods=["GET", "POST"])
    @login_required
    def manv_scan(token: str):
        db = models.get_db()
        card = models.get_manv_card_by_token(db, token)
        if not card:
            flash("Karte nicht gefunden — QR-Code prüfen.", "error")
            return redirect(url_for("manv_index"))
        # Pool-Karte? → Confirmation-Seite zur Event-Zuordnung
        if card["manv_event_id"] is None:
            active = models.get_active_manv_event(db)
            if request.method == "POST":
                # Helfer hat bestätigt → Karte dem (gewählten/aktiven) Event zuordnen
                try:
                    chosen_eid = int(request.form.get("event_id") or 0)
                except ValueError:
                    chosen_eid = 0
                if not chosen_eid and active:
                    chosen_eid = active["id"]
                if not chosen_eid:
                    flash("Kein aktives MANV — bitte erst eines anlegen.",
                          "error")
                    return redirect(url_for("manv_index"))
                if models.claim_pool_card(
                        db, card_id=card["id"], event_id=chosen_eid):
                    db.commit()
                    flash(f"Karte {card['card_no']} dem MANV zugewiesen.",
                          "success")
                return redirect(url_for("manv_card", cid=card["id"]))
            # GET: zeige Confirmation-Seite
            return render_template(
                "manv_scan_claim.html",
                card=card, active=active,
                all_active=models.list_manv_events(db),
            )
        # Bereits zugewiesen → direkt zum Detail
        return redirect(url_for("manv_card", cid=card["id"]))

    @app.route("/manv/card/<int:cid>", methods=["GET", "POST"])
    @login_required
    def manv_card(cid: int):
        db = models.get_db()
        card = models.get_manv_card(db, cid)
        if not card:
            abort(404)
        event = models.get_manv_event(db, card["manv_event_id"])
        if request.method == "POST":
            fields = {}
            for f in models.MANV_CARD_EDIT_FIELDS:
                if f.startswith(("diag_", "th_", "transport_mit_arzt",
                                 "transport_isoliert")):
                    fields[f] = 1 if request.form.get(f) else 0
                elif f == "alter_jahre":
                    raw = (request.form.get(f) or "").strip()
                    fields[f] = int(raw) if raw.isdigit() else None
                else:
                    raw = (request.form.get(f) or "").strip()
                    fields[f] = raw or None
            models.update_manv_card(db, cid, fields)
            # Wenn neue Sichtung im POST mitgeschickt
            new_sichtung = (request.form.get("new_sichtung") or "").strip()
            if new_sichtung in models.MANV_KATEGORIEN:
                models.manv_card_add_sichtung(
                    db, cid, kategorie=new_sichtung,
                    sichter_name=(current_user.full_name
                                  or current_user.username))
            db.commit()
            flash("Karte aktualisiert.", "success")
            return redirect(url_for("manv_card", cid=cid))
        try:
            sichtungen = json.loads(card["sichtungen_json"] or "[]")
        except Exception:
            sichtungen = []
        return render_template(
            "manv_card.html",
            card=card, event=event, sichtungen=sichtungen,
            kategorien=models.MANV_KATEGORIEN,
            kategorie_label=models.MANV_KATEGORIE_LABEL,
            kategorie_color=models.MANV_KATEGORIE_COLOR,
            status_values=models.MANV_STATUS_VALUES,
            format_dt=models.format_dt,
        )

    @app.route("/manv/card/<int:cid>/status", methods=["POST"])
    @login_required
    def manv_card_status(cid: int):
        db = models.get_db()
        card = models.get_manv_card(db, cid)
        if not card:
            abort(404)
        new_status = (request.form.get("status") or "").strip()
        if new_status in models.MANV_STATUS_VALUES:
            models.manv_card_set_status(db, cid, new_status)
            db.commit()
            flash(f"Karten-Status: {new_status}", "success")
        return redirect(url_for("manv_card", cid=cid))

    @app.route("/manv/card/<int:cid>/promote", methods=["POST"])
    @login_required
    def manv_card_promote(cid: int):
        """Übernimmt die Karten-Personalia in einen neuen zentralen
        Bericht und verlinkt beide. Anschließend Redirect zur SPA mit
        Pre-fill."""
        db = models.get_db()
        card = models.get_manv_card(db, cid)
        if not card:
            abort(404)
        # Personalia in Patientendatensatz übernehmen (falls noch nicht)
        if not card["patient_id"]:
            full_name = " ".join(
                x for x in (card["vorname"], card["name"]) if x
            ).strip()
            if full_name and card["geburtsdatum"]:
                pid = models.upsert_patient(
                    db, full_name, card["geburtsdatum"], None)
                models.manv_card_link_patient(db, cid, pid)
                db.commit()
        # Zur SPA mit Pre-fill — der Helfer speichert dort, das Backend
        # erkennt manv_card_id im Querystring und verlinkt sie.
        return redirect(url_for(
            "central_index",
            vorname=card["vorname"] or "",
            nachname=card["name"] or "",
            geburtsdatum=card["geburtsdatum"] or "",
            manv_card_id=cid,
        ))

    # ----- Einsatzbefehle + Einsatztagebuch (admin-only) -----

    @app.route("/einsatzbefehle")
    @login_required
    def einsatzbefehle_index():
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        # Pro aktuellem Event filtern, falls vorhanden
        active_event_id = _current_event_id()
        befehle = models.list_einsatzbefehle(db, event_id=active_event_id)
        return render_template(
            "einsatzbefehle_index.html",
            befehle=befehle,
            current_event_id=active_event_id,
            format_dt=models.format_dt,
        )

    @app.route("/einsatzbefehle/new", methods=["POST"])
    @login_required
    def einsatzbefehle_new():
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        fields = {f: request.form.get(f)
                   for f in models.EINSATZBEFEHL_FIELDS}
        eid, uid = models.create_einsatzbefehl(
            db,
            event_id=_current_event_id(),
            created_by=current_user.id,
            **fields,
        )
        db.commit()
        flash(f"Einsatzbefehl angelegt — ID {uid}.", "success")
        return redirect(url_for("einsatzbefehl_detail", eid=eid))

    @app.route("/einsatzbefehle/<int:eid>", methods=["GET", "POST"])
    @login_required
    def einsatzbefehl_detail(eid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        befehl = models.get_einsatzbefehl(db, eid)
        if not befehl:
            abort(404)
        if request.method == "POST":
            fields = {f: request.form.get(f)
                       for f in models.EINSATZBEFEHL_FIELDS}
            models.update_einsatzbefehl(db, eid, fields)
            db.commit()
            flash("Einsatzbefehl aktualisiert.", "success")
            return redirect(url_for("einsatzbefehl_detail", eid=eid))
        tagebuecher = models.list_einsatztagebuch_for_befehl(db, eid)
        return render_template(
            "einsatzbefehl_edit.html",
            befehl=befehl,
            tagebuecher=tagebuecher,
            format_dt=models.format_dt,
        )

    @app.route("/einsatzbefehle/<int:eid>/delete", methods=["POST"])
    @login_required
    def einsatzbefehl_delete(eid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        befehl = models.get_einsatzbefehl(db, eid)
        if not befehl:
            abort(404)
        models.delete_einsatzbefehl(db, eid)
        db.commit()
        flash(f"Einsatzbefehl {befehl['eindeutige_id']} gelöscht.", "success")
        return redirect(url_for("einsatzbefehle_index"))

    @app.route("/einsatzbefehle/<int:eid>/pdf")
    @login_required
    def einsatzbefehl_pdf(eid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        befehl = models.get_einsatzbefehl(db, eid)
        if not befehl:
            abort(404)
        from einsatzbefehl_pdf import render_einsatzbefehl_pdf
        pdf_bytes = render_einsatzbefehl_pdf(
            befehl=dict(befehl),
            exporter_label=(current_user.full_name or current_user.username),
        )
        fname = f"Einsatzbefehl_{befehl['eindeutige_id']}.pdf"
        return Response(
            pdf_bytes, mimetype="application/pdf",
            headers={"Content-Disposition":
                     f'attachment; filename="{fname}"'},
        )

    # --- Einsatztagebuch ---

    @app.route("/einsatzbefehle/<int:eid>/tagebuch/new", methods=["POST"])
    @login_required
    def einsatztagebuch_new(eid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        if not models.get_einsatzbefehl(db, eid):
            abort(404)
        tid = models.create_einsatztagebuch(
            db, einsatzbefehl_id=eid,
            einrichtung_einheit=request.form.get("einrichtung_einheit"),
            einsatz_anlass=request.form.get("einsatz_anlass"),
            created_by=current_user.id,
        )
        db.commit()
        flash("Einsatztagebuch angelegt.", "success")
        return redirect(url_for("einsatztagebuch_detail", tid=tid))

    @app.route("/einsatztagebuch/<int:tid>", methods=["GET", "POST"])
    @login_required
    def einsatztagebuch_detail(tid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        tagebuch = models.get_einsatztagebuch(db, tid)
        if not tagebuch:
            abort(404)
        if request.method == "POST":
            models.update_einsatztagebuch(
                db, tid,
                einrichtung_einheit=request.form.get("einrichtung_einheit"),
                einsatz_anlass=request.form.get("einsatz_anlass"),
            )
            db.commit()
            flash("Tagebuch-Kopfdaten aktualisiert.", "success")
            return redirect(url_for("einsatztagebuch_detail", tid=tid))
        eintraege = models.list_tagebuch_eintraege(db, tid)
        return render_template(
            "einsatztagebuch_detail.html",
            tagebuch=tagebuch,
            eintraege=eintraege,
            format_dt=models.format_dt,
        )

    @app.route("/einsatztagebuch/<int:tid>/eintrag", methods=["POST"])
    @login_required
    def einsatztagebuch_add_eintrag(tid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        if not models.get_einsatztagebuch(db, tid):
            abort(404)
        darstellung = (request.form.get("darstellung") or "").strip()
        if not darstellung:
            flash("Darstellung ist Pflicht.", "error")
            return redirect(url_for("einsatztagebuch_detail", tid=tid))
        models.add_tagebuch_eintrag(
            db, tagebuch_id=tid,
            ea=request.form.get("ea") or "",
            taktische_zeit=request.form.get("taktische_zeit"),
            darstellung=darstellung,
            vollzug=request.form.get("vollzug"),
            anlage=request.form.get("anlage"),
            created_by=current_user.id,
        )
        db.commit()
        return redirect(url_for("einsatztagebuch_detail", tid=tid))

    @app.route("/einsatztagebuch/<int:tid>/eintrag/<int:eintrag_id>/delete",
               methods=["POST"])
    @login_required
    def einsatztagebuch_delete_eintrag(tid: int, eintrag_id: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        models.delete_tagebuch_eintrag(db, eintrag_id)
        db.commit()
        return redirect(url_for("einsatztagebuch_detail", tid=tid))

    @app.route("/einsatztagebuch/<int:tid>/delete", methods=["POST"])
    @login_required
    def einsatztagebuch_delete(tid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        tagebuch = models.get_einsatztagebuch(db, tid)
        if not tagebuch:
            abort(404)
        eb_id = tagebuch["einsatzbefehl_id"]
        models.delete_einsatztagebuch(db, tid)
        db.commit()
        flash("Einsatztagebuch gelöscht.", "success")
        return redirect(url_for("einsatzbefehl_detail", eid=eb_id))

    @app.route("/einsatztagebuch/<int:tid>/pdf")
    @login_required
    def einsatztagebuch_pdf(tid: int):
        if not current_user.is_admin:
            abort(403)
        db = models.get_db()
        tagebuch = models.get_einsatztagebuch(db, tid)
        if not tagebuch:
            abort(404)
        befehl = models.get_einsatzbefehl(db, tagebuch["einsatzbefehl_id"])
        eintraege = models.list_tagebuch_eintraege(db, tid)
        from einsatzbefehl_pdf import render_einsatztagebuch_pdf
        pdf_bytes = render_einsatztagebuch_pdf(
            tagebuch=dict(tagebuch),
            befehl=dict(befehl) if befehl else {},
            eintraege=[dict(e) for e in eintraege],
            exporter_label=(current_user.full_name or current_user.username),
        )
        fname = f"Einsatztagebuch_{befehl['eindeutige_id'] if befehl else tid}.pdf"
        return Response(
            pdf_bytes, mimetype="application/pdf",
            headers={"Content-Disposition":
                     f'attachment; filename="{fname}"'},
        )

    # ----- Account (any logged-in user) -----

    @app.route("/account/password", methods=["GET", "POST"])
    @login_required
    def account_password():
        if request.method == "POST":
            db = models.get_db()
            current = request.form.get("current_password", "")
            new = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            row = models.get_user_by_id(db, current_user.id)
            if not models.verify_password(row, current):
                flash("Aktuelles Passwort stimmt nicht.", "error")
            elif len(new) < 6:
                flash("Neues Passwort muss mindestens 6 Zeichen haben.", "error")
            elif new != confirm:
                flash("Die beiden neuen Passwörter stimmen nicht überein.", "error")
            else:
                models.set_user_password(db, current_user.id, new)
                db.commit()
                flash("Passwort geändert.", "success")
                return redirect(url_for("index"))
        return render_template("account_password.html")

    # ----- Admin user management -----

    @app.route("/admin/users")
    @admin_required
    def admin_users():
        db = models.get_db()
        users = models.list_users(db)
        return render_template("admin_users.html", users=users,
                               events=models.list_events(db),
                               user_event_permissions={
                                   u["id"]: models.get_user_event_permissions(db, u["id"])
                                   for u in users
                               },
                               server_time=models.get_server_time_info(db),
                               format_dt=models.format_dt)

    @app.route("/admin/timezone", methods=["POST"])
    @admin_required
    def admin_set_timezone():
        db = models.get_db()
        tz = (request.form.get("app_timezone") or "").strip()
        if tz and tz in models.APP_TIMEZONES:
            models.set_app_setting(db, "app_timezone", tz)
            db.commit()
            flash(f"App-Zeitzone gesetzt auf {tz}. Container-Zeit wird beim "
                  f"nächsten Neustart über die Umgebungsvariable TZ "
                  f"aus dem Dockerfile übernommen.", "success")
        else:
            flash("Ungültige Zeitzone.", "error")
        return redirect(_admin_settings_url("system"))

    @app.route("/admin/events/create", methods=["POST"])
    @admin_required
    def admin_event_create():
        db = models.get_db()
        name = (request.form.get("name") or "").strip()
        prefix = (request.form.get("prefix") or "").strip()
        if not name:
            flash("Name der Veranstaltung ist Pflicht.", "error")
            return redirect(_admin_settings_url("events"))
        event_id = models.create_event(
            db, name, prefix,
            (request.form.get("start_date") or "").strip() or None,
            (request.form.get("end_date") or "").strip() or None,
        )
        for u in models.list_users(db):
            if u["is_admin"]:
                continue
            models.set_user_event_permission(
                db, u["id"], event_id, can_view=False, can_create=False)
        db.commit()
        flash(f"Veranstaltung '{name}' angelegt.", "success")
        return redirect(_admin_settings_url("events"))

    @app.route("/admin/events/<int:event_id>/update", methods=["POST"])
    @admin_required
    def admin_event_update(event_id: int):
        db = models.get_db()
        if not models.get_event(db, event_id):
            abort(404)
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Name der Veranstaltung ist Pflicht.", "error")
            return redirect(_admin_settings_url("events"))
        models.update_event(
            db, event_id, name=name,
            prefix=(request.form.get("prefix") or "").strip(),
            start_date=(request.form.get("start_date") or "").strip() or None,
            end_date=(request.form.get("end_date") or "").strip() or None,
            is_active=bool(request.form.get("is_active")),
        )
        db.commit()
        flash("Veranstaltung aktualisiert.", "success")
        return redirect(_admin_settings_url("events"))

    @app.route("/admin/users/create", methods=["POST"])
    @admin_required
    def admin_user_create():
        db = models.get_db()
        username = (request.form.get("username") or "").strip()
        full_name = (request.form.get("full_name") or "").strip() or None
        password = request.form.get("password") or ""
        is_admin = bool(request.form.get("is_admin"))
        role = request.form.get("role") or "full"
        if role not in models.VALID_ROLES:
            flash(f"Unbekannte Rolle: {role}", "error")
            return redirect(_admin_settings_url("new-user"))
        if not username or not password:
            flash("Benutzername und Passwort sind Pflicht.", "error")
        elif len(password) < 6:
            flash("Passwort muss mindestens 6 Zeichen haben.", "error")
        elif models.get_user_by_username(db, username):
            flash(f"Benutzername '{username}' existiert bereits.", "error")
        else:
            user_id = models.create_user(db, username, password, full_name,
                                         is_admin=is_admin, role=role)
            if not is_admin:
                for event in models.list_events(db, active_only=True):
                    models.set_user_event_permission(
                        db, user_id, event["id"], can_view=True,
                        can_create=role in ("full", "zentral_writer",
                                            "triage_intake"))
            db.commit()
            flash(f"Benutzer '{username}' angelegt.", "success")
        return redirect(_admin_settings_url("users"))

    @app.route("/admin/users/<int:user_id>/events", methods=["POST"])
    @admin_required
    def admin_user_set_events(user_id: int):
        db = models.get_db()
        row = models.get_user_by_id(db, user_id)
        if not row:
            abort(404)
        if row["is_admin"]:
            flash("Admins sehen alle Veranstaltungen und dürfen überall anlegen.", "info")
            return redirect(_admin_settings_url("users"))
        for event in models.list_events(db):
            view = bool(request.form.get(f"event_{event['id']}_view"))
            create = bool(request.form.get(f"event_{event['id']}_create"))
            models.set_user_event_permission(
                db, user_id, event["id"], can_view=view, can_create=create)
        db.commit()
        flash(f"Veranstaltungen für '{row['username']}' aktualisiert.", "success")
        return redirect(_admin_settings_url("users"))

    @app.route("/admin/users/<int:user_id>/permissions", methods=["POST"])
    @admin_required
    def admin_user_set_permissions(user_id: int):
        db = models.get_db()
        row = models.get_user_by_id(db, user_id)
        if not row:
            abort(404)
        for perm in models.USER_PERMISSIONS:
            value = bool(request.form.get(perm))
            models.set_user_permission(db, user_id, perm, value)
        db.commit()
        flash(
            f"Berechtigungen für '{row['username']}' aktualisiert.",
            "success",
        )
        return redirect(_admin_settings_url("users"))

    @app.route("/admin/users/<int:user_id>/role", methods=["POST"])
    @admin_required
    def admin_user_set_role(user_id: int):
        db = models.get_db()
        row = models.get_user_by_id(db, user_id)
        if not row:
            abort(404)
        role = request.form.get("role") or "full"
        if role not in models.VALID_ROLES:
            flash(f"Unbekannte Rolle: {role}", "error")
            return redirect(_admin_settings_url("users"))
        models.set_user_role(db, user_id, role)
        db.commit()
        flash(f"Rolle für '{row['username']}' geändert auf '{role}'.",
              "success")
        return redirect(_admin_settings_url("users"))

    @app.route("/admin/users/<int:user_id>/reset-totp", methods=["POST"])
    @admin_required
    def admin_user_reset_totp(user_id: int):
        db = models.get_db()
        row = models.get_user_by_id(db, user_id)
        if not row:
            abort(404)
        models.reset_totp(db, user_id)
        db.commit()
        flash(
            f"2FA für '{row['username']}' zurückgesetzt — User muss bei "
            f"nächster Anmeldung erneut einen Authenticator einrichten.",
            "success",
        )
        return redirect(_admin_settings_url("users"))

    @app.route("/admin/users/<int:user_id>/toggle-totp-required",
               methods=["POST"])
    @admin_required
    def admin_user_toggle_totp_required(user_id: int):
        db = models.get_db()
        row = models.get_user_by_id(db, user_id)
        if not row:
            abort(404)
        new_state = not bool(row["totp_required"])
        models.set_totp_required(db, user_id, new_state)
        db.commit()
        if new_state:
            flash(
                f"2FA für '{row['username']}' wieder aktiviert. Beim "
                f"nächsten Login wird "
                f"{'der Code abgefragt' if row['totp_confirmed'] else 'das Setup gestartet'}.",
                "success",
            )
        else:
            flash(
                f"2FA für '{row['username']}' deaktiviert — meldet sich "
                f"jetzt nur mit Passwort an.",
                "success",
            )
        return redirect(_admin_settings_url("users"))

    @app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
    @admin_required
    def admin_user_reset_password(user_id: int):
        db = models.get_db()
        row = models.get_user_by_id(db, user_id)
        if not row:
            abort(404)
        new_password = request.form.get("new_password") or ""
        if len(new_password) < 6:
            flash("Passwort muss mindestens 6 Zeichen haben.", "error")
        else:
            models.set_user_password(db, user_id, new_password)
            db.commit()
            flash(f"Passwort für '{row['username']}' zurückgesetzt.", "success")
        return redirect(_admin_settings_url("users"))

    @app.route("/admin/users/<int:user_id>/toggle-admin", methods=["POST"])
    @admin_required
    def admin_user_toggle_admin(user_id: int):
        db = models.get_db()
        row = models.get_user_by_id(db, user_id)
        if not row:
            abort(404)
        new_state = not bool(row["is_admin"])
        # Don't let the last admin demote themselves into a lockout.
        if not new_state and models.count_admins(db) <= 1:
            flash("Mindestens ein Admin muss bleiben.", "error")
            return redirect(_admin_settings_url("users"))
        models.set_user_admin(db, user_id, new_state)
        db.commit()
        flash(
            f"'{row['username']}' ist jetzt "
            f"{'Admin' if new_state else 'normaler Benutzer'}.",
            "success",
        )
        return redirect(_admin_settings_url("users"))

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def admin_user_delete(user_id: int):
        db = models.get_db()
        row = models.get_user_by_id(db, user_id)
        if not row:
            abort(404)
        if user_id == current_user.id:
            flash("Du kannst dich nicht selbst löschen.", "error")
            return redirect(_admin_settings_url("users"))
        if row["is_admin"] and models.count_admins(db) <= 1:
            flash("Letzten Admin kann man nicht löschen.", "error")
            return redirect(_admin_settings_url("users"))
        models.delete_user(db, user_id)
        db.commit()
        flash(f"Benutzer '{row['username']}' gelöscht.", "success")
        return redirect(_admin_settings_url("users"))

    # ----- Admin: Reset (alle Protokolle löschen + Counter zurück) -----

    @app.route("/admin/reset-protocols", methods=["POST"])
    @admin_required
    def admin_reset_protocols():
        db = models.get_db()
        # Doppelte Sicherung: Passwort des angemeldeten Admins + Bestätigungstext
        password = request.form.get("password") or ""
        confirm = (request.form.get("confirm") or "").strip()
        user_row = models.get_user_by_id(db, current_user.id)
        if not user_row or not models.verify_password(user_row, password):
            flash("Passwort falsch — nichts gelöscht.", "error")
            return redirect(_admin_settings_url("danger"))
        if confirm != "RESET":
            flash("Bitte 'RESET' (Großbuchstaben) als Bestätigung eintippen — nichts gelöscht.",
                  "error")
            return redirect(_admin_settings_url("danger"))
        stats = models.reset_all_protocols(db)
        db.commit()
        flash(
            f"Alle Protokolle gelöscht: {stats['decentral']} dezentral, "
            f"{stats['central']} zentral, {stats['comments']} Kommentare, "
            f"{stats['central_comments']} zentrale Kommentare. "
            f"Patienten bleiben erhalten. Nächste Bericht-Nr. ist wieder #1.",
            "success",
        )
        return redirect(_admin_settings_url("danger"))

    # ----- Admin-PIN (für Entschlüsselungs-Freigaben) -----

    @app.route("/account/pin", methods=["GET", "POST"])
    @login_required
    def account_pin():
        if not current_user.is_admin:
            abort(403)
        if request.method == "POST":
            db = models.get_db()
            current = request.form.get("current_password", "")
            new_pin = (request.form.get("new_pin") or "").strip()
            confirm = (request.form.get("confirm_pin") or "").strip()
            row = models.get_user_by_id(db, current_user.id)
            if not models.verify_password(row, current):
                flash("Aktuelles Passwort stimmt nicht.", "error")
            elif new_pin and (len(new_pin) < 4 or not new_pin.isdigit()):
                flash("PIN muss mindestens 4 Ziffern haben (nur Zahlen).", "error")
            elif new_pin != confirm:
                flash("Die beiden PIN-Eingaben stimmen nicht überein.", "error")
            else:
                models.set_admin_pin(db, current_user.id,
                                     new_pin if new_pin else None)
                db.commit()
                if new_pin:
                    flash("Admin-PIN gesetzt.", "success")
                else:
                    flash("Admin-PIN entfernt.", "success")
                return redirect(url_for("account_pin"))
        # Show whether a PIN is currently set
        db = models.get_db()
        row = models.get_user_by_id(db, current_user.id)
        return render_template("account_pin.html",
                               has_pin=bool(row["admin_pin_hash"]))

    @app.route("/api/central/protokolle/<int:pid>/reveal-contact",
               methods=["POST"])
    @login_required
    def api_central_reveal_contact(pid: int):
        """Gibt Adresse + Krankenkasse + Telefon eines zentralen Berichts
        an Nicht-Admins frei, wenn ein Admin sich per PIN verifiziert."""
        db = models.get_db()
        rec = models.get_central_protocol(db, pid)
        if not rec:
            return {"error": "not found"}, 404
        body = request.get_json(silent=True) or {}
        admin_username = (body.get("admin_username") or "").strip()
        admin_pin = (body.get("admin_pin") or "").strip()
        if not admin_username or not admin_pin:
            return {"error": "Admin und PIN erforderlich."}, 400
        approver = models.verify_admin_pin(db, admin_username, admin_pin)
        if not approver:
            return {"error": "Admin oder PIN falsch — oder PIN nicht gesetzt."}, 401
        # Audit als zusätzliche Notfall-Freigabe protokollieren
        if rec.get("patient_id"):
            models.log_emergency_unlock(db, rec["patient_id"],
                                        requested_by=current_user.id,
                                        approved_by=approver["id"])
            db.commit()
        d = rec.get("data") or {}
        def _scalar(v):
            if isinstance(v, list):
                return v[0] if v else ""
            return v or ""
        return {
            "contact": {
                "strasse": _scalar(d.get("strasse")),
                "plz": _scalar(d.get("plz")),
                "stadt": _scalar(d.get("stadt")),
                "telefon": _scalar(d.get("telefon")),
                "krankenkasse": _scalar(d.get("krankenkasse")),
            },
            "approved_by": approver["full_name"] or approver["username"],
        }

    # ----- Admin-Liste für Unlock-Dropdowns -----

    @app.route("/api/admin-list")
    @login_required
    def api_admin_list():
        """Liefert Admins mit gesetztem PIN — für die Auswahl beim
        Entschlüsseln-Dialog."""
        rows = models.list_admin_users_with_pin(models.get_db())
        return {"admins": [
            {"username": r["username"],
             "label": r["full_name"] or r["username"]}
            for r in rows
        ]}

    # ----- Sensitive Patient-Daten (Notfallkontakt + Med) -----

    @app.route("/api/patient/<int:pid>/sensitive")
    @login_required
    def api_patient_sensitive(pid: int):
        """Liefert maskierte Sicht (ja/nein) für User ohne Kontakt-
        Berechtigung, volle Sicht sonst. Wird vom SPA-Sidebar-Widget
        aufgerufen."""
        db = models.get_db()
        patient = models.get_patient(db, pid)
        if not patient:
            return {"error": "not found"}, 404
        if current_user.can_view_contact:
            return {"locked": False,
                    "data": models.patient_sensitive_full(patient)}
        return {"locked": True,
                "data": models.patient_sensitive_summary(patient)}

    @app.route("/api/patient/<int:pid>/unlock-emergency", methods=["POST"])
    @login_required
    def api_patient_unlock_emergency(pid: int):
        """Ein Admin verifiziert sich (Username + PIN). Wenn das passt,
        bekommt der anfragende User einmalig die volle Sicht zurück.
        Beide werden in emergency_unlocks geloggt.
        """
        db = models.get_db()
        patient = models.get_patient(db, pid)
        if not patient:
            return {"error": "not found"}, 404
        body = request.get_json(silent=True) or {}
        admin_username = (body.get("admin_username") or "").strip()
        admin_pin = (body.get("admin_pin") or "").strip()
        if not admin_username or not admin_pin:
            return {"error": "Username und PIN erforderlich."}, 400
        approver = models.verify_admin_pin(db, admin_username, admin_pin)
        if not approver:
            return {"error": "Admin-Username oder PIN falsch — oder PIN nicht gesetzt."}, 401
        # Log + return full data
        models.log_emergency_unlock(db, pid,
                                    requested_by=current_user.id,
                                    approved_by=approver["id"])
        db.commit()
        return {
            "locked": False,
            "data": models.patient_sensitive_full(patient),
            "approved_by": approver["full_name"] or approver["username"],
        }

    # ----- Hilfe / Anleitungen -----

    @app.route("/docs")
    @login_required
    def docs_index():
        return render_template("docs_index.html")

    @app.route("/docs/user")
    @login_required
    def docs_user():
        return render_template("docs_user.html")

    @app.route("/docs/admin")
    @login_required
    def docs_admin():
        if not current_user.is_admin:
            abort(403)
        return render_template("docs_admin.html")

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("403.html"), 403

    # ----- CLI -----

    @app.cli.command("create-user")
    @click.argument("username")
    @click.option("--password", prompt=True, hide_input=True,
                  confirmation_prompt=True)
    @click.option("--full-name", default=None)
    @click.option("--admin/--no-admin", default=False,
                  help="Mark this user as administrator.")
    @click.option("--role",
                  type=click.Choice(list(models.VALID_ROLES)),
                  default="full",
                  help="Role: 'full' (default) or 'zentral_writer'.")
    def cli_create_user(username, password, full_name, admin, role):
        """Create a new user account."""
        db = models.get_db()
        if models.get_user_by_username(db, username):
            click.echo(f"User '{username}' exists already.", err=True)
            raise SystemExit(1)
        # If no admin exists yet, the first user is auto-promoted by init_db,
        # but allow explicit --admin too.
        models.create_user(db, username, password, full_name,
                           is_admin=admin, role=role)
        db.commit()
        # Re-run admin migration so the very first user gets is_admin=1
        # without needing the flag.
        models.init_db(Path(app.config["DB_PATH"]))
        click.echo(f"User '{username}' created (role={role}).")

    return app


# ---------- helpers ----------

def _decentral_count_for_patient(db, patient_id, event_id=None):
    if patient_id is None:
        return 0
    event_sql = " AND event_id = ?" if event_id is not None else ""
    params = (patient_id, event_id) if event_id is not None else (patient_id,)
    return db.execute(
        f"SELECT COUNT(*) AS n FROM protocols WHERE patient_id = ?{event_sql}",
        params,
    ).fetchone()["n"]


def _read_filters(args) -> dict:
    return {
        "date_from": (args.get("date_from") or "").strip() or None,
        "date_to": (args.get("date_to") or "").strip() or None,
        "stammnummer": (args.get("stammnummer") or "").strip() or None,
        "name_query": (args.get("name") or "").strip() or None,
    }


def _admin_settings_url(section: str = "events") -> str:
    allowed = {"events", "new-user", "users", "danger"}
    target = section if section in allowed else "events"
    return url_for("admin_users") + f"#{target}"


def _event_report_stats(db, event_id: int) -> dict:
    totals = db.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM protocols WHERE event_id = ?) AS decentral,
          (SELECT COUNT(*) FROM central_protocols WHERE event_id = ?) AS central,
          (SELECT COUNT(DISTINCT patient_id) FROM (
             SELECT patient_id FROM protocols WHERE event_id = ?
             UNION ALL
             SELECT patient_id FROM central_protocols
              WHERE event_id = ? AND patient_id IS NOT NULL
           )) AS patients,
          (SELECT COUNT(*) FROM triage_entries WHERE event_id = ?) AS triage_total,
          (SELECT COUNT(*) FROM triage_entries
            WHERE event_id = ? AND status = 'abgeschlossen') AS triage_finished
        """,
        (event_id, event_id, event_id, event_id, event_id, event_id),
    ).fetchone()
    categories = db.execute(
        """
        SELECT category, COUNT(*) AS n
        FROM triage_entries
        WHERE event_id = ?
        GROUP BY category
        ORDER BY category
        """,
        (event_id,),
    ).fetchall()
    top_responders = models.top_decentral_responders(
        db, limit=8, event_id=event_id)
    latest = models.list_unified_protocols(db, event_id=event_id)[:12]
    return {
        "totals": dict(totals) if totals else {},
        "categories": [dict(r) for r in categories],
        "top_responders": [dict(r) for r in top_responders],
        "latest": [dict(r) for r in latest],
    }


def _save_protocol(protocol_id):
    """Shared handler for create + edit POST. Returns a Flask response."""
    db = models.get_db()
    if protocol_id is None:
        event_id = _require_event_create()
    else:
        existing = models.get_protocol(db, protocol_id)
        if not existing:
            abort(404)
        event_id = _require_event_view(existing["event_id"])
    form = request.form

    name = form.get("patient_name", "").strip()
    geburtsdatum = form.get("patient_geburtsdatum", "").strip()
    if not name or not geburtsdatum:
        flash("Name und Geburtsdatum sind Pflichtfelder.", "error")
        return redirect(request.url)

    patient_id = models.upsert_patient(
        db, name, geburtsdatum,
        form.get("patient_stammnummer", "").strip() or None,
    )

    data = {f: form.get(f, "").strip() for f in models.PROTOCOL_FIELDS}

    if protocol_id is None:
        new_id = models.create_protocol(db, patient_id, data, current_user.id,
                                        event_id=event_id)
        db.commit()
        previous = db.execute(
            "SELECT COUNT(*) AS n FROM protocols WHERE patient_id = ? AND id != ?",
            (patient_id, new_id),
        ).fetchone()["n"]
        # Resolve laufende_nr to show in the flash
        new_label = db.execute(
            "SELECT laufende_nr FROM protocols WHERE id = ?", (new_id,)
        ).fetchone()["laufende_nr"] or f"#dEH{new_id}"
        if previous > 0:
            flash(f"Bericht {new_label} gespeichert (Folgebehandlung — Patient "
                  f"hat bereits {previous} frühere(n) Eintrag/Einträge).", "info")
        else:
            flash(f"Bericht {new_label} gespeichert.", "success")
        # Schnellmodus: "Speichern + nächsten anlegen" → leeres Formular
        if form.get("next") == "new":
            return redirect(url_for("protocol_new"))
        return redirect(url_for("protocol_detail", protocol_id=new_id))

    # Edit: also reassign patient if name/Geburtsdatum changed.
    db.execute("UPDATE protocols SET patient_id = ? WHERE id = ?",
               (patient_id, protocol_id))
    models.update_protocol(db, protocol_id, data)
    db.commit()
    flash("Einsatzbericht aktualisiert.", "success")
    return redirect(url_for("protocol_detail", protocol_id=protocol_id))


# WSGI entrypoint
app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
