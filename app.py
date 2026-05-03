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


# Triage-intake users are restricted to the kiosk flow.
TRIAGE_INTAKE_ALLOWED = (
    "/triage/new", "/triage", "/account/password",
    "/login", "/logout", "/setup-totp", "/two-factor",
    "/static/", "/api/triage/",
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

    # Auto-init schema on startup so the first request never hits a missing table.
    with app.app_context():
        models.init_db(Path(app.config["DB_PATH"]))

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
        filters = _read_filters(request.args)
        source_filter = (request.args.get("type") or "").strip() or None
        if source_filter not in ("decentral", "central"):
            source_filter = None
        unified = models.list_unified_protocols(
            models.get_db(),
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
        comments = models.list_comments(db, protocol_id)
        siblings = models.list_patient_protocols(db, protocol["patient_id"])
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
        if not models.get_protocol(db, protocol_id):
            abort(404)
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
        patients = models.list_patients(models.get_db())
        return render_template("patient_list.html", patients=patients,
                               format_dt=models.format_dt)

    @app.route("/patients/<int:patient_id>")
    @login_required
    def patient_detail(patient_id: int):
        db = models.get_db()
        patient = models.get_patient(db, patient_id)
        if not patient:
            abort(404)
        protocols = models.list_patient_protocols(db, patient_id)
        central_protocols = models.list_central_protocols(db, patient_id=patient_id)
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
        """Vorschauseite für den vollständigen Akten-Export.
        Zeigt, was im PDF landet, plus den Download-Button.
        Sensible Daten erscheinen nur für Admins; Voll-User sehen
        gemaskt und können den Export trotzdem auslösen — die PDF
        enthält dann nur die nicht-vertraulichen Teile."""
        db = models.get_db()
        patient = models.get_patient(db, patient_id)
        if not patient:
            abort(404)
        decentral = models.list_patient_protocols(db, patient_id)
        central = models.list_central_protocols(db, patient_id=patient_id)
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
        )

    @app.route("/patients/<int:patient_id>/akte.pdf")
    @login_required
    def patient_akte_pdf(patient_id: int):
        if not current_user.can_export_akte:
            abort(403)
        db = models.get_db()
        patient = models.get_patient(db, patient_id)
        if not patient:
            abort(404)
        # Sammle alles ein
        decentral = []
        for p in models.list_patient_protocols(db, patient_id):
            full = models.get_protocol(db, p["id"])
            full_dict = dict(full)
            full_dict["comments"] = [dict(c) for c in models.list_comments(db, p["id"])]
            decentral.append(full_dict)
        central = []
        for c in models.list_central_protocols(db, patient_id=patient_id):
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
        counts = models.patient_protocol_counts(models.get_db(), row["id"])
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
        filters = _read_filters(request.args)
        rows = models.list_protocols(models.get_db(), **filters)

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

    # ----- Dashboard -----

    @app.route("/dashboard")
    @decentral_view_required
    def dashboard():
        from datetime import date as _date
        db = models.get_db()
        # Selected day for the day-view; default heute.
        sel = (request.args.get("date") or "").strip()
        try:
            ref = _date.fromisoformat(sel) if sel else _date.today()
        except ValueError:
            ref = _date.today()

        stats = models.dashboard_stats(db, ref)
        chart = models.dashboard_daily_counts(db, end_date=ref, days=14)
        chart_max = max((d["decentral"] + d["central"] for d in chart),
                        default=0)
        # Berichte des ausgewählten Tages
        day_protocols = models.list_unified_protocols(
            db, date_from=ref.isoformat(), date_to=ref.isoformat()
        )
        top = models.top_decentral_responders(db, limit=5)
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
        waiting = models.list_triage_waiting(db)
        active = models.list_triage_active(db, limit=20)
        recently_finished = models.list_triage_recently_finished(db, limit=10)
        # Höchste vorhandene Triage-ID — als Anker fürs Polling
        row = db.execute(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM triage_entries"
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
                created_by=current_user.id,
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
        rows = models.get_db().execute(
            """
            SELECT id, category, name, arrival_at, status
            FROM triage_entries
            WHERE id > ?
            ORDER BY id DESC
            LIMIT 50
            """,
            (since_id,),
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
            counts = models.patient_protocol_counts(db, r["id"])
            last = models.patient_last_treatment(db, r["id"])
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
        db = models.get_db()
        entry = models.get_triage_entry(db, tid)
        if not entry:
            abort(404)
        # Status auf in_behandlung setzen
        models.start_triage_treatment(db, tid)
        db.commit()
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
        triage = models.get_triage_for_protocol(db, pid)
        if not triage:
            return {"has_triage": False}
        finished_at = triage.get("treatment_finished_at")
        return {
            "has_triage": True,
            "triage_id": triage["id"],
            "status": triage.get("status"),
            "category": triage.get("category"),
            "started_at": triage.get("treatment_started_at"),
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
                counts = models.patient_protocol_counts(db, r["id"])
                last = models.patient_last_treatment(db, r["id"])
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
        # Lesen ist für alle eingeloggten User erlaubt (auch zentral_writer
        # darf Vorbehandlungen sehen). Schreiben/Löschen prüft weiter unten.
        comments = models.list_central_comments(db, pid)
        # Geschwister-Berichte (beide Typen) für diesen Patienten anzeigen,
        # sofern wir eine Patienten-Verknüpfung haben.
        decentral_siblings = []
        central_siblings = []
        if rec.get("patient_id"):
            decentral_siblings = models.list_patient_protocols(
                db, rec["patient_id"]
            )
            central_siblings = [
                r for r in models.list_central_protocols(
                    db, patient_id=rec["patient_id"]
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
        if request.method == "GET":
            # zentral_writer: only their own protocols.
            rows = models.list_central_protocols(
                db,
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
                        db, r["patient_id"]),
                }
                for r in rows
            ]
        # POST
        data = request.get_json(silent=True) or {}
        new_id = models.create_central_protocol(db, data, current_user.id)
        # Falls die SPA mit ?triage_id=... aufgerufen wurde, das Protokoll
        # mit dem Triage-Eintrag verknüpfen.
        try:
            tid = int(request.args.get("triage_id") or 0)
        except (ValueError, TypeError):
            tid = 0
        if tid:
            models.link_triage_to_central_protocol(db, tid, new_id)
        db.commit()
        return {"id": new_id}, 201

    @app.route("/api/central/protokolle/<int:pid>", methods=["GET", "PUT", "DELETE"])
    @login_required
    def api_central_one(pid: int):
        db = models.get_db()
        rec = models.get_central_protocol(db, pid)
        if not rec:
            return {"error": "not found"}, 404
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
            counts = models.patient_protocol_counts(db, r["id"])
            last = models.patient_last_treatment(db, r["id"])
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
            "history": models.patient_history(db, pid),
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
        summary = models.protocol_summary(models.get_db(), source, pid)
        if not summary:
            return {"error": "not found"}, 404
        return summary

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
        users = models.list_users(models.get_db())
        return render_template("admin_users.html", users=users,
                               format_dt=models.format_dt)

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
            return redirect(url_for("admin_users"))
        if not username or not password:
            flash("Benutzername und Passwort sind Pflicht.", "error")
        elif len(password) < 6:
            flash("Passwort muss mindestens 6 Zeichen haben.", "error")
        elif models.get_user_by_username(db, username):
            flash(f"Benutzername '{username}' existiert bereits.", "error")
        else:
            models.create_user(db, username, password, full_name,
                               is_admin=is_admin, role=role)
            db.commit()
            flash(f"Benutzer '{username}' angelegt.", "success")
        return redirect(url_for("admin_users"))

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
        return redirect(url_for("admin_users"))

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
            return redirect(url_for("admin_users"))
        models.set_user_role(db, user_id, role)
        db.commit()
        flash(f"Rolle für '{row['username']}' geändert auf '{role}'.",
              "success")
        return redirect(url_for("admin_users"))

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
        return redirect(url_for("admin_users"))

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
        return redirect(url_for("admin_users"))

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
        return redirect(url_for("admin_users"))

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
            return redirect(url_for("admin_users"))
        models.set_user_admin(db, user_id, new_state)
        db.commit()
        flash(
            f"'{row['username']}' ist jetzt "
            f"{'Admin' if new_state else 'normaler Benutzer'}.",
            "success",
        )
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def admin_user_delete(user_id: int):
        db = models.get_db()
        row = models.get_user_by_id(db, user_id)
        if not row:
            abort(404)
        if user_id == current_user.id:
            flash("Du kannst dich nicht selbst löschen.", "error")
            return redirect(url_for("admin_users"))
        if row["is_admin"] and models.count_admins(db) <= 1:
            flash("Letzten Admin kann man nicht löschen.", "error")
            return redirect(url_for("admin_users"))
        models.delete_user(db, user_id)
        db.commit()
        flash(f"Benutzer '{row['username']}' gelöscht.", "success")
        return redirect(url_for("admin_users"))

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
            return redirect(url_for("admin_users"))
        if confirm != "RESET":
            flash("Bitte 'RESET' (Großbuchstaben) als Bestätigung eintippen — nichts gelöscht.",
                  "error")
            return redirect(url_for("admin_users"))
        stats = models.reset_all_protocols(db)
        db.commit()
        flash(
            f"Alle Protokolle gelöscht: {stats['decentral']} dezentral, "
            f"{stats['central']} zentral, {stats['comments']} Kommentare, "
            f"{stats['central_comments']} zentrale Kommentare. "
            f"Patienten bleiben erhalten. Nächste Bericht-Nr. ist wieder #1.",
            "success",
        )
        return redirect(url_for("admin_users"))

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

def _decentral_count_for_patient(db, patient_id):
    if patient_id is None:
        return 0
    return db.execute(
        "SELECT COUNT(*) AS n FROM protocols WHERE patient_id = ?",
        (patient_id,),
    ).fetchone()["n"]


def _read_filters(args) -> dict:
    return {
        "date_from": (args.get("date_from") or "").strip() or None,
        "date_to": (args.get("date_to") or "").strip() or None,
        "stammnummer": (args.get("stammnummer") or "").strip() or None,
        "name_query": (args.get("name") or "").strip() or None,
    }


def _save_protocol(protocol_id):
    """Shared handler for create + edit POST. Returns a Flask response."""
    db = models.get_db()
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
        new_id = models.create_protocol(db, patient_id, data, current_user.id)
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
