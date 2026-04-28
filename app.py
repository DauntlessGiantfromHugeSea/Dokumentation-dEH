"""Erste-Hilfe Camp-Dokumentation — Flask web app."""

from __future__ import annotations

import csv
import io
import os
from functools import wraps
from pathlib import Path

import click
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
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


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.url))
        if not getattr(current_user, "is_admin", False):
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

    @login_manager.user_loader
    def load_user(user_id: str):
        row = models.get_user_by_id(models.get_db(), int(user_id))
        return User(row) if row else None

    # ----- Routes -----

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            row = models.get_user_by_username(models.get_db(), username)
            if row and models.verify_password(row, password):
                login_user(User(row))
                return redirect(request.args.get("next") or url_for("index"))
            flash("Benutzername oder Passwort falsch.", "error")
        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    # ----- Protocols -----

    @app.route("/")
    @login_required
    def index():
        filters = _read_filters(request.args)
        protocols = models.list_protocols(models.get_db(), **filters)
        return render_template("index.html", protocols=protocols, filters=filters,
                               format_dt=models.format_dt)

    @app.route("/protocols/new", methods=["GET", "POST"])
    @login_required
    def protocol_new():
        if request.method == "POST":
            return _save_protocol(None)
        prefill = {
            "patient_name": request.args.get("name", ""),
            "patient_geburtsdatum": request.args.get("geburtsdatum", ""),
            "patient_stammnummer": request.args.get("stammnummer", ""),
        }
        return render_template("protocol_form.html", protocol=prefill,
                               existing_patient=None, mode="new")

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
    @login_required
    def protocol_edit(protocol_id: int):
        db = models.get_db()
        protocol = models.get_protocol(db, protocol_id)
        if not protocol:
            abort(404)
        if request.method == "POST":
            return _save_protocol(protocol_id)
        return render_template("protocol_form.html", protocol=protocol,
                               existing_patient=None, mode="edit")

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
    @login_required
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
        return render_template("patient_detail.html", patient=patient,
                               protocols=protocols, format_dt=models.format_dt)

    # ----- Lookup for the new-protocol form (so the UI can announce
    # "Folgebehandlung" before submit) -----

    @app.route("/api/patient-lookup")
    @login_required
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
        count = models.get_db().execute(
            "SELECT COUNT(*) AS n FROM protocols WHERE patient_id = ?", (row["id"],)
        ).fetchone()["n"]
        return {
            "found": True,
            "patient_id": row["id"],
            "stammnummer": row["stammnummer"] or "",
            "previous_count": count,
        }

    # ----- Export -----

    @app.route("/export")
    @login_required
    def export_form():
        return render_template("export.html")

    @app.route("/export/csv")
    @login_required
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
                full["laufende_nr"] or models.laufende_nr_for(full["id"]),
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
        if not username or not password:
            flash("Benutzername und Passwort sind Pflicht.", "error")
        elif len(password) < 6:
            flash("Passwort muss mindestens 6 Zeichen haben.", "error")
        elif models.get_user_by_username(db, username):
            flash(f"Benutzername '{username}' existiert bereits.", "error")
        else:
            models.create_user(db, username, password, full_name, is_admin)
            db.commit()
            flash(f"Benutzer '{username}' angelegt.", "success")
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
    def cli_create_user(username, password, full_name, admin):
        """Create a new user account."""
        db = models.get_db()
        if models.get_user_by_username(db, username):
            click.echo(f"User '{username}' exists already.", err=True)
            raise SystemExit(1)
        # If no admin exists yet, the first user is auto-promoted by init_db,
        # but allow explicit --admin too.
        models.create_user(db, username, password, full_name, is_admin=admin)
        db.commit()
        # Re-run admin migration so the very first user gets is_admin=1
        # without needing the flag.
        models.init_db(Path(app.config["DB_PATH"]))
        click.echo(f"User '{username}' created.")

    return app


# ---------- helpers ----------

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
        if previous > 0:
            flash(f"Folgebehandlung gespeichert — Patient hat bereits "
                  f"{previous} frühere(n) Eintrag/Einträge.", "info")
        else:
            flash("Einsatzbericht gespeichert.", "success")
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
