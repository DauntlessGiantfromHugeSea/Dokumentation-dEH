"""
Notfallprotokoll als gerenderte PDF (kein AcroForm-Formular mehr).

Layout angelehnt an das DGUV-Einsatzprotokoll mit Sektionen 1-10.
Werte aus dem zentralen Bericht (data-Dict) werden direkt in die
Tabellenzellen gerendert. Am Ende — wenn vorhanden — eine
Unterschriften-Seite.
"""
import base64
import io
from datetime import datetime
from typing import Any

from pypdf import PdfReader, PdfWriter

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# ============================== Helpers ==============================

def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return "" if v is None else str(v)


def _join(values, sep=", "):
    if values is None:
        return ""
    if isinstance(values, list):
        return sep.join(str(x) for x in values
                        if x is not None and str(x).strip() != "")
    return str(values)


def _combine(primary, sonst, sep=", "):
    parts = []
    if primary:
        parts.append(_join(primary, sep))
    if sonst:
        parts.append(str(sonst))
    return sep.join(p for p in parts if p)


def _v(value, default=""):
    """Normalize a value to string for display in cells."""
    if value is None:
        return default
    if isinstance(value, list):
        out = ", ".join(str(x) for x in value if x not in (None, ""))
        return out or default
    s = str(value).strip()
    return s if s else default


def _fmt_date(value):
    if not value:
        return ""
    s = str(value).replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            return d.strftime("%d.%m.%Y")
        except ValueError:
            continue
    return str(value)


def _decode_data_url(data_url):
    if not data_url or not isinstance(data_url, str) or "," not in data_url:
        return None
    try:
        return base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        return None


def _is_valid_png(data):
    try:
        from PIL import Image as PILImage
        with PILImage.open(io.BytesIO(data)) as im:
            im.verify()
        with PILImage.open(io.BytesIO(data)) as im:
            im.load()
        return True
    except Exception:
        return False


# ============================== Styles ==============================

ACCENT = colors.HexColor("#7A1F2B")
SECTION_BG = colors.HexColor("#E8E8E8")
SUB_BG = colors.HexColor("#F4F4F4")
BORDER = colors.HexColor("#888888")
LIGHT_BORDER = colors.HexColor("#BBBBBB")

S_TITLE = ParagraphStyle("Title", fontName="Helvetica-Bold", fontSize=14,
                         leading=17, textColor=colors.black)
S_PATIENT = ParagraphStyle("Patient", fontName="Helvetica-Bold", fontSize=11,
                           leading=14, textColor=colors.black)
S_SECTION = ParagraphStyle("Sec", fontName="Helvetica-Bold", fontSize=9.5,
                           leading=11.5, textColor=colors.black)
S_SUBSEC = ParagraphStyle("Sub", fontName="Helvetica-Bold", fontSize=8.5,
                          leading=10, textColor=colors.black)
S_LABEL = ParagraphStyle("Label", fontName="Helvetica", fontSize=7,
                         leading=8.5, textColor=colors.HexColor("#444444"))
S_VAL = ParagraphStyle("Val", fontName="Helvetica-Bold", fontSize=9,
                       leading=11, textColor=colors.black)
S_TXT = ParagraphStyle("Txt", fontName="Helvetica", fontSize=8.5,
                       leading=11, textColor=colors.black)
S_SMALL = ParagraphStyle("Small", fontName="Helvetica", fontSize=7,
                         leading=9, textColor=colors.HexColor("#666666"))


def _p(text, style=S_TXT):
    text = (text or "").replace("\n", "<br/>") if isinstance(text, str) else text
    return Paragraph(text or "", style)


def _label_value(label, value, value_style=S_VAL, min_h=None):
    """Mini-Block: Label oben klein, Wert darunter."""
    inner = Table(
        [[_p(label, S_LABEL)], [_p(_v(value), value_style)]],
        colWidths=["100%"],
        rowHeights=[None, min_h] if min_h else None,
    )
    inner.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return inner


def _section_bar(title, width=None):
    """Graue Sektionsleiste mit fetter Schrift."""
    tbl = Table([[_p(title, S_SECTION)]], colWidths=[width or 186 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SECTION_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return tbl


def _subsection(title, width=None, bg=SUB_BG):
    """Hellgraue Unter-Sektionsleiste."""
    tbl = Table([[_p(title, S_SUBSEC)]], colWidths=[width or 186 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return tbl


def _bordered_table(rows, col_widths, padding=4, min_row_h=None, valign="TOP"):
    tbl = Table(rows, colWidths=col_widths,
                rowHeights=[min_row_h] * len(rows) if min_row_h else None)
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, LIGHT_BORDER),
        ("VALIGN", (0, 0), (-1, -1), valign),
        ("LEFTPADDING", (0, 0), (-1, -1), padding),
        ("RIGHTPADDING", (0, 0), (-1, -1), padding),
        ("TOPPADDING", (0, 0), (-1, -1), padding - 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), padding - 1),
    ]))
    return tbl


# =========================== Page sections ===========================

def _kasse_strip(d):
    """Krankenkassen-Auswahl AOK/LKK/BKK/IKK/vdek/TK/UV — aktive markiert."""
    kasse = (_v(d.get("krankenkasse")) or "").lower()
    items = [("AOK", "aok"), ("LKK", "lkk"), ("BKK", "bkk"),
             ("IKK", "ikk"), ("vdek", "vdek"), ("TK", "tk"),
             ("UV", "uv")]
    cells = []
    for label, key in items:
        is_active = key in kasse
        st = ParagraphStyle(
            f"K{key}",
            fontName="Helvetica-Bold" if is_active else "Helvetica",
            fontSize=8.5, alignment=1, leading=10,
            textColor=ACCENT if is_active else colors.black,
            backColor=colors.HexColor("#F6E8EA") if is_active else None,
        )
        cells.append(Paragraph(label, st))
    return _bordered_table([cells], [11.5 * mm] * 7, padding=3,
                           valign="MIDDLE")


def _patient_block(d):
    name_addr = ", ".join(p for p in [
        " ".join(p for p in [_v(d.get("vorname")), _v(d.get("nachname"))] if p),
        _v(d.get("strasse")),
        " ".join(p for p in [_v(d.get("plz")), _v(d.get("stadt"))] if p),
    ] if p)
    rows = [
        [_kasse_strip(d)],
        [_label_value("Krankenkasse bzw. Kostenträger", d.get("krankenkasse"))],
        [_label_value(
            "Name, Vorname, Adressdaten des Versicherten",
            name_addr, min_h=18 * mm,
        )],
        [_bordered_table(
            [[_label_value("Geschlecht", d.get("geschlecht")),
              _label_value("Geb. am", _fmt_date(d.get("geburtsdatum")))]],
            [44 * mm, 44 * mm], padding=3,
        )],
        [_bordered_table(
            [[_label_value("Kassen-Nr.", ""),
              _label_value("Versicherten-Nr.", ""),
              _label_value("Status", "")]],
            [29.3 * mm, 29.3 * mm, 29.4 * mm], padding=3,
        )],
        [_label_value("Telefonnummer", d.get("telefon"))],
    ]
    tbl = Table(rows, colWidths=[88 * mm])
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return tbl


def _einsatz_block(d):
    notfallart = _combine(d.get("notfallart"), d.get("notfallart_sonstige"))
    rows = [
        [_p("Rettungs-Einsatzprotokoll", S_PATIENT)],
        [_subsection("1. Rettungstechnische Daten", width=96 * mm)],
        [_label_value("Einsatznummer", d.get("einsatznummer"))],
        [_bordered_table(
            [[_label_value("Einsatzort", d.get("einsatzort"), min_h=10 * mm),
              Table([
                  [_label_value("Datum", _fmt_date(d.get("datum")))],
                  [_label_value("Stichwort", d.get("einsatzstichwort"))],
                  [_label_value("Einsatzbeginn", d.get("einsatzbeginn"))],
              ], colWidths=[40 * mm], style=TableStyle([
                  ("LEFTPADDING", (0, 0), (-1, -1), 0),
                  ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                  ("TOPPADDING", (0, 0), (-1, -1), 1),
                  ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
              ]))]],
            [48 * mm, 40 * mm], padding=3,
        )],
        [_bordered_table(
            [[_label_value("Einsatzkraft 1", d.get("einsatzkraft1")),
              _label_value("Alarm durch", d.get("alarm_durch"))]],
            [48 * mm, 40 * mm], padding=3,
        )],
        [_bordered_table(
            [[_label_value("Einsatzkraft 2", d.get("einsatzkraft2")),
              _label_value("Übergabezeit", d.get("uebergabezeit"))]],
            [48 * mm, 40 * mm], padding=3,
        )],
        [_bordered_table(
            [[_label_value("Einsatzende", d.get("einsatzende")),
              _label_value("Begleitung RTM", d.get("begleitung"))]],
            [48 * mm, 40 * mm], padding=3,
        )],
    ]
    tbl = Table(rows, colWidths=[96 * mm])
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return tbl


def _section_2_notfall(d):
    notfallart = _combine(d.get("notfallart"), d.get("notfallart_sonstige"))
    rows = [
        [_label_value("Notfallart", notfallart)],
        [_label_value("Notfallsituation", d.get("notfallsituation"),
                      min_h=18 * mm, value_style=S_TXT)],
        [_label_value("Verletzung / Verbrennung / Verbrühung / "
                      "Erfrierung / Verätzung / Erkrankung",
                      d.get("verletzung"), min_h=16 * mm,
                      value_style=S_TXT)],
    ]
    return _bordered_table(rows, [186 * mm], padding=4)


def _section_3_erstbefund(d):
    pupillen = "Re.: " + _v(d.get("pupille_l")) + " · Li.: " + _v(d.get("pupille_r"))
    if not _v(d.get("pupille_l")) and not _v(d.get("pupille_r")):
        pupillen = ""
    neuro_text = ""
    parts = []
    if _v(d.get("pupille_l")) or _v(d.get("pupille_r")):
        parts.append(f"Pupillen Re/Li: {_v(d.get('pupille_l')) or '—'} / {_v(d.get('pupille_r')) or '—'}")
    if _v(d.get("gcs_1")):
        parts.append(f"GCS: {_v(d.get('gcs_1'))}")
    neuro_text = "<br/>".join(parts)

    schmerzen = _v(d.get("schmerzen_grad_1"))
    nrs = _v(d.get("nrs_1"))

    # Messwerte 1
    mw_lines = []
    if _v(d.get("zeit_1")):
        mw_lines.append(f"<b>Zeit:</b> {_v(d.get('zeit_1'))}")
    pairs = [
        ("RR Sys (mmHg)", "rr_sys_1"), ("RR Dia (mmHg)", "rr_dia_1"),
        ("Puls (1/min)", "puls_1"), ("HF (1/min)", "hf_1"),
        ("BZ (mmol/l)", "bz_1"), ("AF (1/min)", "af_1"),
        ("etCO2 (mmHg)", "etco2_1"), ("SpO2 (%)", "spo2_1"),
        ("Temperatur (°C)", "temp_1"),
    ]
    for label, key in pairs:
        v = _v(d.get(key))
        if v:
            mw_lines.append(f"{label}: <b>{v}</b>")
    messwerte = "<br/>".join(mw_lines) if mw_lines else "—"

    psych = _combine(d.get("psyche"), d.get("psyche_sonstiges"))
    haut = _combine(d.get("haut"), d.get("haut_sonstiges"))

    return _bordered_table([
        [
            _label_value("3.1 Neurologie", neuro_text),
            _label_value("3.7 Schmerzen / NRS",
                         (f"{schmerzen}" if schmerzen else "")
                         + (f"\nNRS: {nrs}" if nrs else "")),
            _label_value("3.2 Messwerte (Erstbefund)",
                         Paragraph(messwerte, S_TXT) if mw_lines else "—",
                         value_style=S_TXT),
        ],
        [
            _label_value("3.3 Atmung", d.get("atmung_1")),
            "", "",
        ],
        [
            _label_value("3.4 Bewusstseinslage", d.get("bewusstsein_1")),
            "", _label_value("3.8 psychischer Zustand", psych),
        ],
        [
            _label_value("3.5 Kreislauf", d.get("kreislauf_1")),
            "", _label_value("3.9 Hautbefund", haut),
        ],
        [
            _label_value("3.6 EKG", d.get("ekg_1")),
            "", _label_value("3.10 Sonstiges", d.get("erstbefund_sonstiges")),
        ],
    ], [62 * mm, 32 * mm, 92 * mm], padding=4, min_row_h=14 * mm)


def _section_4_erstdiagnose(d):
    return _bordered_table(
        [[_label_value("4. Erstdiagnose", d.get("erstdiagnose"),
                       min_h=14 * mm)]],
        [186 * mm], padding=4,
    )


def _section_5_verlauf(d):
    return _bordered_table(
        [[_label_value("5. Verlauf", d.get("verlauf"), min_h=22 * mm,
                       value_style=S_TXT)]],
        [186 * mm], padding=4,
    )


def _section_6_massnahmen(d):
    massn = _combine(d.get("massnahme"), d.get("massnahmen_sonstiges"))
    return _bordered_table(
        [[_label_value("6. Maßnahmen", massn, min_h=22 * mm,
                       value_style=S_TXT)]],
        [186 * mm], padding=4,
    )


def _section_7_uebergabe(d):
    # 7.2 Messwerte (Übergabe)
    mw_lines = []
    if _v(d.get("zeit_2")):
        mw_lines.append(f"<b>Zeit:</b> {_v(d.get('zeit_2'))}")
    pairs = [
        ("RR Sys (mmHg)", "rr_sys_2"), ("RR Dia (mmHg)", "rr_dia_2"),
        ("Puls (1/min)", "puls_2"), ("HF (1/min)", "hf_2"),
        ("BZ (mmol/l)", "bz_2"), ("AF (1/min)", "af_2"),
        ("etCO2 (mmHg)", "etco2_2"), ("SpO2 (%)", "spo2_2"),
        ("Temperatur (°C)", "temp_2"),
    ]
    for label, key in pairs:
        v = _v(d.get(key))
        if v:
            mw_lines.append(f"{label}: <b>{v}</b>")
    if _v(d.get("nrs_2")):
        mw_lines.append(f"Schmerz NRS: <b>{_v(d.get('nrs_2'))}</b>")
    messwerte = "<br/>".join(mw_lines) if mw_lines else "—"

    zustand_parts = []
    if _v(d.get("bewusstsein_2")):
        zustand_parts.append(f"Zustand: {_v(d.get('bewusstsein_2'))}")
    if _v(d.get("gcs_2")):
        zustand_parts.append(f"GCS-Summe: {_v(d.get('gcs_2'))}")
    zustand_text = "\n".join(zustand_parts)

    return _bordered_table([
        [
            _label_value("7.1 Zustand", zustand_text),
            _label_value("7.2 Messwerte (Übergabe)",
                         Paragraph(messwerte, S_TXT) if mw_lines else "—",
                         value_style=S_TXT),
        ],
        [
            _label_value("7.3 Atmung", d.get("atmung_2")),
            "",
        ],
        [
            _label_value("7.4 Bewusstseinslage", d.get("bewusstsein_2")),
            _label_value("7.7 Übergabe an", d.get("uebergabe_an")),
        ],
        [
            _label_value("7.5 Kreislauf", d.get("kreislauf_2")),
            _label_value("7.8 Infektion", d.get("infektion")),
        ],
        [
            _label_value("7.6 EKG", d.get("ekg_2")),
            "",
        ],
    ], [93 * mm, 93 * mm], padding=4, min_row_h=11 * mm)


def _section_8_9(d):
    return _bordered_table([
        [
            _label_value("8. Ergebnis / Einsatzbeschreibung",
                         d.get("einsatzbeschreibung")
                         or d.get("ergebnis"),
                         min_h=18 * mm, value_style=S_TXT),
            _label_value("9. Abschluss — Ergebnis",
                         d.get("ergebnis"),
                         min_h=18 * mm, value_style=S_TXT),
        ]
    ], [93 * mm, 93 * mm], padding=4)


def _section_10_material(d):
    return _bordered_table(
        [[_label_value("10. Material — was muss nachgefüllt werden?",
                       d.get("material"), min_h=14 * mm,
                       value_style=S_TXT)]],
        [186 * mm], padding=4,
    )


# ============================ Top header ============================

def _top_header(d):
    """Titel-Zeile + Patient-Name."""
    vorname = _v(d.get("vorname"))
    nachname = _v(d.get("nachname"))
    name = " ".join(p for p in [vorname, nachname] if p) or "—"
    geb = _fmt_date(d.get("geburtsdatum"))
    patient_line = f"<b>Patient:</b> {name}" + (
        f" · geb. {geb}" if geb else "")
    return Table([
        [_p("Einsatzprotokoll", S_TITLE)],
        [Paragraph(patient_line, S_PATIENT)],
    ], colWidths=[186 * mm], style=TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))


# ============================ Signature page ============================

def _build_signature_page(data):
    sigs = []
    for n in (1, 2):
        sig_data = _decode_data_url(_first(data.get(f"signature_einsatzkraft{n}")))
        if not sig_data or not _is_valid_png(sig_data):
            continue
        sigs.append({
            "n": n, "img_bytes": sig_data,
            "name": _first(data.get(f"einsatzkraft{n}")),
            "at": _first(data.get(f"signature_einsatzkraft{n}_at")),
            "by": _first(data.get(f"signature_einsatzkraft{n}_by")),
        })
    if not sigs:
        return None

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    c.setFillColor(ACCENT)
    c.rect(0, height - 18 * mm, width, 18 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, height - 12 * mm, "Unterschriften")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, height - 16 * mm,
                 "Nachweis der durchgeführten Behandlung")
    c.setFillColor(colors.black)

    block_h = 60 * mm
    top = height - 35 * mm
    for i, s in enumerate(sigs):
        y_label = top - i * (block_h + 18 * mm)
        c.setFont("Helvetica-Bold", 11)
        label = f"Einsatzkraft {s['n']}"
        if s["name"]:
            label += f" — {s['name']}"
        c.drawString(20 * mm, y_label, label)
        img_y = y_label - block_h - 4 * mm
        c.setStrokeColor(colors.lightgrey)
        c.setLineWidth(0.5)
        c.rect(20 * mm, img_y, 150 * mm, block_h, stroke=1, fill=0)
        try:
            img = ImageReader(io.BytesIO(s["img_bytes"]))
            c.drawImage(img, 21 * mm, img_y + 1 * mm,
                        width=148 * mm, height=block_h - 2 * mm,
                        preserveAspectRatio=True, anchor="c", mask="auto")
        except Exception:
            pass
        meta_parts = []
        if s["at"]:
            meta_parts.append(f"am {s['at']}")
        if s["by"]:
            meta_parts.append(f"erfasst von {s['by']}")
        if meta_parts:
            c.setFont("Helvetica-Oblique", 8)
            c.setFillColor(colors.grey)
            c.drawString(20 * mm, img_y - 4 * mm, " · ".join(meta_parts))
            c.setFillColor(colors.black)

    c.setFont("Helvetica", 7)
    c.setFillColor(colors.grey)
    c.drawString(20 * mm, 12 * mm,
                 "Unterschriften wurden digital im Erste-Hilfe-Camp-System erfasst.")
    c.showPage()
    c.save()
    return buf.getvalue()


# ============================ Page footer ============================

def _make_page_footer(total_pages_label="von 2"):
    def _on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawRightString(
            190 * mm, 8 * mm,
            f"Seite {doc.page} {total_pages_label}",
        )
        canvas.restoreState()
    return _on_page


# ============================ Main ==============================

def render_pdf(data):
    """Rendert das Notfallprotokoll auf 2 A4-Seiten + ggf. Unterschriften.

    Erwartet das `data`-Dict eines zentralen Berichts (so wie es in
    central_protocols.data steht).
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=14 * mm,
        title="Einsatzprotokoll",
    )

    story = []
    # Header
    story.append(_top_header(data))
    story.append(Spacer(1, 4))

    # Top-Block: Patient + Einsatz nebeneinander
    top = Table(
        [[_patient_block(data), _einsatz_block(data)]],
        colWidths=[88 * mm, 96 * mm],
    )
    top.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 2),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(top)
    story.append(Spacer(1, 6))

    # 2. Notfallgeschehen
    story.append(_section_bar("2. Notfallgeschehen / Anamnese / Erstbefund"))
    story.append(_section_2_notfall(data))
    story.append(Spacer(1, 6))

    # 3. Erstbefund
    story.append(_section_bar("3. Erstbefund"))
    story.append(_section_3_erstbefund(data))

    # Page break
    story.append(PageBreak())

    # 4. Erstdiagnose
    story.append(_section_bar("4. Erstdiagnose"))
    story.append(_section_4_erstdiagnose(data))
    story.append(Spacer(1, 6))

    # 5. Verlauf
    story.append(_section_bar("5. Verlauf"))
    story.append(_section_5_verlauf(data))
    story.append(Spacer(1, 6))

    # 6. Maßnahmen
    story.append(_section_bar("6. Maßnahmen"))
    story.append(_section_6_massnahmen(data))
    story.append(Spacer(1, 6))

    # 7. Übergabe
    story.append(_section_bar("7. Übergabe"))
    story.append(_section_7_uebergabe(data))
    story.append(Spacer(1, 6))

    # 8/9 + 10
    story.append(_section_8_9(data))
    story.append(Spacer(1, 4))
    story.append(_section_10_material(data))

    on_page = _make_page_footer()
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    main_pdf = buf.getvalue()

    # Signaturseite anhängen
    sig_pdf = _build_signature_page(data)
    if not sig_pdf:
        return main_pdf

    main_reader = PdfReader(io.BytesIO(main_pdf))
    sig_reader = PdfReader(io.BytesIO(sig_pdf))
    writer = PdfWriter()
    for page in main_reader.pages:
        writer.add_page(page)
    for page in sig_reader.pages:
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
