"""Krankheits-Ausbruch-Liste als A4-PDF (z. B. Hand-Fuß-Mund).

Tabelle: Name | Geburtsdatum | Stamm | Temperatur | Bemerkung — plus
ein paar Leerzeilen für handschriftliche Ergänzungen.
"""
import io
from datetime import date as _date
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, Spacer, Table, TableStyle, SimpleDocTemplate,
)

ACCENT = colors.HexColor("#7A1F2B")
ACCENT_DARK = colors.HexColor("#5A141D")
BORDER = colors.HexColor("#999999")
BORDER_SOFT = colors.HexColor("#DDDDDD")
MUTED = colors.HexColor("#666666")

S_TITLE = ParagraphStyle("Title", fontName="Helvetica-Bold",
                          fontSize=15, leading=18, textColor=ACCENT_DARK)
S_SMALL = ParagraphStyle("Small", fontName="Helvetica",
                          fontSize=7.5, leading=9, textColor=MUTED)
S_HEAD = ParagraphStyle("Head", fontName="Helvetica-Bold",
                         fontSize=8.5, leading=10, textColor=colors.white)
S_CELL = ParagraphStyle("Cell", fontName="Helvetica",
                         fontSize=9, leading=11)
S_CELL_B = ParagraphStyle("CellB", fontName="Helvetica-Bold",
                           fontSize=9, leading=11)

EXTRA_BLANK_ROWS = 6


def _fmt_date(iso):
    if not iso:
        return ""
    try:
        return _date.fromisoformat(str(iso)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return str(iso)


def render_outbreak_pdf(outbreak, entries, exporter_label=None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=16 * mm,
        title=f"Ausbruchsliste {outbreak['name']}",
    )
    avail_w = A4[0] - 28 * mm
    story = []
    story.append(Paragraph(
        f"Krankheits-Liste: {escape(outbreak['name'])}", S_TITLE))
    meta = [f"Stand: {_date.today().strftime('%d.%m.%Y')}"]
    if exporter_label:
        meta.append(f"erstellt von {escape(exporter_label)}")
    meta.append(f"{len(entries)} Person(en)")
    if outbreak.get("status") == "geschlossen":
        meta.append("Status: geschlossen")
    story.append(Paragraph(" · ".join(meta), S_SMALL))
    story.append(Spacer(1, 10))

    header = [Paragraph(t, S_HEAD) for t in
              ("Name", "Geburtsdatum", "Stamm", "Temp.", "Bemerkung")]
    rows = [header]
    for e in entries:
        rows.append([
            Paragraph(escape(e["name"] or "—"), S_CELL_B),
            Paragraph(escape(_fmt_date(e["geburtsdatum"])), S_CELL),
            Paragraph(escape(e["stamm"] or "—"), S_CELL),
            Paragraph(escape(e["temperatur"] or ""), S_CELL),
            Paragraph(escape(e["bemerkung"] or ""), S_CELL),
        ])
    for _ in range(EXTRA_BLANK_ROWS):
        rows.append([Paragraph("", S_CELL)] * 5)

    col_w = [avail_w * f for f in (0.26, 0.14, 0.16, 0.10, 0.34)]
    row_heights = [None] * (len(entries) + 1) + [9 * mm] * EXTRA_BLANK_ROWS
    tbl = Table(rows, colWidths=col_w, repeatRows=1,
                rowHeights=row_heights)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, BORDER_SOFT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#FAF7F5")]),
    ]
    tbl.setStyle(TableStyle(style))
    story.append(tbl)

    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "Leerzeilen für handschriftliche Nachträge — bitte zeitnah ins "
        "System übertragen. Vertraulich — nur für das Sanitätsteam.",
        S_SMALL))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(14 * mm, 8 * mm,
                          f"Krankheits-Liste: {outbreak['name']}")
        canvas.drawRightString(A4[0] - 14 * mm, 8 * mm,
                                f"Seite {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()
