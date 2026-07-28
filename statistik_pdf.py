"""Statistik-Auswertung als kompaktes A4-PDF (in der Regel eine Seite).

Aufbau: Kennzahlen-Kacheln oben, darunter die Auswertungen als
Tabellen mit Anzahl, Prozentanteil und Mini-Balken.
"""
import io
from datetime import date as _date
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, Spacer, Table, TableStyle, SimpleDocTemplate, Flowable,
)

ACCENT = colors.HexColor("#7A1F2B")
ACCENT_DARK = colors.HexColor("#5A141D")
ACCENT_SOFT = colors.HexColor("#F6E8EA")
BORDER = colors.HexColor("#BBBBBB")
BORDER_SOFT = colors.HexColor("#E2E2E2")
MUTED = colors.HexColor("#666666")

S_TITLE = ParagraphStyle("T", fontName="Helvetica-Bold", fontSize=16,
                          leading=19, textColor=ACCENT_DARK)
S_META = ParagraphStyle("M", fontName="Helvetica", fontSize=8,
                         leading=10, textColor=MUTED)
S_SECTION = ParagraphStyle("S", fontName="Helvetica-Bold", fontSize=9.5,
                            leading=12, textColor=colors.white)
S_KPI_VAL = ParagraphStyle("KV", fontName="Helvetica-Bold", fontSize=17,
                            leading=19, alignment=1, textColor=ACCENT_DARK)
S_KPI_LBL = ParagraphStyle("KL", fontName="Helvetica", fontSize=6.8,
                            leading=8.5, alignment=1, textColor=MUTED)
S_CELL = ParagraphStyle("C", fontName="Helvetica", fontSize=8.5, leading=10.5)
S_CELL_R = ParagraphStyle("CR", fontName="Helvetica-Bold", fontSize=8.5,
                           leading=10.5, alignment=2)
S_NOTE = ParagraphStyle("N", fontName="Helvetica-Oblique", fontSize=7,
                         leading=9, textColor=MUTED)


class Bar(Flowable):
    """Schlanker horizontaler Balken (0–100 %)."""

    def __init__(self, pct, width, height=4.2):
        super().__init__()
        self.pct = max(0, min(100, pct or 0))
        self.width = width
        self.height = height

    def draw(self):
        c = self.canv
        c.setFillColor(BORDER_SOFT)
        c.roundRect(0, 0, self.width, self.height,
                    self.height / 2, stroke=0, fill=1)
        w = max(self.width * self.pct / 100, 1.2)
        c.setFillColor(ACCENT)
        c.roundRect(0, 0, w, self.height, self.height / 2, stroke=0, fill=1)


def _kpi_row(items, avail_w):
    """Kennzahlen-Kacheln: [(Wert, Label), …]"""
    cells = []
    for value, label in items:
        inner = Table([[Paragraph(str(value), S_KPI_VAL)],
                       [Paragraph(escape(label), S_KPI_LBL)]],
                      colWidths=["100%"])
        inner.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (0, 0), 4),
            ("BOTTOMPADDING", (0, 1), (0, 1), 4),
            ("TOPPADDING", (0, 1), (0, 1), 0),
        ]))
        cells.append(inner)
    col_w = avail_w / len(cells)
    tbl = Table([cells], colWidths=[col_w] * len(cells))
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT_SOFT),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return tbl


def _section(title, rows, avail_w, note=None, hint=None):
    """Auswertungs-Block: Titelbalken + Tabelle (Label | Balken | n | %)."""
    head = Table([[Paragraph(escape(title), S_SECTION)]],
                 colWidths=[avail_w])
    head.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    out = [head]
    if hint:
        out.append(Spacer(1, 1))
        out.append(Paragraph(escape(hint), S_NOTE))
    if not rows:
        body = Table([[Paragraph("keine Daten im Zeitraum", S_NOTE)]],
                     colWidths=[avail_w])
        body.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, BORDER_SOFT),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        out.append(body)
        return out

    label_w = avail_w * 0.42
    bar_w = avail_w * 0.34
    num_w = avail_w * 0.10
    pct_w = avail_w - label_w - bar_w - num_w
    data = []
    for label, n, pct, barpct in rows:
        data.append([
            Paragraph(escape(str(label)), S_CELL),
            Bar(barpct, bar_w - 6),
            Paragraph(str(n), S_CELL_R),
            Paragraph(f"{pct} %", S_CELL_R),
        ])
    body = Table(data, colWidths=[label_w, bar_w, num_w, pct_w])
    body.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_SOFT),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, BORDER_SOFT),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    out.append(body)
    if note:
        out.append(Paragraph(escape(note), S_NOTE))
    return out


def render_statistik_pdf(stats, event_name=None, exporter_label=None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=13 * mm, bottomMargin=14 * mm,
        title="Statistik-Auswertung",
    )
    avail_w = A4[0] - 28 * mm
    half_w = (avail_w - 5 * mm) / 2
    story = []

    story.append(Paragraph("Statistik — Auswertung Erste Hilfe", S_TITLE))
    meta = []
    if event_name:
        meta.append(f"Veranstaltung: {event_name}")
    if stats.get("date_from") or stats.get("date_to"):
        meta.append(f"Zeitraum: {stats.get('date_from') or '…'} bis "
                    f"{stats.get('date_to') or '…'}")
    else:
        meta.append("Zeitraum: gesamtes Event")
    meta.append(f"Stand: {_date.today().strftime('%d.%m.%Y')}")
    if exporter_label:
        meta.append(f"erstellt von {exporter_label}")
    story.append(Paragraph(" · ".join(escape(m) for m in meta), S_META))
    story.append(Spacer(1, 8))

    # Kennzahlen
    story.append(_kpi_row([
        (stats["behandlungen"], "Behandlungen gesamt"),
        (stats["zeh_count"], "zentral (zEH)"),
        (stats["deh_count"], "dezentral (dEH)"),
        (stats["triage_count"], "Triage-Anmeldungen"),
        (f"{stats['rettungs_quote']} %", "Rettung / Klinik"),
    ], avail_w))
    story.append(Spacer(1, 4))
    story.append(_kpi_row([
        (stats["rettung_klinik"], "Fälle Rettung / Klinik"),
        (stats["schnitt_pro_tag"], "Behandlungen / Tag"),
        (stats["nrs_schnitt"] if stats["nrs_schnitt"] is not None else "—",
         "Ø Schmerz (NRS)"),
        (stats["spitzenstunde"], "Spitzenstunde"),
        (stats["stärkster_tag"], "stärkster Tag"),
    ], avail_w))
    story.append(Spacer(1, 10))

    # Verbleib (Kernauswertung) — volle Breite
    for f in _section("Verbleib der Patienten", stats["verbleib"], avail_w,
                      note="Anteile bezogen auf zentrale Protokolle; "
                           "maßgeblich ist das erste Übergabe-Ziel."):
        story.append(f)
    story.append(Spacer(1, 8))

    # Zwei Spalten: Triage-Kategorien | Notfallarten
    left = _section("Sichtungskategorien (mSTaRT)", stats["triage_kat"],
                    half_w)
    right = _section("Notfallarten (Top 8)", stats["notfallart"], half_w,
                     hint="Mehrfachnennung möglich")
    two = Table([[left, right]], colWidths=[half_w + 2.5 * mm,
                                            half_w + 2.5 * mm])
    two.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (-1, 0), (-1, 0), 0),
    ]))
    story.append(two)
    story.append(Spacer(1, 8))

    left2 = _section("Maßnahmen (Top 8)", stats["massnahmen"], half_w,
                     hint="Mehrfachnennung möglich")
    right2 = _section("Übergabe-Ziele im Detail", stats["uebergabe"], half_w)
    two2 = Table([[left2, right2]], colWidths=[half_w + 2.5 * mm,
                                               half_w + 2.5 * mm])
    two2.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (-1, 0), (-1, 0), 0),
    ]))
    story.append(two2)
    story.append(Spacer(1, 8))

    for f in _section("Behandlungen pro Tag", stats["tage"], avail_w,
                      note="zentral + dezentral zusammengefasst"):
        story.append(f)

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(14 * mm, 8 * mm,
                          "Vertraulich — nur für das Sanitätsteam · "
                          "keine personenbezogenen Daten enthalten")
        canvas.drawRightString(A4[0] - 14 * mm, 8 * mm,
                                f"Seite {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
