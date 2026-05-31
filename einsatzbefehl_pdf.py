"""Einsatzbefehl + Einsatztagebuch als A4-Portrait PDF.

Beide Renderer sind eigenständig (Canvas, low-level). Optik im
Bordeaux-Akzent des Tools, klare Labels oberhalb der Felder, dezente
Sektions-Header.
"""

import io
from datetime import datetime as _dt

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas

# Tool-Farbpalette (an --accent etc. aus style.css angelehnt)
ACCENT = colors.HexColor("#7A1F2B")
ACCENT_DARK = colors.HexColor("#5A141D")
ACCENT_SOFT = colors.HexColor("#FBF3F4")
ACCENT_LIGHT = colors.HexColor("#F6E8EA")
BG_PAGE = colors.HexColor("#FBFAF8")
BORDER = colors.HexColor("#D9D3CD")
BORDER_LIGHT = colors.HexColor("#ECE8E4")
TEXT = colors.HexColor("#1D1815")
TEXT_MUTED = colors.HexColor("#7A6F6A")


def _section_bar(c, x, y, w, h, label):
    """Akzent-Bordeaux-Sektions-Header mit weißem Text."""
    c.setFillColor(ACCENT)
    c.rect(x, y, w, h, stroke=0, fill=1)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 10)
    c.drawString(x + 6, y + h / 2 - 3, label)


def _field_box(c, x, y, w, h, label, value):
    """Single-Line-Feld: dezente Border, kleines Label oben links
    (in muted color), Wert darunter groß. Kein dunkler Stripe."""
    c.setStrokeColor(BORDER); c.setLineWidth(0.5)
    c.setFillColor(BG_PAGE)
    c.rect(x, y, w, h, stroke=1, fill=1)
    # Label oben in mutes
    c.setFillColor(TEXT_MUTED); c.setFont("Helvetica", 6.5)
    c.drawString(x + 3, y + h - 5, label)
    # Wert darunter
    if value:
        c.setFillColor(TEXT); c.setFont("Helvetica", 10)
        c.drawString(x + 3, y + 3, str(value)[:140])


def _multiline_box(c, x, y, w, h, label, value):
    """Mehrzeiliges Textfeld mit Label oben, Word-Wrap im freien Bereich."""
    c.setStrokeColor(BORDER); c.setLineWidth(0.5)
    c.setFillColor(BG_PAGE)
    c.rect(x, y, w, h, stroke=1, fill=1)
    c.setFillColor(TEXT_MUTED); c.setFont("Helvetica", 6.5)
    c.drawString(x + 3, y + h - 5, label)
    if value:
        text_top_y = y + h - 9
        max_lines = max(1, int((text_top_y - y - 2) / 4.5))
        c.setFillColor(TEXT); c.setFont("Helvetica", 9.5)
        text_obj = c.beginText(x + 4, text_top_y - 4)
        text_obj.setLeading(11.5)
        count = 0
        for paragraph in str(value).splitlines():
            for chunk in _wrap(paragraph, 100):
                if count >= max_lines: break
                text_obj.textLine(chunk)
                count += 1
            if count >= max_lines: break
        c.drawText(text_obj)


def _wrap(text, max_chars):
    if not text:
        yield ""
        return
    words = text.split(" ")
    line = ""
    for w in words:
        if len(line) + len(w) + 1 <= max_chars:
            line = (line + " " + w).strip()
        else:
            if line: yield line
            line = w
    if line: yield line


def render_einsatzbefehl_pdf(*, befehl, exporter_label=None):
    """A4 portrait, 1 Seite. Bordeaux-Akzente, kein knalliges Rot/Gelb."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    PAGE_W, PAGE_H = A4
    margin = 14 * mm
    inner_w = PAGE_W - 2 * margin
    cur_y = PAGE_H - margin

    # === Titel-Header (Bordeaux-Akzent) ===
    title_h = 14 * mm
    cur_y -= title_h
    c.setFillColor(ACCENT)
    c.rect(margin, cur_y, inner_w, title_h, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin + 6, cur_y + title_h / 2 - 5, "Einsatzbefehl")
    # ID rechts in der Header-Box
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(margin + inner_w - 6, cur_y + title_h / 2 - 4,
                       befehl.get("eindeutige_id") or "—")
    c.setFont("Helvetica", 6.5); c.setFillColor(colors.HexColor("#F6E8EA"))
    c.drawRightString(margin + inner_w - 6, cur_y + 2,
                       "eindeutige ID")

    # === Kopfdaten: Befehlende Stelle | Takt. Zeit (2 Spalten) ===
    cur_y -= 4
    row_h = 13 * mm
    cur_y -= row_h
    half = inner_w / 2
    _field_box(c, margin, cur_y, half, row_h,
                "Befehlende Stelle", befehl.get("befehlende_stelle"))
    _field_box(c, margin + half, cur_y, inner_w - half, row_h,
                "Takt. Zeit", befehl.get("takt_zeit"))

    # Befehl für (volle Breite)
    cur_y -= 1
    cur_y -= row_h
    _field_box(c, margin, cur_y, inner_w, row_h,
                "Befehl für", befehl.get("befehl_fuer"))

    # === Lage ===
    cur_y -= 4
    cur_y -= 7 * mm
    _section_bar(c, margin, cur_y, inner_w, 7 * mm, "Lage")
    cur_y -= 26 * mm
    _multiline_box(c, margin, cur_y, inner_w, 26 * mm,
                    "Lagebild", befehl.get("lage"))

    # === Auftrag ===
    cur_y -= 4
    cur_y -= 7 * mm
    _section_bar(c, margin, cur_y, inner_w, 7 * mm, "Auftrag")
    cur_y -= 22 * mm
    _multiline_box(c, margin, cur_y, inner_w, 22 * mm,
                    "Auftrag", befehl.get("auftrag"))
    cur_y -= 1
    cur_y -= row_h
    _field_box(c, margin, cur_y, inner_w, row_h,
                "Auftragsort", befehl.get("auftragsort"))
    cur_y -= 1
    cur_y -= row_h
    _field_box(c, margin, cur_y, half, row_h,
                "Ansprechpartner vor Ort", befehl.get("ansprechpartner"))
    _field_box(c, margin + half, cur_y, inner_w - half, row_h,
                "Kontaktnummer vor Ort", befehl.get("kontaktnummer"))

    # === Durchführung ===
    cur_y -= 4
    cur_y -= 7 * mm
    _section_bar(c, margin, cur_y, inner_w, 7 * mm, "Durchführung")
    cur_y -= 22 * mm
    _multiline_box(c, margin, cur_y, inner_w, 22 * mm,
                    "Durchführung", befehl.get("durchfuehrung"))

    # === Versorgung ===
    cur_y -= 4
    cur_y -= 7 * mm
    _section_bar(c, margin, cur_y, inner_w, 7 * mm, "Versorgung")
    cur_y -= 18 * mm
    _multiline_box(c, margin, cur_y, inner_w, 18 * mm,
                    "Versorgung", befehl.get("versorgung"))

    # === Verbindung und Führung ===
    cur_y -= 4
    cur_y -= 7 * mm
    _section_bar(c, margin, cur_y, inner_w, 7 * mm,
                  "Verbindung und Führung")
    cur_y -= 14 * mm
    _multiline_box(c, margin, cur_y, inner_w, 14 * mm,
                    "Verbindung", befehl.get("verbindung"))

    # === Rückwärtige Führungseinrichtung (4-Spalten-Block) ===
    cur_y -= 4
    cur_y -= 7 * mm
    _section_bar(c, margin, cur_y, inner_w, 7 * mm,
                  "Rückwärtige Führungseinrichtung")
    field_h = 9 * mm
    quarter = inner_w / 4
    cur_y -= field_h
    _field_box(c, margin + 0 * quarter, cur_y, quarter, field_h,
                "Bezeichnung", befehl.get("rueck_bezeichnung"))
    _field_box(c, margin + 1 * quarter, cur_y, quarter, field_h,
                "Rufname", befehl.get("rueck_rufname"))
    _field_box(c, margin + 2 * quarter, cur_y, quarter, field_h,
                "Funkgruppe / Kanal", befehl.get("rueck_funkgruppe"))
    _field_box(c, margin + 3 * quarter, cur_y, quarter, field_h,
                "Telefon", befehl.get("rueck_telefon"))

    # === Befehl erstellt von ===
    cur_y -= 4
    cur_y -= 10 * mm
    c.setStrokeColor(BORDER); c.setLineWidth(0.5)
    c.setFillColor(ACCENT_SOFT)
    c.rect(margin, cur_y, inner_w, 10 * mm, stroke=1, fill=1)
    c.setFillColor(TEXT_MUTED); c.setFont("Helvetica", 7)
    c.drawString(margin + 4, cur_y + 10 * mm - 5, "Befehl erstellt von")
    if befehl.get("erstellt_von_text"):
        c.setFillColor(ACCENT_DARK); c.setFont("Helvetica-Bold", 11)
        c.drawString(margin + 4, cur_y + 2, befehl["erstellt_von_text"])

    # === Footer ===
    c.setStrokeColor(BORDER_LIGHT); c.setLineWidth(0.4)
    c.line(margin, margin - 3, margin + inner_w, margin - 3)
    c.setFont("Helvetica", 7); c.setFillColor(TEXT_MUTED)
    footer_y = margin - 7
    c.drawString(margin, footer_y,
                  f"Erstellt: {_dt.now().strftime('%d.%m.%Y %H:%M')}")
    if exporter_label:
        c.drawCentredString(PAGE_W / 2, footer_y,
                             f"Exportiert von {exporter_label}")
    c.drawRightString(margin + inner_w, footer_y, "Seite 1 von 1")
    c.save()
    return buf.getvalue()


# ============ Einsatztagebuch ============

def render_einsatztagebuch_pdf(*, tagebuch, befehl, eintraege,
                                exporter_label=None):
    """A4 portrait, mehrseitig. Bordeaux-Akzente konsistent mit dem
    Einsatzbefehl-PDF."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    PAGE_W, PAGE_H = A4
    margin = 14 * mm
    inner_w = PAGE_W - 2 * margin

    col_widths = [
        14 * mm,   # lfd. Nr.
        12 * mm,   # E/A
        24 * mm,   # Takt. Zeit
        100 * mm,  # Darstellung
        16 * mm,   # Vollzug
        16 * mm,   # Anlage
    ]
    col_x = []
    acc = margin
    for w in col_widths:
        col_x.append(acc)
        acc += w

    row_h = 14 * mm
    header_total_h = 56 * mm
    avail_h = PAGE_H - 2 * margin - header_total_h - 10
    rows_per_page = max(8, int(avail_h / row_h))

    total_entries = len(eintraege) or 0
    total_pages = max(1, (total_entries + rows_per_page - 1) // rows_per_page) \
        if total_entries else 1

    for page in range(total_pages):
        cur_y = PAGE_H - margin
        # === Titel-Header ===
        title_h = 14 * mm
        cur_y -= title_h
        c.setFillColor(ACCENT)
        c.rect(margin, cur_y, inner_w, title_h, stroke=0, fill=1)
        c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 16)
        c.drawString(margin + 6, cur_y + title_h / 2 - 5, "Einsatztagebuch")
        c.setFont("Helvetica-Bold", 10)
        c.drawRightString(margin + inner_w - 6, cur_y + title_h / 2 - 3,
                           "EB-Ref: " + (befehl.get("eindeutige_id") or "—"))

        # === Kopfdaten ===
        cur_y -= 3
        cur_y -= 11 * mm
        _field_box(c, margin, cur_y, inner_w, 11 * mm,
                    "Einrichtung / Einheit",
                    tagebuch.get("einrichtung_einheit"))
        cur_y -= 1
        cur_y -= 11 * mm
        _field_box(c, margin, cur_y, inner_w, 11 * mm,
                    "Einsatz / Anlass",
                    tagebuch.get("einsatz_anlass"))
        # Blatt X/Y rechts unter den Kopfdaten — eigene Zeile mit Abstand
        cur_y -= 6 * mm
        c.setFillColor(TEXT_MUTED); c.setFont("Helvetica-Bold", 9)
        c.drawRightString(margin + inner_w, cur_y + 1,
                           f"Blatt {page + 1} / {total_pages}")

        # === Tabellen-Header ===
        cur_y -= 3
        head_h = 13 * mm
        cur_y -= head_h
        # Akzent-Bordeaux Hintergrund — fill-only, dann Outline separat
        c.setFillColor(ACCENT)
        c.rect(margin, cur_y, inner_w, head_h, stroke=0, fill=1)
        # Outline + Spaltentrenner in dunklerem Bordeaux
        c.setStrokeColor(ACCENT_DARK); c.setLineWidth(0.5)
        c.rect(margin, cur_y, inner_w, head_h, stroke=1, fill=0)
        c.setStrokeColor(colors.HexColor("#9A3540"))
        c.setLineWidth(0.4)
        for i in range(1, len(col_widths)):
            x = col_x[i]
            c.line(x, cur_y, x, cur_y + head_h)
        labels = ["lfd. Nr.", "E/A", "Taktische\nZeit",
                  "Darstellung der Ereignisse, Maßnahmen und Überlegungen",
                  "Voll-\nzug", "Anlage"]
        c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.white)
        LINE_H = 9  # Zeilenabstand zwischen multi-line label-Zeilen (in points)
        for i, lbl in enumerate(labels):
            lines = lbl.split("\n")
            n = len(lines)
            # Block vertikal mittig: erste Zeile so platzieren, dass
            # gesamter Block (n Zeilen mit Abstand LINE_H) zentriert ist
            block_h = (n - 1) * LINE_H + 8  # 8 ≈ font-cap-height
            top_y = cur_y + (head_h + block_h) / 2 - 8
            for li, ln in enumerate(lines):
                row_y = top_y - li * LINE_H
                if i in (0, 1, 2, 4, 5):
                    c.drawCentredString(col_x[i] + col_widths[i] / 2,
                                         row_y, ln)
                else:
                    c.drawString(col_x[i] + 4, row_y, ln)

        # === Daten-Zeilen ===
        page_entries = eintraege[page * rows_per_page:
                                  (page + 1) * rows_per_page]
        actual_rows = max(len(page_entries), rows_per_page)
        for i in range(actual_rows):
            ry = cur_y - (i + 1) * row_h
            # Zebra-Streifen
            if i % 2 == 1:
                c.setFillColor(ACCENT_SOFT)
                c.rect(margin, ry, inner_w, row_h, stroke=0, fill=1)
            c.setStrokeColor(BORDER); c.setLineWidth(0.3)
            c.rect(margin, ry, inner_w, row_h, stroke=1, fill=0)
            for j in range(1, len(col_widths)):
                c.line(col_x[j], ry, col_x[j], ry + row_h)
            if i < len(page_entries):
                e = page_entries[i]
                c.setFillColor(TEXT)
                c.setFont("Helvetica-Bold", 10)
                c.drawCentredString(col_x[0] + col_widths[0] / 2,
                                      ry + row_h - 6, str(e["lfd_nr"]))
                if e.get("ea"):
                    c.drawCentredString(col_x[1] + col_widths[1] / 2,
                                         ry + row_h - 6, e["ea"])
                if e.get("taktische_zeit"):
                    c.setFont("Helvetica", 8.5)
                    c.drawCentredString(col_x[2] + col_widths[2] / 2,
                                         ry + row_h - 6,
                                         str(e["taktische_zeit"])[:14])
                if e.get("darstellung"):
                    c.setFont("Helvetica", 8.5)
                    text_obj = c.beginText(col_x[3] + 3, ry + row_h - 5)
                    text_obj.setLeading(10)
                    cnt = 0
                    max_lines = int(row_h / 3.4)
                    for para in str(e["darstellung"]).splitlines():
                        for chunk in _wrap(para, 88):
                            if cnt >= max_lines: break
                            text_obj.textLine(chunk)
                            cnt += 1
                        if cnt >= max_lines: break
                    c.drawText(text_obj)
                if e.get("vollzug"):
                    c.setFont("Helvetica", 8.5)
                    c.drawCentredString(col_x[4] + col_widths[4] / 2,
                                         ry + row_h - 6,
                                         str(e["vollzug"])[:6])
                if e.get("anlage"):
                    c.setFont("Helvetica", 8.5)
                    c.drawCentredString(col_x[5] + col_widths[5] / 2,
                                         ry + row_h - 6,
                                         str(e["anlage"])[:8])

        # === Footer ===
        c.setStrokeColor(BORDER_LIGHT); c.setLineWidth(0.4)
        c.line(margin, margin - 3, margin + inner_w, margin - 3)
        c.setFont("Helvetica", 7); c.setFillColor(TEXT_MUTED)
        footer_y = margin - 7
        c.drawString(margin, footer_y,
                      f"Einsatzbefehl: {befehl.get('eindeutige_id','—')}"
                      f"  ·  Erstellt: {_dt.now().strftime('%d.%m.%Y %H:%M')}")
        if exporter_label:
            c.drawCentredString(PAGE_W / 2, footer_y,
                                 f"Exportiert von {exporter_label}")
        c.drawRightString(margin + inner_w, footer_y,
                           f"Seite {page + 1} von {total_pages}")
        c.showPage()

    c.save()
    return buf.getvalue()
