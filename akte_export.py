"""Vollständiger Patientenakten-Export als PDF.

Wird vom /patients/<id>/akte.pdf-Endpunkt aufgerufen und erzeugt einen
ausführlichen Auszug für Rettungsdienst, Eltern oder Ärzte:

  - Stammdaten der Person
  - (admin-only) Notfallkontakt + Allergien + Medikamente
  - alle dezentralen Berichte mit Kommentaren
  - alle zentralen Berichte mit allen Notfall-Daten + Messwerten + Kommentaren
  - Änderungs-Historie
  - Footer mit Exporter und Erstellungs-Zeitstempel auf jeder Seite
"""

from __future__ import annotations

import base64
import io
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image as RLImage,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from models import format_dt


ACCENT = colors.HexColor("#7A1F2B")
SOFT = colors.HexColor("#F4F4F4")
RED_LIGHT = colors.HexColor("#FCE8E6")
GREEN_LIGHT = colors.HexColor("#E8F4EC")


def _styles():
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle("H1", parent=base["Title"], fontName="Helvetica-Bold",
                             fontSize=18, textColor=ACCENT, spaceAfter=4),
        "tagline": ParagraphStyle("Tagline", parent=base["Normal"], fontSize=10,
                                  textColor=colors.grey, spaceAfter=10),
        "h2": ParagraphStyle("H2", parent=base["Heading2"], fontName="Helvetica-Bold",
                             fontSize=12, textColor=colors.white, leading=16),
        "h3": ParagraphStyle("H3", parent=base["Heading3"], fontName="Helvetica-Bold",
                             fontSize=11, textColor=ACCENT, spaceBefore=8, spaceAfter=4),
        "label": ParagraphStyle("Label", parent=base["Normal"], fontName="Helvetica",
                                fontSize=8, textColor=colors.grey, leading=10),
        "value": ParagraphStyle("Value", parent=base["Normal"], fontName="Helvetica",
                                fontSize=10, leading=13),
        "comment": ParagraphStyle("Comment", parent=base["Normal"], fontName="Helvetica",
                                  fontSize=9, leading=12),
        "comment_meta": ParagraphStyle("CommentMeta", parent=base["Normal"],
                                       fontName="Helvetica-Oblique", fontSize=8,
                                       textColor=colors.grey, leading=10, spaceAfter=2),
        "small": ParagraphStyle("Small", parent=base["Normal"], fontName="Helvetica",
                                fontSize=8, leading=10, textColor=colors.grey),
        "warn": ParagraphStyle("Warn", parent=base["Normal"], fontName="Helvetica-Bold",
                               fontSize=10, leading=13, textColor=colors.HexColor("#b3261e")),
    }


def _section(title: str, styles, color=None) -> Table:
    bg = color or ACCENT
    tbl = Table([[Paragraph(title, styles["h2"])]], colWidths=[170 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return tbl


def _v(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, list):
        return ", ".join(str(x) for x in value if x not in (None, "")) or "—"
    return str(value)


def _kv_grid(pairs: Iterable[tuple[str, Any]], styles, col_widths) -> Table:
    pairs = list(pairs)
    if not pairs:
        return Spacer(1, 0)
    label_row = [Paragraph(label, styles["label"]) for label, _ in pairs]
    value_row = [
        Paragraph(_v(value).replace("\n", "<br/>"), styles["value"])
        for _, value in pairs
    ]
    tbl = Table([label_row, value_row], colWidths=col_widths)
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.4, colors.lightgrey),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (0, 0), (-1, 0), SOFT),
    ]))
    return tbl


def _kv_full(label: str, value: Any, styles) -> Table:
    return _kv_grid([(label, value)], styles, [170 * mm])


def _comments_block(comments: list[dict], styles) -> list:
    if not comments:
        return []
    out = [Paragraph("Kommentare", styles["h3"])]
    for c in comments:
        author = c.get("author_full_name") or c.get("author_username") or "—"
        meta = f"{author} · {format_dt(c.get('created_at'))}"
        out.append(Paragraph(meta, styles["comment_meta"]))
        out.append(Paragraph((c.get("text") or "").replace("\n", "<br/>"),
                             styles["comment"]))
        out.append(Spacer(1, 4))
    return out


def _decode_data_url(data_url) -> Optional[bytes]:
    if not data_url or not isinstance(data_url, str) or "," not in data_url:
        return None
    try:
        return base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        return None


def _is_valid_png(data: bytes) -> bool:
    """Validiert das PNG mit Pillow vorab — sonst kracht ReportLab beim
    eigentlichen Rendern (drawOn)."""
    try:
        from PIL import Image as PILImage
        with PILImage.open(io.BytesIO(data)) as im:
            im.verify()
        # verify() invalidiert das Bild — neu öffnen für späteres laden
        with PILImage.open(io.BytesIO(data)) as im:
            im.load()
        return True
    except Exception:
        return False


def _signatures_block(d: dict, styles) -> list:
    """Render signatures of einsatzkraft1 + einsatzkraft2 if present."""
    rows = []
    for n in (1, 2):
        sig_bytes = _decode_data_url(d.get(f"signature_einsatzkraft{n}"))
        if not sig_bytes or not _is_valid_png(sig_bytes):
            continue
        rows.append({
            "n": n,
            "img_bytes": sig_bytes,
            "name": d.get(f"einsatzkraft{n}") or "",
            "at": d.get(f"signature_einsatzkraft{n}_at"),
            "by": d.get(f"signature_einsatzkraft{n}_by") or "",
        })
    if not rows:
        return []

    elements = [Paragraph("Unterschriften", styles["h3"])]
    cells = []
    for r in rows:
        label = f"Einsatzkraft {r['n']}"
        if r["name"]:
            label += f" — {r['name']}"
        try:
            img = RLImage(io.BytesIO(r["img_bytes"]),
                          width=82 * mm, height=24 * mm,
                          kind="proportional")
        except Exception:
            img = Paragraph("(Unterschrift konnte nicht gerendert werden)",
                            styles["small"])
        meta_parts = []
        if r["at"]:
            meta_parts.append(f"am {format_dt(r['at'])}")
        if r["by"]:
            meta_parts.append(f"erfasst von {r['by']}")
        meta = Paragraph(" · ".join(meta_parts) or "—", styles["small"])
        cells.append([
            Paragraph(label, styles["label"]),
            img,
            meta,
        ])
    # Layout: two columns side-by-side if two signatures
    if len(cells) == 1:
        col_widths = [170 * mm]
        rows_table = [
            [cells[0][0]],
            [cells[0][1]],
            [cells[0][2]],
        ]
    else:
        col_widths = [85 * mm, 85 * mm]
        rows_table = [
            [cells[0][0], cells[1][0]],
            [cells[0][1], cells[1][1]],
            [cells[0][2], cells[1][2]],
        ]
    tbl = Table(rows_table, colWidths=col_widths)
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.4, colors.lightgrey),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (0, 0), (-1, 0), SOFT),
    ]))
    elements.append(tbl)
    return elements


def _decentral_block(p: dict, styles) -> list:
    laufende = p.get("laufende_nr") or f"#dEH{p['id']}"
    elements = [
        Paragraph(f"Bericht {laufende}", styles["h3"]),
        _kv_grid([
            ("EH-Datum/Uhrzeit", format_dt(p.get("eh_datum_uhrzeit"))),
            ("Ersthelfer", p.get("name_ersthelfer")),
            ("Unfall-Datum/Uhrzeit", format_dt(p.get("unfall_datum_uhrzeit"))),
            ("Unfallort", p.get("unfallort")),
        ], styles, col_widths=[42 * mm, 42 * mm, 42 * mm, 44 * mm]),
        Spacer(1, 4),
        _kv_full("Unfallhergang", p.get("unfallhergang"), styles),
        _kv_grid([
            ("Art und Umfang der Verletzung", p.get("art_umfang_verletzung")),
            ("Zeugen", p.get("name_zeugen")),
        ], styles, col_widths=[110 * mm, 60 * mm]),
        _kv_full("Erste-Hilfe-Maßnahmen", p.get("art_weise_massnahmen"), styles),
        _kv_full("Verbrauchtes Material", p.get("verbrauchtes_material"), styles),
    ]
    elements.extend(_comments_block(p.get("comments") or [], styles))
    author = p.get("author_full_name") or p.get("author_username") or "—"
    elements.append(Paragraph(
        f"Eingetragen von {author} am {format_dt(p.get('created_at'))}",
        styles["small"],
    ))
    return elements


def _vitals_table(d: dict, styles) -> Table:
    """Erst- und Übergabe-Vitalwerte nebeneinander."""
    header = ["Zeitpunkt", "Zeit", "RR", "Puls", "AF", "HF", "SpO₂",
              "etCO₂", "BZ", "Temp", "GCS"]
    def row(label, suf):
        rr = d.get(f"rr_sys_{suf}")
        rd = d.get(f"rr_dia_{suf}")
        rr_str = f"{rr or '?'}/{rd or '?'}" if (rr or rd) else "—"
        return [label, _v(d.get(f"zeit_{suf}")), rr_str,
                _v(d.get(f"puls_{suf}")), _v(d.get(f"af_{suf}")),
                _v(d.get(f"hf_{suf}")), _v(d.get(f"spo2_{suf}")),
                _v(d.get(f"etco2_{suf}")), _v(d.get(f"bz_{suf}")),
                _v(d.get(f"temp_{suf}")), _v(d.get(f"gcs_{suf}"))]
    data = [header, row("Erstbefund", "1"), row("Übergabe", "2")]
    tbl = Table(data, colWidths=[24 * mm] + [14.6 * mm] * 10)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SOFT),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.lightgrey),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return tbl


def _central_block(rec: dict, styles) -> list:
    d = rec.get("data") or {}
    laufende = rec.get("laufende_nr") or f"#zEH{rec['id']}"
    elements: list = [
        Paragraph(f"Bericht {laufende}", styles["h3"]),
    ]
    # Behandler-Block — wer hat behandelt + Triage-Workflow
    behandler = (rec.get("author_full_name") or rec.get("author_username")
                 or rec.get("name_summary") or "")
    if behandler:
        elements.append(_kv_full("Behandelt von", behandler, styles))
    triage = rec.get("triage")
    if triage:
        cat = triage.get("category")
        cat_label = ("SK I rot — sofort" if cat == "SK1"
                     else "SK II gelb — dringend" if cat == "SK2"
                     else "SK III grün — kann warten")
        elements.append(_kv_grid([
            ("Triage-Kategorie", cat_label),
            ("Anmeldung", format_dt(triage.get("arrival_at"))),
            ("Angemeldet von",
             triage.get("anmelder_full_name")
             or triage.get("anmelder_username") or "—"),
        ], styles, col_widths=[64 * mm, 53 * mm, 53 * mm]))
        if triage.get("treatment_started_at") or triage.get("treatment_finished_at"):
            elements.append(_kv_grid([
                ("Behandlungsstart", format_dt(triage.get("treatment_started_at"))),
                ("Behandlungsende", format_dt(triage.get("treatment_finished_at")) or "—"),
            ], styles, col_widths=[85 * mm, 85 * mm]))
        if triage.get("notes"):
            elements.append(_kv_full("Anmelde-Notiz", triage.get("notes"), styles))
        elements.append(Spacer(1, 4))
    elements += [
        _kv_grid([
            ("Einsatznummer", d.get("einsatznummer")),
            ("Datum", format_dt(d.get("datum"))),
            ("Beginn", d.get("einsatzbeginn")),
            ("Ende", d.get("einsatzende")),
        ], styles, col_widths=[44 * mm, 44 * mm, 41 * mm, 41 * mm]),
        _kv_grid([
            ("Einsatzort", d.get("einsatzort")),
            ("Stichwort", d.get("einsatzstichwort")),
            ("Alarmierung", d.get("alarm_durch")),
        ], styles, col_widths=[80 * mm, 50 * mm, 40 * mm]),
        _kv_grid([
            ("Einsatzkraft 1", d.get("einsatzkraft1")),
            ("Einsatzkraft 2", d.get("einsatzkraft2")),
        ], styles, col_widths=[85 * mm, 85 * mm]),
        Spacer(1, 4),
        _kv_full("Notfallsituation", d.get("notfallsituation"), styles),
        _kv_grid([
            ("Notfallart", d.get("notfallart")),
            ("Sonstige Notfallart", d.get("notfallart_sonstige")),
        ], styles, col_widths=[110 * mm, 60 * mm]),
        _kv_full("Verletzung", d.get("verletzung"), styles),
    ]
    # Verletzungslokalisation (Body-Chart) wenn Marker vorhanden
    from pdf_fill import _parse_body_markers, _body_chart_flowable
    markers = _parse_body_markers(d.get("body_markers"))
    if markers:
        from reportlab.platypus import Table as _Tab, TableStyle as _TS
        from reportlab.lib import colors as _col
        elements.append(Paragraph("Verletzungslokalisation", styles["h3"]))
        marker_text = "<br/>".join(
            f"<b>{i + 1}.</b> {'hinten' if m.get('side') == 'back' else 'vorne'}"
            f"{(' — ' + str(m.get('note',''))) if m.get('note') else ''}"
            for i, m in enumerate(markers)
        )
        tbl = _Tab([[
            _body_chart_flowable(markers, width_mm=75),
            Paragraph(marker_text, styles["small"]),
        ]], colWidths=[75 * mm, 95 * mm])
        tbl.setStyle(_TS([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.4, _col.HexColor("#999")),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, _col.HexColor("#CCC")),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(tbl)
    elements += [
        Spacer(1, 4),
        Paragraph("Erstbefund", styles["h3"]),
        _kv_grid([
            ("Bewusstsein", d.get("bewusstsein_1")),
            ("Atmung", d.get("atmung_1")),
            ("Kreislauf", d.get("kreislauf_1")),
            ("EKG", d.get("ekg_1")),
        ], styles, col_widths=[42 * mm, 42 * mm, 42 * mm, 44 * mm]),
        _kv_grid([
            ("Schmerzen", d.get("schmerzen_grad_1")),
            ("NRS", d.get("nrs_1")),
            ("Pupille links", d.get("pupille_l")),
            ("Pupille rechts", d.get("pupille_r")),
        ], styles, col_widths=[42 * mm, 42 * mm, 42 * mm, 44 * mm]),
        _kv_grid([
            ("Haut", d.get("haut")),
            ("Psyche", d.get("psyche")),
        ], styles, col_widths=[85 * mm, 85 * mm]),
        Spacer(1, 4),
        Paragraph("Vitalwerte", styles["h3"]),
        _vitals_table(d, styles),
        Spacer(1, 6),
        Paragraph("Diagnose &amp; Maßnahmen", styles["h3"]),
        _kv_full("Erstdiagnose", d.get("erstdiagnose"), styles),
        _kv_full("Maßnahmen", d.get("massnahme"), styles),
    ]
    if d.get("massnahmen_sonstiges"):
        elements.append(_kv_full("Sonstige Maßnahmen", d.get("massnahmen_sonstiges"), styles))
    if d.get("verlauf"):
        elements.append(Paragraph("Verlauf", styles["h3"]))
        elements.append(_kv_full("Verlauf", d.get("verlauf"), styles))
    elements.append(Paragraph("Übergabe", styles["h3"]))
    elements.append(_kv_grid([
        ("Übergabe an", d.get("uebergabe_an")),
        ("Ergebnis", d.get("ergebnis")),
        ("Infektion", d.get("infektion")),
        ("Begleitung", d.get("begleitung")),
        ("Übergabezeit", d.get("uebergabezeit")),
    ], styles, col_widths=[34 * mm, 34 * mm, 34 * mm, 34 * mm, 34 * mm]))
    if d.get("einsatzbeschreibung") or d.get("material"):
        elements.append(Paragraph("Abschluss", styles["h3"]))
        if d.get("einsatzbeschreibung"):
            elements.append(_kv_full("Einsatzbeschreibung", d.get("einsatzbeschreibung"), styles))
        if d.get("material"):
            elements.append(_kv_full("Verbrauchtes Material", d.get("material"), styles))
    elements.extend(_signatures_block(d, styles))
    elements.extend(_comments_block(rec.get("comments") or [], styles))
    elements.append(Paragraph(
        f"Erstellt am {format_dt(rec.get('created_at'))}"
        + (f" · zuletzt aktualisiert {format_dt(rec.get('updated_at'))}"
           if rec.get('updated_at') and rec.get('updated_at') != rec.get('created_at')
           else ""),
        styles["small"],
    ))
    return elements


def _bool_label(v):
    if v is None or v == "":
        return "unbekannt"
    return "ja" if str(v) in ("1", "True", "true") else "nein"


def render_patient_akte_pdf(*, patient: dict, decentral: list, central: list,
                            change_log: list, include_sensitive: bool,
                            exporter_label: str,
                            field_label: dict,
                            sensitive_fields: set) -> bytes:
    buf = io.BytesIO()
    exported_at = datetime.now().strftime("%d.%m.%Y %H:%M")
    title = f"Patientenakte — {patient.get('name', '—')}"

    def _on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        footer_l = (
            f"Akten-Auszug · exportiert von {exporter_label} am {exported_at}"
            + (" · Admin-Sicht inkl. vertraulicher Daten" if include_sensitive
               else " · Standard-Sicht (vertrauliche Daten ausgeblendet)")
        )
        canvas.drawString(20 * mm, 12 * mm, footer_l)
        canvas.drawRightString(190 * mm, 12 * mm, f"Seite {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=20 * mm,
        title=title,
    )
    styles = _styles()
    story: list = []

    # ----- Titel -----
    story.append(Paragraph(title, styles["h1"]))
    story.append(Paragraph(
        "Vollständiger Akten-Auszug — Stammdaten, Notfallinformationen, "
        "Behandlungs-Historie und Änderungsprotokoll.",
        styles["tagline"],
    ))

    # ----- Stammdaten -----
    story.append(_section("Stammdaten", styles))
    story.append(_kv_grid([
        ("Name", patient.get("name")),
        ("Geburtsdatum", format_dt(patient.get("geburtsdatum"))),
        ("Stammnummer", patient.get("stammnummer")),
    ], styles, col_widths=[80 * mm, 50 * mm, 40 * mm]))

    # ----- Sensitive: Notfallkontakt + Allergien + Medikamente -----
    story.append(Spacer(1, 6))
    story.append(_section("Notfallkontakt &amp; medizinische Hinweise", styles))
    if include_sensitive:
        ec_lines = []
        if patient.get("emergency_contact_name"):
            ec_lines.append(patient["emergency_contact_name"])
        if patient.get("emergency_contact_relation"):
            ec_lines[-1:] = [
                f"{ec_lines[-1] if ec_lines else ''} ({patient['emergency_contact_relation']})".strip()
            ]
        if patient.get("emergency_contact_phone"):
            ec_lines.append(patient["emergency_contact_phone"])
        ec_text = "<br/>".join(ec_lines) if ec_lines else "—"

        story.append(_kv_grid([
            ("Notfallkontakt", ec_text),
            ("Allergien", _bool_label(patient.get("has_allergies"))),
            ("Medikamente", _bool_label(patient.get("has_medications"))),
        ], styles, col_widths=[80 * mm, 45 * mm, 45 * mm]))
        if patient.get("allergies_text"):
            story.append(_kv_full("Allergien — Details", patient["allergies_text"], styles))
        if patient.get("medications_text"):
            story.append(_kv_full("Medikamente — Details", patient["medications_text"], styles))
        if patient.get("extras_notes"):
            story.append(_kv_full("Sonstige Hinweise", patient["extras_notes"], styles))
    else:
        # Voll-User: nur Indikatoren
        story.append(_kv_grid([
            ("Notfallkontakt vorhanden",
             "ja" if (patient.get("emergency_contact_name")
                      or patient.get("emergency_contact_phone")) else "nein"),
            ("Allergien", _bool_label(patient.get("has_allergies"))),
            ("Medikamente", _bool_label(patient.get("has_medications"))),
        ], styles, col_widths=[80 * mm, 45 * mm, 45 * mm]))
        story.append(Paragraph(
            "Hinweis: Notfallkontaktdaten und Details zu Allergien / "
            "Medikamenten sind nur in Admin-exportierten Akten enthalten.",
            styles["small"],
        ))

    # ----- Dezentrale Berichte -----
    story.append(PageBreak())
    story.append(_section(f"Dezentrale Erste-Hilfe-Berichte ({len(decentral)})", styles))
    if not decentral:
        story.append(Paragraph("Keine dezentralen Berichte.", styles["small"]))
    for i, p in enumerate(decentral):
        if i > 0:
            story.append(Spacer(1, 8))
        story.extend(_decentral_block(p, styles))

    # ----- Zentrale Berichte -----
    story.append(PageBreak())
    story.append(_section(f"Zentrale Notfallprotokolle ({len(central)})", styles))
    if not central:
        story.append(Paragraph("Keine zentralen Berichte.", styles["small"]))
    for i, rec in enumerate(central):
        if i > 0:
            story.append(PageBreak())
        story.extend(_central_block(rec, styles))

    # ----- Änderungs-Historie -----
    story.append(PageBreak())
    story.append(_section("Änderungs-Historie der Stammdaten", styles))
    if not change_log:
        story.append(Paragraph("Keine Änderungen dokumentiert.", styles["small"]))
    else:
        rows: list = [["Wann", "Wer", "Feld", "Alt → Neu"]]
        for c in change_log:
            field = c.get("field_name", "")
            label = field_label.get(field, field)
            who = c.get("author_full_name") or c.get("author_username") or "—"
            if field in sensitive_fields and not include_sensitive:
                change = "vertraulich · nur in Admin-Export"
            elif field == "__akte_export__":
                change = f"Akten-PDF erzeugt ({c.get('new_value') or '—'})"
            else:
                change = f"{c.get('old_value') or '—'} → {c.get('new_value') or '—'}"
            rows.append([format_dt(c.get("changed_at")), who, label, change])
        tbl = Table(rows, colWidths=[28 * mm, 32 * mm, 40 * mm, 70 * mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), SOFT),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.grey),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
            ("BOX", (0, 0), (-1, -1), 0.4, colors.lightgrey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(tbl)

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buf.getvalue()
