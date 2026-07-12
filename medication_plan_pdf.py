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
    period = f"{days[0].strftime('%d.%m.')} – {days[-1].strftime('%d.%m.%Y')}"

    title_para = Paragraph(
        "Medikamenten-Vergabe-Protokoll"
        + (" <font color='#888888'>(Blanko)</font>" if blanko else ""),
        S_TITLE,
    )
    sub_lines = []
    if name: sub_lines.append(f"<b>Patient/in:</b> {name}")
    else: sub_lines.append(
        "<b>Patient/in:</b> ______________________________________")
    if geb:
        try:
            d = _date.fromisoformat(str(geb)[:10])
            sub_lines.append(f"<b>Geb.:</b> {d.strftime('%d.%m.%Y')}")
        except Exception:
            sub_lines.append(f"<b>Geb.:</b> {geb}")
    else:
        sub_lines.append("<b>Geb.:</b> __________")
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
        sub.append(Paragraph("M&nbsp;&nbsp;Mi&nbsp;&nbsp;A&nbsp;&nbsp;N",
                              S_SLOT))
    sub.append(Paragraph("Datum / Uhrzeit", S_SLOT))
    return [top, sub]


def _slot_cell(med, day_iso, slot, admin_map, blanko):
    """Mini-Zelle pro Slot. Leer = noch nicht gegeben (die Zellen-Border
    selbst dient als ankreuzbares Kästchen). „✓" mit Initialen wenn schon
    vergeben. Strich „–" nur wenn klar nicht im Plan und KEIN Blanko."""
    # Blanko: alle Slots sind „leer & ankreuzbar" — die Zellenborder ist
    # der visuelle Kreuz-Kasten. Nichts rendern.
    if blanko or not med:
        return Paragraph("", S_SLOT)
    in_plan = bool(med.get(slot))
    if not in_plan:
        return Paragraph("–", S_SLOT)
    rec = admin_map.get((med["id"], day_iso, slot))
    if rec:
        by = (rec.get("by_full_name") or rec.get("by_username")
              or "").split(" ")[0][:7]
        return Paragraph(f"<b>✓</b><br/><font size=5>{by}</font>", S_TICK)
    # Plan vorhanden, aber noch nicht gegeben → leere Zelle (Border = Kasten)
    return Paragraph("", S_SLOT)


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
            # Leere Zeile fürs Handschriftliche — Underscores knapper,
            # damit sie in die 44mm-Spalte passen.
            cell = [Paragraph("____________________", S_TXT_BOLD),
                    Paragraph("Dosis: __________", S_SMALL),
                    Paragraph("Lager.: __________", S_SMALL)]

        row = [cell]
        for d in days:
            day_iso = d.isoformat()
            slot_cells = []
            for slot in SLOTS_PLAN:
                slot_cells.append([_slot_cell(med, day_iso, slot,
                                               admin_map, blanko)])
            # Mini-Tabelle der 4 Slots nebeneinander
            slot_tbl = Table([[c[0] for c in slot_cells]],
                             colWidths=[5.7 * mm] * 4,
                             rowHeights=[11 * mm])
            slot_tbl.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 1),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]))
            row.append(slot_tbl)
        # Bei-Bedarf-Spalte: freies Feld zum Eintragen
        row.append(Paragraph(
            "_____________<br/>_____________<br/>_____________",
            S_SMALL))
        rows.append(row)

    # Spaltenbreiten: A4 landscape = 297mm, minus 10mm Marge je Seite
    # → 277mm verfügbar. Med + 7×Tage + Bedarf muss exakt reinpassen.
    col_widths = ([44 * mm]                    # Medikament
                  + [28 * mm] * len(days)      # 7 Tage à 28mm = 196mm
                  + [37 * mm])                 # Bei Bedarf
    # Summe: 44 + 196 + 37 = 277mm ✓
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
        "<b>Legende:</b> leeres Kästchen = noch nicht gegeben "
        "&nbsp;·&nbsp; <b>✓</b> gegeben (mit Initialen) "
        "&nbsp;·&nbsp; <b>–</b> nicht im Plan "
        "&nbsp;·&nbsp; <b>M</b> = morgens, <b>Mi</b> = mittags, "
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


# ============ Medikamentenschein pro Stamm/Region (A4 portrait) ============

S_STAMM_TITLE = ParagraphStyle(
    "StammTitle", fontName="Helvetica-Bold", fontSize=15, leading=18,
    textColor=ACCENT_DARK)
S_PATIENT = ParagraphStyle(
    "PatientName", fontName="Helvetica-Bold", fontSize=10.5, leading=13,
    textColor=ACCENT_DARK)
S_CELL = ParagraphStyle("Cell", fontName="Helvetica", fontSize=8.5, leading=11)
S_CELL_B = ParagraphStyle("CellB", fontName="Helvetica-Bold",
                           fontSize=8.5, leading=11)
S_CELL_HEAD = ParagraphStyle(
    "CellHead", fontName="Helvetica-Bold", fontSize=8, leading=10,
    textColor=colors.white)


def _einnahme_text(row) -> str:
    """Einnahme-Slots als Klartext: 'morgens, abends' + 'bei Bedarf'."""
    parts = [SLOT_LONG[s].lower() for s in SLOTS_PLAN if row[s]]
    if row["bei_bedarf"]:
        parts.append("bei Bedarf")
    return ", ".join(parts) if parts else "—"


def _patient_med_table(meds, width):
    """Tabelle: Medikament | Dosierung | Einnahme | Lagerung | Hinweise."""
    header = [Paragraph(t, S_CELL_HEAD) for t in
              ("Medikament", "Dosierung", "Einnahme", "Lagerung", "Hinweise")]
    rows = [header]
    for m in meds:
        rows.append([
            Paragraph(str(m["med_name"] or "—"), S_CELL_B),
            Paragraph(str(m["dosage"] or "—"), S_CELL),
            Paragraph(_einnahme_text(m), S_CELL),
            Paragraph(str(m["lagerung"] or "—"), S_CELL),
            Paragraph(str(m["notes"] or "—"), S_CELL),
        ])
    col_w = [width * f for f in (0.24, 0.16, 0.20, 0.18, 0.22)]
    tbl = Table(rows, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, BORDER_SOFT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#FAF7F5")]),
    ]))
    return tbl


S_ANMELDUNG = ParagraphStyle(
    "Anmeldung", fontName="Helvetica", fontSize=8.5, leading=11,
    textColor=colors.HexColor("#333333"),
    backColor=colors.HexColor("#FFF8E8"),
    borderPadding=5, borderWidth=0.5,
    borderColor=colors.HexColor("#D8C58A"))


def render_stamm_medication_sheet(sheet_patients, *, stamm_filter=None,
                                    region_filter=None, region_order=None,
                                    exporter_label=None):
    """Medikamentenschein nach Region → Stamm.

    `sheet_patients` = Liste aus medication_sheet_patients(): pro Person
    {name, geburtsdatum, stamm, region, meds: [...], anmeldung_text}.
    Gruppiert zuerst nach Region (Reihenfolge aus `region_order`), dann
    nach Stamm; je Region eine neue Seite. Je Person die strukturierte
    Plan-Tabelle (Medikament/Dosierung/Einnahme/Lagerung/Hinweise) und —
    falls vorhanden — die Medikamenten-Angabe aus der Camp-Anmeldung
    (frodor, live abgeglichen) als gelber Block.
    """
    from collections import OrderedDict
    from xml.sax.saxutils import escape as _esc

    UNASSIGNED = "Ohne Region"
    # region -> (stamm -> [entries])
    grouped: "OrderedDict[str, OrderedDict]" = OrderedDict()
    for entry in sheet_patients:
        region = (entry.get("region") or UNASSIGNED).strip() or UNASSIGNED
        stamm = (entry.get("stamm") or "").strip()
        grouped.setdefault(region, OrderedDict()).setdefault(stamm, []).append(entry)

    # Regions-Reihenfolge: konfigurierte Reihenfolge zuerst, dann übrige,
    # "Ohne Region" ganz am Ende.
    ordered = [r for r in (region_order or []) if r in grouped]
    for r in grouped:
        if r not in ordered and r != UNASSIGNED:
            ordered.append(r)
    if UNASSIGNED in grouped and UNASSIGNED not in ordered:
        ordered.append(UNASSIGNED)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=16 * mm,
        title="Medikamentenschein",
    )
    avail_w = A4[0] - 28 * mm
    story = []
    today = _date.today().strftime("%d.%m.%Y")

    if not grouped:
        story.append(Paragraph("Medikamentenschein", S_STAMM_TITLE))
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            "Keine Medikamente erfasst"
            + (f" für Region „{_esc(region_filter)}“" if region_filter else "")
            + (f" für Stamm „{_esc(stamm_filter)}“" if stamm_filter else "")
            + ".", S_TXT))
    else:
        first = True
        for region in ordered:
            stamm_groups = grouped[region]
            if not first:
                story.append(PageBreak())
            first = False
            n_people = sum(len(v) for v in stamm_groups.values())
            story.append(Paragraph(
                f"Medikamentenschein — Region: {_esc(region)}",
                S_STAMM_TITLE))
            story.append(Paragraph(
                f"Stand: {today}"
                + (f" · erstellt von {_esc(exporter_label)}"
                   if exporter_label else "")
                + f" · {n_people} Person(en)", S_SMALL))
            story.append(Spacer(1, 8))
            for stamm, patients in stamm_groups.items():
                stamm_label = stamm if stamm else "ohne Stamm-Zuordnung"
                story.append(Paragraph(
                    f"Stamm: {_esc(stamm_label)}", S_PATIENT))
                story.append(Spacer(1, 4))
                for entry in patients:
                    geb = (entry.get("geburtsdatum") or "").strip()
                    if geb:
                        try:
                            geb = _date.fromisoformat(geb[:10]).strftime("%d.%m.%Y")
                        except ValueError:
                            pass
                    story.append(Paragraph(
                        "<b>" + _esc(entry.get("name") or "—") + "</b>"
                        + (f" · geb. {_esc(geb)}" if geb else ""),
                        S_TXT))
                    story.append(Spacer(1, 3))
                    if entry.get("meds"):
                        story.append(_patient_med_table(entry["meds"], avail_w))
                        story.append(Spacer(1, 4))
                    if (entry.get("anmeldung_text") or "").strip():
                        story.append(Paragraph(
                            "<b>Medikamente laut Camp-Anmeldung:</b> "
                            + _esc(entry["anmeldung_text"].strip()),
                            S_ANMELDUNG))
                    story.append(Spacer(1, 10))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(14 * mm, 8 * mm,
                          "Vertraulich — nur für das Sanitätsteam")
        canvas.drawRightString(A4[0] - 14 * mm, 8 * mm,
                                f"Seite {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
