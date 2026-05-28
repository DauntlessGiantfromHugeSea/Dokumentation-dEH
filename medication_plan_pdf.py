"""Medikamenten-Vergabe-Protokoll als A4-PDF, ein Blatt pro Person.

Layout (Landscape):
- Header: Patient, Geburtsdatum, Stammnummer, Zeitraum, Lagerungs-Note
- Große Tabelle: Zeilen = Medikament, Spalten = 7 Tage × 4 Slots (M/Mi/A/N)
                  + Bedarf-Spalte rechts
- Footer: Notfallkontakt, Allergien, Hinweise, Unterschrift-Linien, Exporter
"""

import io
from datetime import date as _date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, Spacer, Table, TableStyle, SimpleDocTemplate, PageBreak,
)

ACCENT = colors.HexColor("#7A1F2B")
ACCENT_DARK = colors.HexColor("#5A141D")
ACCENT_SOFT = colors.HexColor("#F6E8EA")
BORDER = colors.HexColor("#999999")
BORDER_SOFT = colors.HexColor("#DDDDDD")
BG_HEAD = colors.HexColor("#EFEAE6")
TICK = colors.HexColor("#1F6B3A")
MUTED = colors.HexColor("#666666")

S_TITLE = ParagraphStyle("Title", fontName="Helvetica-Bold",
                          fontSize=14, leading=17, textColor=ACCENT_DARK)
S_HEAD = ParagraphStyle("Head", fontName="Helvetica-Bold",
                         fontSize=8.5, leading=10, alignment=1)
S_HEAD_DAY = ParagraphStyle("Day", fontName="Helvetica-Bold",
                             fontSize=8.5, leading=10, alignment=1,
                             textColor=ACCENT_DARK)
S_SLOT = ParagraphStyle("Slot", fontName="Helvetica",
                         fontSize=6.5, leading=8, alignment=1,
                         textColor=MUTED)
S_TXT = ParagraphStyle("Txt", fontName="Helvetica", fontSize=8.5, leading=11)
S_TXT_BOLD = ParagraphStyle("TxtB", fontName="Helvetica-Bold",
                             fontSize=8.5, leading=11)
S_SMALL = ParagraphStyle("Small", fontName="Helvetica",
                          fontSize=7, leading=9, textColor=MUTED)
S_TICK = ParagraphStyle("Tick", fontName="Helvetica-Bold",
                         fontSize=10, alignment=1, textColor=TICK)


WEEKDAY_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

SLOTS_PLAN = ["morgens", "mittags", "abends", "nachts"]
SLOT_LABEL = {"morgens": "M", "mittags": "Mi",
              "abends": "A", "nachts": "N", "bedarf": "B.B."}
SLOT_LONG = {"morgens": "Morgens", "mittags": "Mittags",
             "abends": "Abends", "nachts": "Nachts", "bedarf": "Bei Bedarf"}


def _fmt_date(d):
    if isinstance(d, str):
        try:
            d = _date.fromisoformat(d[:10])
        except Exception:
            return d
    return d.strftime("%d.%m.")


def _patient_header(patient, days, exporter_label, blanko):
    name = (patient.get("name") or "").strip()
    geb = (patient.get("geburtsdatum") or "").strip()
    stamm = (patient.get("stammnummer") or "").strip()
    period = f"{_fmt_date(days[0])} – {_fmt_date(days[-1])}.{days[-1].year}"

    title_para = Paragraph(
        "Medikamenten-Vergabe-Protokoll"
        + (" <font color='#888888'>(Blanko)</font>" if blanko else ""),
        S_TITLE,
    )
    sub_lines = []
    if name: sub_lines.append(f"<b>Patient/in:</b> {name}")
    else: sub_lines.append(
        "<b>Patient/in:</b> ______________________________________")
    if geb: sub_lines.append(f"<b>Geb.:</b> {_fmt_date(geb)}")
    else: sub_lines.append("<b>Geb.:</b> __________")
    if stamm: sub_lines.append(f"<b>Stamm-Nr.:</b> {stamm}")
    sub_lines.append(f"<b>Zeitraum:</b> {period}")
    sub_para = Paragraph(" &nbsp;·&nbsp; ".join(sub_lines), S_TXT)
    return [title_para, Spacer(1, 4), sub_para, Spacer(1, 8)]


def _build_grid_header(days):
    # Erste Zeile: Medikament | Tag 1 .. Tag 7 (3-spaltig pro Tag) | BB
    top = [Paragraph("Medikament / Dosierung", S_HEAD)]
    for d in days:
        wd = WEEKDAY_DE[d.weekday()]
        top.append(Paragraph(f"{wd}<br/>{_fmt_date(d)}", S_HEAD_DAY))
    top.append(Paragraph("Bei<br/>Bedarf", S_HEAD_DAY))

    # Zweite Zeile: leerer Kopf-Cell + 4 Slot-Mini-Spalten je Tag
    sub = [Paragraph("", S_SLOT)]
    for _ in days:
        sub.append(Paragraph("M&nbsp;·&nbsp;Mi&nbsp;·&nbsp;A&nbsp;·&nbsp;N",
                              S_SLOT))
    sub.append(Paragraph("✎ Datum / Uhrzeit / Slot", S_SLOT))
    return [top, sub]


def _slot_cell(med, day_iso, slot, admin_map, blanko):
    """Eine kleine Mini-Zelle: ✓ wenn vergeben, sonst Quadrat zum Abhaken."""
    if med and slot in (
        # Schedule: nur Slots zeigen, die im Plan sind, sonst Strich
        "morgens" if med.get("morgens") else "",
        "mittags" if med.get("mittags") else "",
        "abends"  if med.get("abends")  else "",
        "nachts"  if med.get("nachts")  else "",
    ):
        if not blanko and med:
            rec = admin_map.get((med["id"], day_iso, slot))
            if rec:
                by = (rec.get("by_full_name") or rec.get("by_username")
                      or "").split(" ")[0][:7]
                return Paragraph(f"✓<br/><font size=5>{by}</font>", S_TICK)
        return Paragraph("☐", S_SLOT)
    # nicht im Schedule
    return Paragraph("·", S_SLOT)


def _med_grid(meds, days, admin_map, blanko):
    """Eine Tabelle mit pro Medikament 1 Zeile, in der die 7×4 Slot-
    Mini-Zellen und die BB-Zelle stehen. Wir nesten Sub-Tabellen pro Tag."""
    rows = _build_grid_header(days)

    # Bei Blanko füllen wir mind. 8 leere Zeilen für handschriftliche
    # Einträge. Sonst alle aktiven Medikamente.
    if blanko and not meds:
        meds_iter = [None] * 8
    else:
        meds_iter = list(meds) + [None] * max(0, 4 - len(meds))

    for med in meds_iter:
        if med:
            name = med["name"]
            dos = (med.get("dosage") or "").strip()
            lag = (med.get("lagerung") or "").strip()
            cell = []
            cell.append(Paragraph(f"<b>{name}</b>", S_TXT_BOLD))
            if dos:
                cell.append(Paragraph(dos, S_SMALL))
            if lag:
                cell.append(Paragraph(f"<i>Lagerung:</i> {lag}", S_SMALL))
        else:
            # Leere Zeile fürs Handschriftliche
            cell = [Paragraph("&nbsp;", S_TXT),
                    Paragraph("Dosierung: ______________", S_SMALL),
                    Paragraph("Lagerung: ______________", S_SMALL)]

        row = [cell]
        for d in days:
            day_iso = d.isoformat()
            slot_cells = []
            for slot in SLOTS_PLAN:
                slot_cells.append([_slot_cell(med, day_iso, slot,
                                               admin_map, blanko)])
            # Mini-Tabelle der 4 Slots nebeneinander
            slot_tbl = Table([[c[0] for c in slot_cells]],
                             colWidths=[7.0 * mm] * 4,
                             rowHeights=[10 * mm])
            slot_tbl.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 0.25, BORDER_SOFT),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, BORDER_SOFT),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 1),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]))
            row.append(slot_tbl)
        # Bei-Bedarf-Spalte: freies Feld
        row.append(Paragraph(
            ("__________________<br/>__________________<br/>"
             "__________________") if not med
            else ("__________________<br/>__________________"),
            S_SMALL))
        rows.append(row)

    col_widths = ([56 * mm]
                  + [30 * mm] * len(days)
                  + [38 * mm])
    tbl = Table(rows, colWidths=col_widths, repeatRows=2)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 1), BG_HEAD),
        ("TEXTCOLOR", (0, 0), (-1, 0), ACCENT_DARK),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, BORDER_SOFT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        # Erste 2 Zeilen sind Header
        ("ALIGN", (1, 0), (-1, 1), "CENTER"),
    ]))
    return tbl


def _legend_and_footer(patient_extra, exporter_label, blanko):
    extra = patient_extra or {}
    allerg = (extra.get("allergies_text") or "").strip()
    nk_n = (extra.get("emergency_contact_name") or "").strip()
    nk_p = (extra.get("emergency_contact_phone") or "").strip()
    nk_r = (extra.get("emergency_contact_relation") or "").strip()
    nk_str = " ".join(
        x for x in (nk_n,
                    f"({nk_r})" if nk_r else "",
                    f"Tel. {nk_p}" if nk_p else "") if x
    )
    parts = []
    if allerg:
        parts.append(Paragraph(
            f"<b>Allergien:</b> {allerg}", S_TXT))
    elif blanko:
        parts.append(Paragraph(
            "<b>Allergien:</b> _______________________________________", S_TXT))
    if nk_str:
        parts.append(Paragraph(
            f"<b>Notfallkontakt:</b> {nk_str}", S_TXT))
    elif blanko:
        parts.append(Paragraph(
            "<b>Notfallkontakt:</b> _________________________________", S_TXT))
    parts.append(Spacer(1, 4))
    parts.append(Paragraph(
        "<b>Bemerkungen / Beobachtungen:</b>", S_TXT_BOLD))
    parts.append(Paragraph(
        "<br/>".join(["_" * 110] * 4), S_SMALL))
    parts.append(Spacer(1, 6))
    parts.append(Paragraph(
        "<b>Lagerung allgemein:</b> "
        "______________________________________________________&nbsp; "
        "<b>Schlüssel-Ausgabe:</b> _______________", S_TXT))
    parts.append(Spacer(1, 6))
    parts.append(Paragraph(
        "<b>Quittiert von (Tag):</b> "
        "Mo ____________ Di ____________ Mi ____________ "
        "Do ____________ Fr ____________ Sa ____________ So ____________",
        S_SMALL))
    parts.append(Spacer(1, 4))
    parts.append(Paragraph(
        "Legende: <b>☐</b> noch nicht gegeben &nbsp;·&nbsp; "
        "<b>✓</b> gegeben (mit Initialen) &nbsp;·&nbsp; "
        "<b>·</b> nicht im Plan &nbsp;·&nbsp; "
        "<b>M</b> = morgens, <b>Mi</b> = mittags, "
        "<b>A</b> = abends, <b>N</b> = nachts", S_SMALL))
    return parts


def _on_page(canvas, doc, exporter_label):
    from datetime import datetime
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    txt = f"Erste Hilfe — Medikamenten-Plan"
    if exporter_label:
        txt += f"  ·  Exportiert von {exporter_label}"
    txt += f"  ·  {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    canvas.drawString(12 * mm, 8 * mm, txt)
    canvas.drawRightString(285 * mm, 8 * mm,
                            f"Seite {canvas.getPageNumber()}")
    canvas.restoreState()


def render_medication_plan_pdf(*, patient, medications, days,
                                admin_map, exporter_label=None,
                                patient_extra=None, blanko=False):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=10 * mm, rightMargin=10 * mm,
        topMargin=10 * mm, bottomMargin=14 * mm,
        title="Medikamentenplan",
    )
    story = []
    story.extend(_patient_header(patient, days, exporter_label, blanko))
    story.append(_med_grid(medications, days, admin_map, blanko))
    story.append(Spacer(1, 8))
    story.extend(_legend_and_footer(patient_extra, exporter_label, blanko))

    def on_page(canvas, doc_):
        _on_page(canvas, doc_, exporter_label)

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
