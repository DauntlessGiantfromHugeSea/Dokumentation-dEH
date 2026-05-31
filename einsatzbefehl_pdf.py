"""Einsatzbefehl + Einsatztagebuch als A4-Portrait PDF.

Beide Renderer sind Eigenständig (Canvas, low-level), Layout an den
Vorlagen aus Rheinland-Pfalz orientiert — ohne deren Logos/Embleme.
"""

import io
from datetime import datetime as _dt

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Paragraph

ACCENT = colors.HexColor("#7A1F2B")
RED_BAR = colors.HexColor("#D32F2F")
YELLOW_BAR = colors.HexColor("#F4C430")
BORDER = colors.HexColor("#222222")
BORDER_LIGHT = colors.HexColor("#999999")
TEXT_MUTED = colors.HexColor("#555555")

S_TITLE = ParagraphStyle("T", fontName="Helvetica-Bold",
                          fontSize=14, leading=17)
S_SEC = ParagraphStyle("S", fontName="Helvetica-Bold",
                        fontSize=11, leading=14,
                        textColor=colors.black)
S_LABEL = ParagraphStyle("L", fontName="Helvetica-Bold",
                          fontSize=8, leading=10)
S_VAL = ParagraphStyle("V", fontName="Helvetica",
                        fontSize=10, leading=13, wordWrap="LTR")


def _section_bar(c, x, y, w, h, label, fill=YELLOW_BAR):
    c.setFillColor(fill)
    c.rect(x, y, w, h, stroke=0, fill=1)
    c.setStrokeColor(BORDER); c.setLineWidth(0.5)
    c.rect(x, y, w, h, stroke=1, fill=0)
    c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 10)
    c.drawString(x + 4, y + h / 2 - 3, label)


def _label_box(c, x, y, w, h, label, value, *, value_lines=1):
    """Rechteckiger Feld-Block: oben kleines schwarzes Label-Stripe,
    darunter freier Bereich mit eingetragenem Wert (oder leer)."""
    # Außenrahmen
    c.setStrokeColor(BORDER); c.setLineWidth(0.5)
    c.rect(x, y, w, h, stroke=1, fill=0)
    # Label-Stripe oben (schwarz mit weißem Text)
    lbl_h = 4 * mm
    c.setFillColor(colors.black)
    c.rect(x, y + h - lbl_h, w, lbl_h, stroke=0, fill=1)
    c.setFillColor(colors.white); c.setFont("Helvetica", 7)
    c.drawString(x + 2, y + h - lbl_h + 1, label)
    # Wert
    if value:
        text_x = x + 2
        text_y = y + h - lbl_h - 5
        c.setFillColor(colors.black); c.setFont("Helvetica", 9.5)
        for line in str(value).splitlines()[:value_lines]:
            c.drawString(text_x, text_y, line[:120])
            text_y -= 4.5


def _multiline_box(c, x, y, w, h, label, value):
    """Großes Textfeld mit Label oben."""
    c.setStrokeColor(BORDER); c.setLineWidth(0.5)
    c.rect(x, y, w, h, stroke=1, fill=0)
    lbl_h = 4 * mm
    c.setFillColor(colors.black)
    c.rect(x, y + h - lbl_h, w, lbl_h, stroke=0, fill=1)
    c.setFillColor(colors.white); c.setFont("Helvetica", 7)
    c.drawString(x + 2, y + h - lbl_h + 1, label)
    # Wert
    if value:
        max_lines = int((h - lbl_h - 4) / 4.2)
        c.setFillColor(colors.black); c.setFont("Helvetica", 9.5)
        # Sehr einfaches Wrapping: Zeilen splitten + auf max. ~95 Zeichen
        text_obj = c.beginText(x + 3, y + h - lbl_h - 5)
        text_obj.setLeading(11)
        count = 0
        for paragraph in str(value).splitlines():
            for chunk in _wrap(paragraph, 95):
                if count >= max_lines: break
                text_obj.textLine(chunk)
                count += 1
            if count >= max_lines: break
        c.drawText(text_obj)


def _wrap(text, max_chars):
    """Sehr einfacher Word-Wrap auf max_chars."""
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
    """Liefert PDF-Bytes für einen Einsatzbefehl. A4 portrait, 1 Seite."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    PAGE_W, PAGE_H = A4
    margin = 12 * mm
    inner_w = PAGE_W - 2 * margin
    cur_y = PAGE_H - margin

    # === Titelbalken oben ===
    title_h = 9 * mm
    cur_y -= title_h
    c.setStrokeColor(BORDER); c.setLineWidth(0.6)
    c.rect(margin, cur_y, inner_w, title_h, stroke=1, fill=0)
    c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 12)
    c.drawString(margin + 3, cur_y + 2, "Einsatzbefehl")
    # Eindeutige ID rechts
    c.setFont("Helvetica-Bold", 9); c.setFillColor(ACCENT)
    c.drawRightString(margin + inner_w - 3, cur_y + 2,
                       befehl.get("eindeutige_id") or "—")
    cur_y -= 3

    # === Roter Header „Einsatzbefehl" ===
    hdr_h = 8 * mm
    cur_y -= hdr_h
    c.setFillColor(RED_BAR)
    c.rect(margin, cur_y, inner_w, hdr_h, stroke=0, fill=1)
    c.setStrokeColor(BORDER); c.setLineWidth(0.5)
    c.rect(margin, cur_y, inner_w, hdr_h, stroke=1, fill=0)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(PAGE_W / 2, cur_y + 2, "Einsatzbefehl")

    # === Befehlende Stelle + Takt. Zeit ===
    cur_y -= 2
    row_h = 10 * mm
    cur_y -= row_h
    half = inner_w / 2
    _label_box(c, margin, cur_y, half, row_h,
                "Befehlende Stelle", befehl.get("befehlende_stelle"))
    _label_box(c, margin + half, cur_y, inner_w - half, row_h,
                "Takt. Zeit", befehl.get("takt_zeit"))

    # === Befehl für ===
    cur_y -= 1
    row_h = 10 * mm
    cur_y -= row_h
    _label_box(c, margin, cur_y, inner_w, row_h,
                "Befehl für:", befehl.get("befehl_fuer"))

    # === Lage ===
    cur_y -= 3
    cur_y -= 6 * mm
    _section_bar(c, margin, cur_y, inner_w, 6 * mm, "Lage", fill=YELLOW_BAR)
    cur_y -= 26 * mm
    _multiline_box(c, margin, cur_y, inner_w, 26 * mm,
                    "Lage", befehl.get("lage"))

    # === Auftrag ===
    cur_y -= 2
    cur_y -= 6 * mm
    _section_bar(c, margin, cur_y, inner_w, 6 * mm, "Auftrag")
    cur_y -= 22 * mm
    _multiline_box(c, margin, cur_y, inner_w, 22 * mm,
                    "Auftrag:", befehl.get("auftrag"))
    cur_y -= 1
    row_h = 8 * mm
    cur_y -= row_h
    _label_box(c, margin, cur_y, inner_w, row_h,
                "Auftragsort:", befehl.get("auftragsort"))
    cur_y -= 1
    cur_y -= row_h
    _label_box(c, margin, cur_y, half, row_h,
                "Ansprechpartner vor Ort:", befehl.get("ansprechpartner"))
    _label_box(c, margin + half, cur_y, inner_w - half, row_h,
                "Kontaktnummer vor Ort:", befehl.get("kontaktnummer"))

    # === Durchführung ===
    cur_y -= 2
    cur_y -= 6 * mm
    _section_bar(c, margin, cur_y, inner_w, 6 * mm, "Durchführung")
    cur_y -= 22 * mm
    _multiline_box(c, margin, cur_y, inner_w, 22 * mm,
                    "Durchführung:", befehl.get("durchfuehrung"))

    # === Versorgung ===
    cur_y -= 2
    cur_y -= 6 * mm
    _section_bar(c, margin, cur_y, inner_w, 6 * mm, "Versorgung")
    cur_y -= 18 * mm
    _multiline_box(c, margin, cur_y, inner_w, 18 * mm,
                    "Versorgung:", befehl.get("versorgung"))

    # === Verbindung und Führung ===
    cur_y -= 2
    cur_y -= 6 * mm
    _section_bar(c, margin, cur_y, inner_w, 6 * mm,
                  "Verbindung und Führung")
    cur_y -= 14 * mm
    _multiline_box(c, margin, cur_y, inner_w, 14 * mm,
                    "Verbindung:", befehl.get("verbindung"))

    # === Rückwärtige Führungseinrichtung ===
    cur_y -= 2
    cur_y -= 6 * mm
    _section_bar(c, margin, cur_y, inner_w, 6 * mm,
                  "Rückwärtige Führungseinrichtung")
    # 4 Felder untereinander (Bezeichnung, Rufname, Funkgruppe, Telefon)
    field_h = 7 * mm
    label_w = 36 * mm
    for lbl, val in [
        ("Bezeichnung:", befehl.get("rueck_bezeichnung")),
        ("Rufname:", befehl.get("rueck_rufname")),
        ("Funkgruppe/Kanal:", befehl.get("rueck_funkgruppe")),
        ("Telefon:", befehl.get("rueck_telefon")),
    ]:
        cur_y -= field_h
        c.setStrokeColor(BORDER); c.setLineWidth(0.5)
        c.rect(margin, cur_y, inner_w, field_h, stroke=1, fill=0)
        # Label-Spalte links
        c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 8)
        c.drawString(margin + 2, cur_y + field_h - 4, lbl)
        # Wert-Spalte rechts
        c.setStrokeColor(BORDER); c.setLineWidth(0.4)
        c.line(margin + label_w, cur_y, margin + label_w,
                cur_y + field_h)
        if val:
            c.setFont("Helvetica", 10)
            c.drawString(margin + label_w + 3, cur_y + field_h - 5,
                          str(val)[:100])

    # === „Befehl erstellt von" ===
    cur_y -= 6
    c.setFillColor(colors.black); c.setFont("Helvetica", 9)
    c.drawString(margin, cur_y, "Befehl erstellt von:")
    if befehl.get("erstellt_von_text"):
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin + 40 * mm, cur_y, befehl["erstellt_von_text"])

    # === Footer ===
    c.setFont("Helvetica", 7); c.setFillColor(TEXT_MUTED)
    footer_y = margin - 5
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
    """Liefert PDF-Bytes für ein Einsatztagebuch. A4 portrait, ggf. mehrere
    Seiten je nach Eintragsanzahl."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    PAGE_W, PAGE_H = A4
    margin = 12 * mm
    inner_w = PAGE_W - 2 * margin

    # Spaltenbreiten (Summe = inner_w ≈ 186mm)
    col_widths = [
        14 * mm,   # lfd. Nr.
        12 * mm,   # E/A
        24 * mm,   # Takt. Zeit
        102 * mm,  # Darstellung
        16 * mm,   # Vollzug
        18 * mm,   # Anlage
    ]
    col_x = []
    acc = margin
    for w in col_widths:
        col_x.append(acc)
        acc += w

    # Wie viele Einträge pro Seite (Zeilenhöhe ~14mm)?
    row_h = 14 * mm
    header_total_h = 60 * mm  # Titel + Meta + Tabelle-Header
    avail_h = PAGE_H - 2 * margin - header_total_h - 8
    rows_per_page = max(8, int(avail_h / row_h))

    total_entries = len(eintraege) or 0
    # Mindestens 1 Seite, auch ohne Einträge
    total_pages = max(1, (total_entries + rows_per_page - 1) // rows_per_page) \
        if total_entries else 1

    for page in range(total_pages):
        cur_y = PAGE_H - margin
        # === Header-Box mit Titel + Meta ===
        # Titel-Bereich + Einrichtung + Einsatz
        header_h = 32 * mm
        cur_y -= header_h
        c.setStrokeColor(BORDER); c.setLineWidth(0.6)
        c.rect(margin, cur_y, inner_w, header_h, stroke=1, fill=0)
        # Titel zentriert oben
        c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 16)
        c.drawCentredString(PAGE_W / 2, cur_y + header_h - 8,
                             "Einsatztagebuch")
        # Eindeutige ID des zugehörigen Befehls oben rechts
        c.setFont("Helvetica", 8); c.setFillColor(ACCENT)
        c.drawRightString(margin + inner_w - 3, cur_y + header_h - 4,
                           "EB-Ref: " + (befehl.get("eindeutige_id") or "—"))
        # Einrichtung/Einheit
        line_y = cur_y + header_h - 16
        c.setStrokeColor(BORDER); c.setLineWidth(0.4)
        c.line(margin, line_y, margin + inner_w, line_y)
        c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 10)
        c.drawString(margin + 3, line_y + 3, "Einrichtung/Einheit:")
        if tagebuch.get("einrichtung_einheit"):
            c.setFont("Helvetica", 10)
            c.drawString(margin + 48 * mm, line_y + 3,
                          str(tagebuch["einrichtung_einheit"])[:120])
        # Einsatz/Anlass
        line_y2 = cur_y + 8
        c.line(margin, line_y2 + 4, margin + inner_w, line_y2 + 4)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin + 3, line_y2 - 1, "Einsatz/Anlass:")
        if tagebuch.get("einsatz_anlass"):
            c.setFont("Helvetica", 10)
            c.drawString(margin + 38 * mm, line_y2 - 1,
                          str(tagebuch["einsatz_anlass"])[:120])

        # Blatt X/Y rechts unter dem Block
        cur_y -= 6 * mm
        c.setFont("Helvetica-Bold", 10); c.setFillColor(colors.black)
        c.drawRightString(margin + inner_w, cur_y + 1,
                           f"Blatt {page + 1} / {total_pages}")

        # === Tabellen-Header ===
        cur_y -= 1
        head_h = 10 * mm
        cur_y -= head_h
        c.setStrokeColor(BORDER); c.setLineWidth(0.5)
        c.rect(margin, cur_y, inner_w, head_h, stroke=1, fill=0)
        # Spaltentrenner
        for i in range(1, len(col_widths)):
            x = col_x[i]
            c.line(x, cur_y, x, cur_y + head_h)
        # Labels
        labels = ["lfd. Nr.", "E/A", "Taktische\nZeit",
                  "Darstellung der Ereignisse, Maßnahmen und Überlegungen",
                  "Voll-\nzug", "Anlage"]
        c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.black)
        for i, lbl in enumerate(labels):
            lines = lbl.split("\n")
            for li, ln in enumerate(lines):
                if i in (0, 1, 4):  # Schmale Spalten zentriert
                    c.drawCentredString(col_x[i] + col_widths[i] / 2,
                                         cur_y + head_h - 4 - li * 3.5, ln)
                else:
                    c.drawString(col_x[i] + 2,
                                   cur_y + head_h - 4 - li * 3.5, ln)

        # === Zeilen ===
        page_entries = eintraege[page * rows_per_page:
                                  (page + 1) * rows_per_page]
        # Auch wenn keine Einträge: Leerzeilen für Handschrift
        actual_rows = max(len(page_entries), rows_per_page)
        for i in range(actual_rows):
            ry = cur_y - (i + 1) * row_h
            c.setStrokeColor(BORDER); c.setLineWidth(0.3)
            c.rect(margin, ry, inner_w, row_h, stroke=1, fill=0)
            for j in range(1, len(col_widths)):
                c.line(col_x[j], ry, col_x[j], ry + row_h)
            # Inhalt falls vorhanden
            if i < len(page_entries):
                e = page_entries[i]
                # lfd. Nr.
                c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 10)
                c.drawCentredString(col_x[0] + col_widths[0] / 2,
                                      ry + row_h - 6, str(e["lfd_nr"]))
                # E/A
                if e.get("ea"):
                    c.drawCentredString(col_x[1] + col_widths[1] / 2,
                                         ry + row_h - 6, e["ea"])
                # Takt. Zeit
                if e.get("taktische_zeit"):
                    c.setFont("Helvetica", 8.5)
                    c.drawString(col_x[2] + 2, ry + row_h - 6,
                                  str(e["taktische_zeit"])[:14])
                # Darstellung (mehrzeilig)
                if e.get("darstellung"):
                    c.setFont("Helvetica", 8.5)
                    text_obj = c.beginText(col_x[3] + 2, ry + row_h - 5)
                    text_obj.setLeading(10)
                    cnt = 0
                    max_lines = int(row_h / 3.5)
                    for para in str(e["darstellung"]).splitlines():
                        for chunk in _wrap(para, 88):
                            if cnt >= max_lines: break
                            text_obj.textLine(chunk)
                            cnt += 1
                        if cnt >= max_lines: break
                    c.drawText(text_obj)
                # Vollzug
                if e.get("vollzug"):
                    c.setFont("Helvetica", 8.5)
                    c.drawCentredString(col_x[4] + col_widths[4] / 2,
                                         ry + row_h - 6,
                                         str(e["vollzug"])[:6])
                # Anlage
                if e.get("anlage"):
                    c.setFont("Helvetica", 8.5)
                    c.drawString(col_x[5] + 2, ry + row_h - 6,
                                  str(e["anlage"])[:8])

        # === Footer ===
        c.setFont("Helvetica", 7); c.setFillColor(TEXT_MUTED)
        footer_y = margin - 5
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
