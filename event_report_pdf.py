"""Reduced event report PDF export."""

from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


ACCENT = colors.HexColor("#7A1F2B")
SOFT = colors.HexColor("#F5F2EF")
BORDER = colors.HexColor("#D6CEC8")
TEXT = colors.HexColor("#1D1815")
MUTED = colors.HexColor("#6F655F")


def _styles():
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle(
            "H1", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=18, leading=22, textColor=ACCENT, spaceAfter=4,
        ),
        "tag": ParagraphStyle(
            "Tag", parent=base["Normal"], fontSize=9.5, leading=12,
            textColor=MUTED, spaceAfter=10,
        ),
        "h2": ParagraphStyle(
            "H2", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=11, leading=14, textColor=colors.white,
        ),
        "label": ParagraphStyle(
            "Label", parent=base["Normal"], fontName="Helvetica",
            fontSize=7.5, leading=9, textColor=MUTED,
        ),
        "value": ParagraphStyle(
            "Value", parent=base["Normal"], fontName="Helvetica",
            fontSize=9.5, leading=12, textColor=TEXT,
        ),
        "small": ParagraphStyle(
            "Small", parent=base["Normal"], fontName="Helvetica",
            fontSize=8, leading=10, textColor=MUTED,
        ),
    }


def _v(value):
    if value is None or value == "":
        return "—"
    return str(value).replace("\n", "<br/>")


def _section(title, styles):
    table = Table([[Paragraph(title, styles["h2"])]], colWidths=[170 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _kv_grid(pairs, styles, widths=None):
    widths = widths or [42.5 * mm] * len(pairs)
    labels = [Paragraph(label, styles["label"]) for label, _ in pairs]
    values = [Paragraph(_v(value), styles["value"]) for _, value in pairs]
    table = Table([labels, values], colWidths=widths)
    table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("BACKGROUND", (0, 0), (-1, 0), SOFT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _simple_table(headers, rows, styles, widths):
    data = [[Paragraph(h, styles["label"]) for h in headers]]
    for row in rows:
        data.append([Paragraph(_v(v), styles["value"]) for v in row])
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("BACKGROUND", (0, 0), (-1, 0), SOFT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def render_event_report_pdf(*, event, form, helpers, stats, exporter_label, format_dt):
    buf = io.BytesIO()
    exported_at = datetime.now().strftime("%d.%m.%Y %H:%M")
    title = form.get("title") or f"Veranstaltungs-Report {event.get('name')}"

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(
            20 * mm, 12 * mm,
            f"Exportiert von {exporter_label} am {exported_at}",
        )
        canvas.drawRightString(190 * mm, 12 * mm, f"Seite {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=20 * mm, title=title,
    )
    styles = _styles()
    story = [
        Paragraph(title, styles["h1"]),
        Paragraph("Reduzierter Report zur Veranstaltung und Dokumentation.", styles["tag"]),
        _section("Veranstaltung", styles),
        _kv_grid([
            ("Name", form.get("event_name") or event.get("name")),
            ("Zeitraum", " bis ".join(
                p for p in (format_dt(form.get("start_date")),
                            format_dt(form.get("end_date"))) if p
            )),
            ("Ort", form.get("location")),
            ("Veranstalter", form.get("organizer")),
        ], styles),
        Spacer(1, 8),
        _kv_grid([
            ("Sanitätsdienst-Leitung", form.get("medical_lead")),
            ("Einsatz-/Veranstaltungsleitung", form.get("incident_lead")),
            ("Nummern-Präfix", event.get("prefix")),
            ("Export", exported_at),
        ], styles),
        Spacer(1, 10),
        _section("Kennzahlen", styles),
    ]

    totals = stats.get("totals") or {}
    decentral = totals.get("decentral") or 0
    central = totals.get("central") or 0
    story.append(_kv_grid([
        ("Berichte gesamt", decentral + central),
        ("Dezentral", decentral),
        ("Zentral", central),
        ("Personen", totals.get("patients") or 0),
    ], styles))
    story.append(Spacer(1, 8))
    story.append(_kv_grid([
        ("Triage gesamt", totals.get("triage_total") or 0),
        ("Triage abgeschlossen", totals.get("triage_finished") or 0),
        ("SK I / II / III", " · ".join(
            f"{r.get('category')}: {r.get('n')}"
            for r in stats.get("categories", [])
        ) or "—"),
    ], styles, widths=[50 * mm, 50 * mm, 70 * mm]))

    story.append(Spacer(1, 10))
    story.append(_section("Helfer", styles))
    story.append(Paragraph(_v(", ".join(helpers)), styles["value"]))

    top = stats.get("top_responders") or []
    if top:
        story.append(Spacer(1, 10))
        story.append(_section("Top Ersthelfer dezentral", styles))
        story.append(_simple_table(
            ["Name", "Behandlungen"],
            [(r.get("responder"), r.get("n")) for r in top],
            styles,
            [130 * mm, 40 * mm],
        ))

    latest = stats.get("latest") or []
    if latest:
        story.append(Spacer(1, 10))
        story.append(_section("Letzte Berichte", styles))
        story.append(_simple_table(
            ["Nr.", "Typ", "Datum", "Name"],
            [
                (
                    r.get("laufende_nr"),
                    "dEH" if r.get("source") == "decentral" else "zEH",
                    format_dt(r.get("event_date")) or format_dt(r.get("created_at")),
                    r.get("patient_name"),
                )
                for r in latest
            ],
            styles,
            [32 * mm, 22 * mm, 42 * mm, 74 * mm],
        ))

    if form.get("notes"):
        story.append(Spacer(1, 10))
        story.append(_section("Bemerkungen", styles))
        story.append(Paragraph(_v(form.get("notes")), styles["value"]))

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
