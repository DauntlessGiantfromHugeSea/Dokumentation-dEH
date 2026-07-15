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
from xml.sax.saxutils import escape

from pypdf import PdfReader, PdfWriter

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    KeepTogether,
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
CONTENT_W = 186 * mm
INSET_CONTENT_W = 178 * mm

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
S_BOX_TXT = ParagraphStyle(
    "BoxTxt", parent=S_TXT,
    fontSize=8.2, leading=10.5,
    leftIndent=4,
    rightIndent=4,
    spaceBefore=3,
    spaceAfter=4,
)


def _p(text, style=S_TXT):
    text = (text or "").replace("\n", "<br/>") if isinstance(text, str) else text
    return Paragraph(text or "", style)


def _long_text_box(value):
    text = _v(value, "—")
    safe = escape(text).replace("\n", "<br/>")
    return Paragraph(safe, S_BOX_TXT)


def _label_value(label, value, value_style=S_VAL, min_h=None):
    """Mini-Block: Label oben klein, Wert darunter.
    Akzeptiert für `value` entweder einen String oder einen ReportLab-
    Flowable (Paragraph/Table) — wenn `wrap` vorhanden ist, wird der
    Flowable direkt eingesetzt, sonst durch `_v()` als String gerendert.
    `min_h` bleibt aus Kompatibilitätsgründen im Aufruf, darf aber keine
    feste Tabellenhöhe erzwingen: lange Freitexte müssen mitwachsen.
    """
    if hasattr(value, "wrap"):
        value_cell = value
    else:
        value_cell = _p(_v(value), value_style)
    inner = Table(
        [[_p(label, S_LABEL)], [value_cell]],
        colWidths=["100%"],
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
    tbl = Table([[_p(title, S_SECTION)]], colWidths=[width or CONTENT_W])
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
    tbl = Table([[_p(title, S_SUBSEC)]], colWidths=[width or CONTENT_W])
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


def _patient_inner(d):
    """Inneres Patient-Layout (ohne äußere Box — wird vom Combined-Container
    umrahmt)."""
    # Name auf Zeile 1, Adresse (Straße Nr, PLZ Ort) auf Zeile 2 —
    # gleiche Box, nur eigene Zeile darunter.
    full_name = " ".join(p for p in [_v(d.get("vorname")),
                                     _v(d.get("nachname"))] if p)
    addr = ", ".join(p for p in [
        _v(d.get("strasse")),
        " ".join(p for p in [_v(d.get("plz")), _v(d.get("stadt"))] if p),
    ] if p)
    if full_name and addr:
        name_addr_cell = Paragraph(
            f"<b>{escape(full_name)}</b><br/>{escape(addr)}", S_VAL)
    else:
        name_addr_cell = full_name or addr
    rows = [
        [_kasse_strip(d)],
        [_label_value("Krankenkasse bzw. Kostenträger", d.get("krankenkasse"))],
        [_label_value("Name, Vorname, Adressdaten des Versicherten",
                      name_addr_cell)],
        [_bordered_table(
            [[_label_value("Geschlecht", d.get("geschlecht")),
              _label_value("Geb. am", _fmt_date(d.get("geburtsdatum")))]],
            [42 * mm, 42 * mm], padding=3,
        )],
        [_bordered_table(
            [[_label_value("Kassen-Nr.", ""),
              _label_value("Versicherten-Nr.", ""),
              _label_value("Status", "")]],
            [28 * mm, 28 * mm, 28 * mm], padding=3,
        )],
        [_label_value("Telefonnummer", d.get("telefon"))],
    ]
    tbl = Table(rows, colWidths=[84 * mm])
    tbl.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LIGHT_BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return tbl


def _einsatz_inner(d):
    """Inneres Einsatz-Layout (ohne äußere Box)."""
    title_rows = Table([
        [_p("Rettungs-Einsatzprotokoll", S_PATIENT)],
        [_subsection("1. Rettungstechnische Daten", width=92 * mm)],
    ], colWidths=[92 * mm])
    title_rows.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    body = Table([
        [_label_value("Einsatznummer", d.get("einsatznummer")), ""],
        [_label_value("Einsatzort", d.get("einsatzort")),
         _label_value("Datum", _fmt_date(d.get("datum")))],
        [_label_value("Einsatzkraft 1", d.get("einsatzkraft1")),
         _label_value("Stichwort", d.get("einsatzstichwort"))],
        [_label_value("Einsatzkraft 2", d.get("einsatzkraft2")),
         _label_value("Einsatzbeginn", d.get("einsatzbeginn"))],
        [_label_value("Alarm durch", d.get("alarm_durch")),
         _label_value("Einsatzende", d.get("einsatzende"))],
        [_label_value("Übergabezeit", d.get("uebergabezeit")),
         _label_value("Begleitung RTM", d.get("begleitung"))],
    ], colWidths=[50 * mm, 42 * mm])
    body.setStyle(TableStyle([
        ("INNERGRID", (0, 0), (-1, -1), 0.4, LIGHT_BORDER),
        ("LINEABOVE", (0, 0), (-1, 0), 0.4, LIGHT_BORDER),
        ("SPAN", (0, 0), (1, 0)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    outer = Table([[title_rows], [body]], colWidths=[92 * mm])
    outer.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return outer


def _top_combined(d):
    """Patient links, Einsatz rechts — gemeinsame äußere Box mit
    vertikaler Trennlinie. Beide Hälften enden auf derselben Y-Achse."""
    pat = _patient_inner(d)
    ein = _einsatz_inner(d)
    tbl = Table([[pat, ein]], colWidths=[88 * mm, 98 * mm])
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("LINEAFTER", (0, 0), (0, 0), 0.5, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return tbl


# Alte Funktionen als Aliase behalten, falls noch verwendet
def _patient_block(d): return _patient_inner(d)
def _einsatz_block(d): return _einsatz_inner(d)


def _section_medical(medical_info):
    """Notfallrelevante medizinische Vorinfos für den Rettungsdienst —
    Allergien, Dauer-Medikation, Notfallkontakt. Wird über der
    Notfallgeschehen-Sektion gerendert, damit der Rettungsdienst sie
    sofort sieht."""
    m = medical_info or {}
    has_allergies = bool(m.get("has_allergies"))
    has_meds = bool(m.get("has_medications"))
    allergies = (m.get("allergies_text") or "").strip()
    meds = (m.get("medications_text") or "").strip()
    nk_name = (m.get("emergency_contact_name") or "").strip()
    nk_phone = (m.get("emergency_contact_phone") or "").strip()
    nk_rel = (m.get("emergency_contact_relation") or "").strip()
    nk_parts = []
    if nk_name: nk_parts.append(nk_name)
    if nk_rel: nk_parts.append(f"({nk_rel})")
    if nk_phone: nk_parts.append(f"Tel. {nk_phone}")
    nk = " ".join(nk_parts) or "— nicht hinterlegt —"
    allergies_str = (allergies or ("ja — Details nicht hinterlegt"
                     if has_allergies else "keine bekannt"))
    meds_str = (meds or ("ja — Details nicht hinterlegt"
                if has_meds else "keine"))
    rows = [
        [_label_value("Allergien", allergies_str,
                      min_h=10 * mm, value_style=S_TXT)],
        [_label_value("Dauer-Medikation", meds_str,
                      min_h=10 * mm, value_style=S_TXT)],
        [_label_value("Notfallkontakt (Angehörige/Eltern)", nk,
                      min_h=8 * mm, value_style=S_TXT)],
    ]
    return _bordered_table(rows, [186 * mm], padding=4)


def _body_chart_flowable(markers, *, width_mm=170, label="Verletzungslokalisation"):
    """Body-Chart als bordered Table-Zelle mit eigener Drawing-Subklasse.

    Liefert einen Flowable, der die SVG-Datei aus static/body_chart.svg
    rendert (falls vorhanden) und die übergebenen Marker als rote
    Kreise mit Nummer darüber legt. Fallback: einfache schematische
    Boxen mit „vorne"/„hinten".
    """
    from reportlab.platypus import Flowable
    import os

    class _BodyChartDraw(Flowable):
        def __init__(self, mks, w_mm):
            super().__init__()
            self.markers = mks or []
            self.w = w_mm * mm
            # Body-Chart-PNG ist 827×1170 (h/w ≈ 1.41). Wenn die Datei
            # einmal anders sein sollte: Aspect dynamisch nachholen.
            self.h = self.w * 1170 / 827
            try:
                from PIL import Image
                p = os.path.join(os.path.dirname(__file__),
                                  "static", "body_chart.png")
                if os.path.exists(p):
                    with Image.open(p) as im:
                        self.h = self.w * im.size[1] / im.size[0]
            except Exception:
                pass

        def wrap(self, availWidth, availHeight):
            return (self.w, self.h)

        def draw(self):
            c = self.canv
            # Bevorzugt: echte anatomische Vorlage als PNG/WebP.
            # Fallback: schematische Outline mit Canvas-Linien.
            drew_bg = False
            for fname in ("body_chart.png", "body_chart.jpg",
                           "body_chart.webp"):
                p = os.path.join(
                    os.path.dirname(__file__), "static", fname)
                if not os.path.exists(p):
                    continue
                try:
                    from reportlab.lib.utils import ImageReader
                    img = ImageReader(p)
                    c.drawImage(img, 0, 0, width=self.w, height=self.h,
                                 preserveAspectRatio=True, mask='auto')
                    drew_bg = True
                    break
                except Exception:
                    continue
            if not drew_bg:
                try:
                    # Versuche SVG via svglib (falls vorhanden)
                    svg_path = os.path.join(
                        os.path.dirname(__file__), "static",
                        "body_chart.svg")
                    if os.path.exists(svg_path):
                        from svglib.svglib import svg2rlg
                        from reportlab.graphics import renderPDF
                        drawing = svg2rlg(svg_path)
                        if drawing is not None:
                            scale = self.w / drawing.width
                            drawing.scale(scale, scale)
                            drawing.width *= scale
                            drawing.height *= scale
                            renderPDF.draw(drawing, c, 0, 0)
                            drew_bg = True
                except Exception:
                    pass
            if not drew_bg:
                # Fallback: zwei schematische Körper-Outlines aus
                # einfachen Canvas-Linien — kein svglib nötig.
                from reportlab.lib import colors as _col
                c.setStrokeColor(_col.HexColor("#222"))
                c.setFillColor(_col.HexColor("#FFFFFF"))
                c.setLineWidth(0.8)
                half = self.w / 2
                # Pro Seite (vorne / hinten) einen Outline
                for side_idx in range(2):
                    bx = side_idx * (half + 2)
                    bw = half - 4
                    label = "vorne" if side_idx == 0 else "hinten"
                    # Hintergrund-Karte
                    c.setStrokeColor(_col.HexColor("#CCC"))
                    c.rect(bx, 0, bw, self.h, stroke=1, fill=0)
                    # Body proportions
                    cx = bx + bw / 2
                    head_r = bw * 0.10
                    head_cy = self.h - head_r - 8
                    # Kopf
                    c.setStrokeColor(_col.HexColor("#222"))
                    c.setLineWidth(0.9)
                    c.circle(cx, head_cy, head_r, stroke=1, fill=0)
                    # Hals
                    neck_y = head_cy - head_r
                    c.line(cx - head_r * 0.6, neck_y,
                            cx - head_r * 0.6, neck_y - 6)
                    c.line(cx + head_r * 0.6, neck_y,
                            cx + head_r * 0.6, neck_y - 6)
                    # Torso (Schultern + Rumpf)
                    torso_top_y = neck_y - 6
                    torso_w = bw * 0.45
                    torso_h = self.h * 0.34
                    torso_bottom_y = torso_top_y - torso_h
                    # Schulter-Bogen
                    c.line(cx - torso_w/2, torso_top_y,
                            cx + torso_w/2, torso_top_y)
                    c.line(cx - torso_w/2, torso_top_y,
                            cx - torso_w/2 - 2, torso_top_y - 4)
                    c.line(cx + torso_w/2, torso_top_y,
                            cx + torso_w/2 + 2, torso_top_y - 4)
                    # Rumpf-Seiten
                    c.line(cx - torso_w/2 - 2, torso_top_y - 4,
                            cx - torso_w/2 - 2, torso_bottom_y)
                    c.line(cx + torso_w/2 + 2, torso_top_y - 4,
                            cx + torso_w/2 + 2, torso_bottom_y)
                    # Hüft-Linie
                    c.line(cx - torso_w/2 - 2, torso_bottom_y,
                            cx + torso_w/2 + 2, torso_bottom_y)
                    # Arme (Linien an den Seiten, leicht außen)
                    arm_x_l = cx - torso_w/2 - 2 - bw * 0.05
                    arm_x_r = cx + torso_w/2 + 2 + bw * 0.05
                    arm_top = torso_top_y - 2
                    arm_end_y = torso_bottom_y - 8
                    c.line(arm_x_l, arm_top, arm_x_l, arm_end_y)
                    c.line(arm_x_r, arm_top, arm_x_r, arm_end_y)
                    # Beine
                    leg_top = torso_bottom_y
                    leg_bottom = 14
                    leg_offset = torso_w * 0.18
                    c.line(cx - leg_offset, leg_top, cx - leg_offset, leg_bottom)
                    c.line(cx + leg_offset, leg_top, cx + leg_offset, leg_bottom)
                    # Beschriftung unten
                    c.setFont("Helvetica", 7)
                    c.setFillColor(_col.HexColor("#666"))
                    c.drawCentredString(cx, 4, label)
            # Marker drüberlegen
            from reportlab.lib import colors as _col2
            for idx, m in enumerate(self.markers, start=1):
                try:
                    mx = float(m["x"]) * self.w
                    my = (1 - float(m["y"])) * self.h
                except (KeyError, ValueError, TypeError):
                    continue
                r = 3.5
                c.setFillColor(_col2.HexColor("#D32F2F"))
                c.setStrokeColor(_col2.white)
                c.setLineWidth(0.8)
                c.circle(mx, my, r, stroke=1, fill=1)
                c.setFillColor(_col2.white)
                c.setFont("Helvetica-Bold", 6.5)
                c.drawCentredString(mx, my - 2, str(idx))

    return _BodyChartDraw(markers, width_mm)


def _parse_body_markers(value):
    """Body-Marker liegen in data als JSON-String oder Liste vor."""
    if not value: return []
    if isinstance(value, list): return value
    try:
        import json as _j
        parsed = _j.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


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


def _messwerte_para(d, suffix):
    """Compact key/value list of vital signs as a single Paragraph."""
    lines = []
    if _v(d.get(f"zeit_{suffix}")):
        lines.append(f"<b>Zeit:</b> {_v(d.get(f'zeit_{suffix}'))}")
    rr_sys = _v(d.get(f"rr_sys_{suffix}"))
    rr_dia = _v(d.get(f"rr_dia_{suffix}"))
    if rr_sys or rr_dia:
        lines.append(f"<b>RR:</b> {rr_sys or '?'}/{rr_dia or '?'} mmHg")
    if _v(d.get(f"puls_{suffix}")):
        lines.append(f"<b>Puls:</b> {_v(d.get(f'puls_{suffix}'))}/min")
    af = _v(d.get(f"af_{suffix}"))
    hf = _v(d.get(f"hf_{suffix}"))
    af_hf = []
    if af: af_hf.append(f"AF {af}")
    if hf: af_hf.append(f"HF {hf}")
    if af_hf:
        lines.append("<b>" + " &middot; ".join(af_hf) + "</b>")
    if _v(d.get(f"spo2_{suffix}")):
        lines.append(f"<b>SpO2:</b> {_v(d.get(f'spo2_{suffix}'))}%")
    if _v(d.get(f"etco2_{suffix}")):
        lines.append(f"<b>etCO2:</b> {_v(d.get(f'etco2_{suffix}'))}")
    if _v(d.get(f"bz_{suffix}")):
        lines.append(f"<b>BZ:</b> {_v(d.get(f'bz_{suffix}'))} mmol/l")
    if _v(d.get(f"temp_{suffix}")):
        lines.append(f"<b>Temp:</b> {_v(d.get(f'temp_{suffix}'))}°C")
    if _v(d.get(f"gcs_{suffix}")):
        lines.append(f"<b>GCS:</b> {_v(d.get(f'gcs_{suffix}'))}")
    if not lines:
        return Paragraph("—", S_TXT)
    return Paragraph("<br/>".join(lines), S_TXT)


def _section_3_erstbefund(d):
    parts = []
    if _v(d.get("pupille_l")) or _v(d.get("pupille_r")):
        parts.append(f"Pupillen Re/Li: {_v(d.get('pupille_l')) or '—'} / "
                     f"{_v(d.get('pupille_r')) or '—'}")
    if _v(d.get("gcs_1")):
        parts.append(f"GCS: {_v(d.get('gcs_1'))}")
    neuro = Paragraph("<br/>".join(parts), S_VAL) if parts else _v("")

    schmerzen = _v(d.get("schmerzen_grad_1"))
    nrs = _v(d.get("nrs_1"))
    schmerz_lines = []
    if schmerzen:
        schmerz_lines.append(f"<b>{schmerzen}</b>")
    if nrs:
        schmerz_lines.append(f"NRS: <b>{nrs}</b>")
    schmerz = (Paragraph("<br/>".join(schmerz_lines), S_TXT)
               if schmerz_lines else "")

    psych = _combine(d.get("psyche"), d.get("psyche_sonstiges"))
    haut = _combine(d.get("haut"), d.get("haut_sonstiges"))

    return _bordered_table([
        [
            _label_value("3.1 Neurologie", neuro),
            _label_value("3.7 Schmerzen / NRS", schmerz),
            _label_value("3.2 Messwerte (Erstbefund)",
                         _messwerte_para(d, "1")),
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
    ], [62 * mm, 32 * mm, 92 * mm], padding=4)


def _section_4_erstdiagnose(d):
    # Neues Feld 'verdachtsdiagnose'; alte Protokolle haben noch
    # 'erstdiagnose' — Fallback, damit nichts verloren geht.
    value = _v(d.get("verdachtsdiagnose")) or _v(d.get("erstdiagnose"))
    return _bordered_table(
        [[_label_value("4. Verdachtsdiagnose(n)", value,
                       min_h=14 * mm)]],
        [186 * mm], padding=4,
    )


def _vital_trend_table(d, width=CONTENT_W):
    """Vergleicht Vitalwerte Erstbefund ↔ Übergabe nebeneinander, sodass
    der Trend auf einen Blick erkennbar ist (RR, Puls, AF, SpO2, etc.)."""
    columns = [
        ("Zeitpunkt", None, None, None),
        ("Zeit",      "zeit_1",   "zeit_2",   None),
        ("RR",        "rr_sys_1", "rr_sys_2", "_rr"),  # special
        ("Puls",      "puls_1",   "puls_2",   "/min"),
        ("AF",        "af_1",     "af_2",     "/min"),
        ("HF",        "hf_1",     "hf_2",     "/min"),
        ("SpO2",      "spo2_1",   "spo2_2",   "%"),
        ("etCO2",     "etco2_1",  "etco2_2",  None),
        ("BZ",        "bz_1",     "bz_2",     "mmol/l"),
        ("Temp",      "temp_1",   "temp_2",   "°C"),
        ("GCS",       "gcs_1",    "gcs_2",    None),
        ("NRS",       "nrs_1",    "nrs_2",    None),
    ]

    def _cell(d, k1, k2, suffix, unit):
        if k1 is None:
            return ""
        # Special-case RR: combine sys/dia
        if unit == "_rr":
            sys_v = _v(d.get(f"rr_sys_{suffix}"))
            dia_v = _v(d.get(f"rr_dia_{suffix}"))
            if not sys_v and not dia_v:
                return "—"
            return f"{sys_v or '?'}/{dia_v or '?'}"
        v = _v(d.get(k1 if suffix == "1" else k2))
        if not v:
            return "—"
        return v

    header_cells = [_p(col[0], S_LABEL) for col in columns]

    erst_cells = [_p("<b>Erstbefund</b>", S_TXT)]
    ueber_cells = [_p("<b>Übergabe</b>", S_TXT)]
    for label, k1, k2, unit in columns[1:]:
        erst_cells.append(_p(_cell(d, k1, k2, "1", unit), S_TXT))
        ueber_cells.append(_p(_cell(d, k1, k2, "2", unit), S_TXT))

    rows = [header_cells, erst_cells, ueber_cells]

    # Equal-ish widths inside the available box width. Do not use the full
    # page width when nested in a padded cell, otherwise the table spills out.
    n = len(columns)
    first_w = 20 * mm
    rest_w = (width - first_w) / (n - 1)
    col_widths = [first_w] + [rest_w] * (n - 1)

    tbl = Table(rows, colWidths=col_widths, hAlign="LEFT")
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SUB_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, LIGHT_BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
    ]))
    return tbl


def _section_5_verlauf(d):
    """Section 5: Freitext-Verlauf + Vital-Trend-Tabelle.

    Der Verlauf ist bewusst ein eigener Flowable. So kann ein langer Text
    die Höhe selbst bestimmen und die Vitalwerte rutschen sauber nach unten
    oder auf die nächste Seite, statt sich zu überlagern.
    """
    return [
        _long_text_box(d.get("verlauf")),
        Spacer(1, 3),
        _subsection("Verlauf der Vitalwerte", width=CONTENT_W),
        _vital_trend_table(d, width=CONTENT_W),
    ]


def _section_6_massnahmen(d):
    massn = _combine(d.get("massnahme"), d.get("massnahmen_sonstiges"))
    return _bordered_table(
        [[_label_value("6. Maßnahmen", massn, min_h=22 * mm,
                       value_style=S_TXT)]],
        [186 * mm], padding=4,
    )


def _section_7_uebergabe(d):
    zustand_lines = []
    if _v(d.get("bewusstsein_2")):
        zustand_lines.append(f"Zustand: <b>{_v(d.get('bewusstsein_2'))}</b>")
    if _v(d.get("gcs_2")):
        zustand_lines.append(f"GCS-Summe: <b>{_v(d.get('gcs_2'))}</b>")
    if _v(d.get("nrs_2")):
        zustand_lines.append(f"Schmerz NRS: <b>{_v(d.get('nrs_2'))}</b>")
    zustand = (Paragraph("<br/>".join(zustand_lines), S_TXT)
               if zustand_lines else "")

    return _bordered_table([
        [
            _label_value("7.1 Zustand", zustand),
            _label_value("7.2 Messwerte (Übergabe)",
                         _messwerte_para(d, "2")),
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
            _label_value("7.9 Wiedervorstellung",
                         (" · ".join(p for p in [
                             _fmt_date(d.get("wiedervorstellung_datum")),
                             _v(d.get("wiedervorstellung_zeit")),
                         ] if p)
                          if _v(d.get("wiedervorstellung_datum")) else "")),
        ],
    ], [93 * mm, 93 * mm], padding=4)


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

def _draw_footer(canvas, data, exporter_label, page_num, protocol_uid=None):
    """Identischer Footer wie auf den Hauptseiten — auf jede Seite drauf.
    Optional: Code128-Barcode (Protokoll-UID) für den Admin-Scanner."""
    sig1_signed = bool(_decode_data_url(_first(data.get("signature_einsatzkraft1"))))
    sig2_signed = bool(_decode_data_url(_first(data.get("signature_einsatzkraft2"))))
    sig1_name = _first(data.get("einsatzkraft1")) or "Einsatzkraft 1"
    sig2_name = _first(data.get("einsatzkraft2")) or "Einsatzkraft 2"
    signed = []
    if sig1_signed: signed.append(sig1_name)
    if sig2_signed: signed.append(sig2_name)
    sig_text = ("Unterschrieben: " + " · ".join(signed)) if signed else "Noch nicht unterschrieben"

    exported_at = datetime.now().strftime("%d.%m.%Y %H:%M")
    exporter_text = (f"Exportiert von {exporter_label} · {exported_at}"
                     if exporter_label
                     else f"Exportiert {exported_at}")

    canvas.saveState()
    # Barcode unten links (über dem Footer-Text) wenn protocol_uid gegeben
    if protocol_uid:
        _draw_barcode(canvas, protocol_uid, x_mm=12, y_mm=14, w_mm=60, h_mm=8)
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(colors.grey)
    canvas.setStrokeColor(colors.HexColor("#CCCCCC"))
    canvas.setLineWidth(0.3)
    canvas.line(12 * mm, 11 * mm, 198 * mm, 11 * mm)
    canvas.drawString(12 * mm, 7 * mm, sig_text[:90])
    canvas.drawCentredString(105 * mm, 7 * mm, exporter_text[:80])
    canvas.drawRightString(198 * mm, 7 * mm, f"Seite {page_num}")
    canvas.restoreState()


def _build_signature_page(data, exporter_label=None, page_num=3,
                            protocol_uid=None):
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

    box_w = 105 * mm
    box_h = 28 * mm
    top = height - 35 * mm
    for i, s in enumerate(sigs):
        y_label = top - i * (box_h + 18 * mm)
        c.setFont("Helvetica-Bold", 11)
        label = f"Einsatzkraft {s['n']}"
        if s["name"]:
            label += f" — {s['name']}"
        c.drawString(20 * mm, y_label, label)
        img_y = y_label - box_h - 4 * mm
        c.setStrokeColor(colors.lightgrey)
        c.setLineWidth(0.5)
        c.rect(20 * mm, img_y, box_w, box_h, stroke=1, fill=0)
        try:
            img = ImageReader(io.BytesIO(s["img_bytes"]))
            c.drawImage(img, 21 * mm, img_y + 1 * mm,
                        width=box_w - 2 * mm, height=box_h - 2 * mm,
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

    # Footer wie auf Hauptseiten
    _draw_footer(c, data, exporter_label, page_num, protocol_uid=protocol_uid)

    c.showPage()
    c.save()
    return buf.getvalue()


# ============================ Page footer ============================

def _make_page_footer(data, exporter_label=None, protocol_uid=None):
    """Footer auf jeder Seite: links Unterschriften-Status, mittig
    Export-Info, rechts Seitenzahl. Optional: Barcode unten links."""
    sig1_signed = bool(_decode_data_url(_first(data.get("signature_einsatzkraft1"))))
    sig2_signed = bool(_decode_data_url(_first(data.get("signature_einsatzkraft2"))))
    sig1_name = _first(data.get("einsatzkraft1")) or "Einsatzkraft 1"
    sig2_name = _first(data.get("einsatzkraft2")) or "Einsatzkraft 2"
    signed_names = []
    if sig1_signed: signed_names.append(sig1_name)
    if sig2_signed: signed_names.append(sig2_name)
    if signed_names:
        sig_text = "Unterschrieben: " + " · ".join(signed_names)
    else:
        sig_text = "Noch nicht unterschrieben"

    exported_at = datetime.now().strftime("%d.%m.%Y %H:%M")
    exporter_text = (f"Exportiert von {exporter_label} · {exported_at}"
                     if exporter_label
                     else f"Exportiert {exported_at}")

    def _on_page(canvas, doc):
        canvas.saveState()
        # Barcode (Protokoll-UID) unten links — admin-only scanner
        if protocol_uid:
            _draw_barcode(canvas, protocol_uid,
                            x_mm=12, y_mm=14, w_mm=60, h_mm=8)
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(colors.grey)
        # Trenn-Linie über dem Footer
        canvas.setStrokeColor(colors.HexColor("#CCCCCC"))
        canvas.setLineWidth(0.3)
        canvas.line(12 * mm, 11 * mm, 198 * mm, 11 * mm)
        # Links: Unterschriften (über dem Barcode)
        canvas.drawString(12 * mm, 7 * mm, sig_text[:90])
        # Mittig: Exporter + Zeitstempel
        canvas.drawCentredString(105 * mm, 7 * mm, exporter_text[:80])
        # Rechts: Seite n
        canvas.drawRightString(198 * mm, 7 * mm, f"Seite {doc.page}")
        canvas.restoreState()
    return _on_page


# ============================ Main ==============================

def _draw_barcode(canvas, value, *, x_mm=12, y_mm=14, w_mm=28, h_mm=4):
    """Schmaler Code128-Barcode unten am PDF (admin-Scanner). Wird
    kompakt links unten platziert — Klartext direkt darunter in 5pt."""
    if not value:
        return
    try:
        from reportlab.graphics.barcode import code128
        bc = code128.Code128(str(value), barHeight=h_mm * mm,
                              barWidth=0.28 * mm, humanReadable=False)
        bc_w = bc.width
        scale = (w_mm * mm) / bc_w if bc_w else 1.0
        canvas.saveState()
        canvas.translate(x_mm * mm, y_mm * mm)
        canvas.scale(scale, 1.0)
        bc.drawOn(canvas, 0, 0)
        canvas.restoreState()
        # Klartext darunter (klein)
        canvas.setFont("Helvetica", 5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(x_mm * mm, (y_mm - 1.5) * mm, str(value))
    except Exception:
        pass


def _build_attachment_pages(attachments, protocol_uid=None):
    """Anhang-Seiten: Bilder je auf eigener A4-Seite (eingepasst),
    sonstige Dateien als Liste. Angehängte PDFs werden vom Aufrufer
    seitenweise gemerged. Gibt PDF-Bytes oder None zurück."""
    images = [a for a in attachments
              if (a.get("mime") or "").startswith("image/")]
    others = [a for a in attachments
              if not (a.get("mime") or "").startswith("image/")
              and (a.get("mime") or "") != "application/pdf"]
    if not images and not others:
        return None
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    def _header(title):
        c.setFillColor(ACCENT)
        c.rect(0, height - 14 * mm, width, 14 * mm, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(14 * mm, height - 9 * mm, "Anhang zum Notfallprotokoll"
                     + (f" {protocol_uid}" if protocol_uid else ""))
        c.setFont("Helvetica", 8)
        c.drawRightString(width - 14 * mm, height - 9 * mm, title)

    for i, a in enumerate(images, start=1):
        _header(f"Foto {i} von {len(images)} — {a.get('filename', '')[:60]}")
        try:
            img = ImageReader(io.BytesIO(a["content"]))
            iw, ih = img.getSize()
            avail_w = width - 28 * mm
            avail_h = height - 40 * mm
            scale = min(avail_w / iw, avail_h / ih, 1.0)
            dw, dh = iw * scale, ih * scale
            c.drawImage(img, (width - dw) / 2,
                        (height - 20 * mm - dh) - ((avail_h - dh) / 2),
                        width=dw, height=dh,
                        preserveAspectRatio=True, mask="auto")
        except Exception:
            c.setFillColor(colors.black)
            c.setFont("Helvetica", 10)
            c.drawString(14 * mm, height - 30 * mm,
                         f"Bild konnte nicht gerendert werden: "
                         f"{a.get('filename', '?')}")
        c.showPage()

    if others:
        _header("Weitere Anhänge")
        c.setFillColor(colors.black)
        y = height - 26 * mm
        c.setFont("Helvetica-Bold", 10)
        c.drawString(14 * mm, y, "Nicht darstellbare Anhänge "
                     "(digital im System hinterlegt):")
        y -= 8 * mm
        c.setFont("Helvetica", 9)
        for a in others:
            size_kb = (a.get("size_bytes") or 0) // 1024
            c.drawString(16 * mm, y,
                         f"• {a.get('filename', '?')} "
                         f"({a.get('mime', '?')}, {size_kb} KB)")
            y -= 6 * mm
            if y < 20 * mm:
                c.showPage()
                _header("Weitere Anhänge (Fortsetzung)")
                y = height - 26 * mm
                c.setFillColor(colors.black)
                c.setFont("Helvetica", 9)
        c.showPage()

    c.save()
    return buf.getvalue()


def render_pdf(data, exporter_label=None, medical_info=None,
               protocol_uid=None, attachments=None):
    """Rendert das Notfallprotokoll auf 2 A4-Seiten + ggf. Unterschriften.

    Erwartet das `data`-Dict eines zentralen Berichts (so wie es in
    central_protocols.data steht). `exporter_label` erscheint im Fuß
    jeder Seite zusammen mit Unterschriften-Status und Zeitstempel.

    `medical_info` (optional) enthält Allergien, Medikamente und
    Notfallkontakt aus der Patientenakte — wird oberhalb von Sektion 2
    eingefügt, damit der Rettungsdienst die Infos sofort sieht.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=16 * mm,
        title="Einsatzprotokoll",
    )

    story = []
    # Header
    story.append(_top_header(data))
    story.append(Spacer(1, 4))

    # Top-Block: Patient + Einsatz in gemeinsamer Box
    story.append(_top_combined(data))
    story.append(Spacer(1, 6))

    # Medizinische Vorinfos (Allergien / Medikamente / Notfallkontakt)
    # — direkt sichtbar für den Rettungsdienst.
    if medical_info:
        story.append(_section_bar(
            "Vorinfos aus der Patientenakte"
        ))
        story.append(_section_medical(medical_info))
        story.append(Spacer(1, 6))

    # 2. Notfallgeschehen — KeepTogether sichert das Sektion nicht
    # in der Mitte der Tabelle umbricht.
    story.append(KeepTogether([
        _section_bar("2. Notfallgeschehen / Anamnese / Erstbefund"),
        _section_2_notfall(data),
        Spacer(1, 4),
    ]))

    # Verletzungslokalisation (Body-Chart) — nur wenn Marker vorhanden,
    # eigene Seite damit sie die folgenden Sektionen nicht ans Ende drückt.
    markers = _parse_body_markers(data.get("body_markers"))
    if markers:
        from reportlab.platypus import Table as _Tab, TableStyle as _TS
        marker_lines = "<br/>".join(
            f"<b>{i + 1}.</b> {'hinten' if m.get('side') == 'back' else 'vorne'}"
            f"{(' — ' + escape(str(m.get('note', '')))) if m.get('note') else ''}"
            for i, m in enumerate(markers)
        )
        body_w = 80
        list_w = 100
        tbl = _Tab([
            [_body_chart_flowable(markers, width_mm=body_w),
             Paragraph(marker_lines, S_TXT)],
        ], colWidths=[body_w * mm, list_w * mm])
        tbl.setStyle(_TS([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(KeepTogether([
            _section_bar("Verletzungslokalisation"),
            tbl,
            Spacer(1, 6),
        ]))

    # 3. Erstbefund — als Block zusammenhalten
    story.append(KeepTogether([
        _section_bar("3. Erstbefund"),
        _section_3_erstbefund(data),
    ]))

    # Page break
    story.append(PageBreak())

    # 4. Verdachtsdiagnose
    story.append(KeepTogether([
        _section_bar("4. Verdachtsdiagnose(n)"),
        _section_4_erstdiagnose(data),
        Spacer(1, 6),
    ]))

    # 5. Verlauf (kann mehrere Flowables enthalten)
    story.append(KeepTogether(
        [_section_bar("5. Verlauf")] + list(_section_5_verlauf(data))
        + [Spacer(1, 6)]
    ))

    # 6. Maßnahmen
    story.append(KeepTogether([
        _section_bar("6. Maßnahmen"),
        _section_6_massnahmen(data),
        Spacer(1, 6),
    ]))

    # 7. Übergabe
    story.append(KeepTogether([
        _section_bar("7. Übergabe"),
        _section_7_uebergabe(data),
        Spacer(1, 6),
    ]))

    # 8/9 + 10 — können zusammen
    story.append(KeepTogether([
        _section_8_9(data),
        Spacer(1, 4),
        _section_10_material(data),
    ]))

    on_page = _make_page_footer(data, exporter_label=exporter_label,
                                  protocol_uid=protocol_uid)
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    main_pdf = buf.getvalue()

    main_reader = PdfReader(io.BytesIO(main_pdf))
    n_main = len(main_reader.pages)

    # Signaturseite anhängen — Seitenzahl folgt auf Hauptseiten
    sig_pdf = _build_signature_page(data, exporter_label=exporter_label,
                                     protocol_uid=protocol_uid,
                                     page_num=n_main + 1)

    writer = PdfWriter()
    for page in main_reader.pages:
        writer.add_page(page)
    if sig_pdf:
        for page in PdfReader(io.BytesIO(sig_pdf)).pages:
            writer.add_page(page)

    # Anhänge hinten dran: Bilder + Datei-Liste als gerenderte Seiten,
    # angehängte PDFs werden seitenweise übernommen.
    attachments = attachments or []
    att_pdf = _build_attachment_pages(attachments, protocol_uid=protocol_uid)
    if att_pdf:
        for page in PdfReader(io.BytesIO(att_pdf)).pages:
            writer.add_page(page)
    for a in attachments:
        if (a.get("mime") or "") == "application/pdf":
            try:
                for page in PdfReader(io.BytesIO(a["content"])).pages:
                    writer.add_page(page)
            except Exception:
                pass  # defektes PDF — bleibt digital im System abrufbar

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
