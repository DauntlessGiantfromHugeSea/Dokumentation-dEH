"""Blanko-Protokolle als PDF — Papier-Fallback, wenn das System steht.

- render_blanko_zeh(): 4-seitiges zentrales Notfallprotokoll im
  Tool-Bordeaux-Design, DIVI-artig dicht mit Ankreuzfeldern.
- render_blanko_deh(): dezentraler Einsatzbericht mit leeren Feldern
  (nutzt den normalen dEH-Renderer).
"""
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas as rl_canvas

ACCENT      = colors.HexColor("#7a1f2b")
BORDER      = colors.HexColor("#c4c8cc")
BORDER_DARK = colors.HexColor("#9aa0a6")
TEXT        = colors.HexColor("#1f2328")
MUTED       = colors.HexColor("#6a6f74")

PAGE_W, PAGE_H = A4
MARGIN = 10 * mm
GAP    = 2.5 * mm
BAR_H  = 6 * mm
FS_LBL = 7.2
FS_TINY = 6.5
ROW_H  = 4.2 * mm
PAD    = 2.5 * mm


def _bar(c, x, y, w, title, sub=None):
    c.setFillColor(ACCENT)
    c.rect(x, y, w, BAR_H, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(x + 2.5 * mm, y + BAR_H / 2 - 3, title)
    if sub:
        c.setFont("Helvetica", 7)
        c.drawRightString(x + w - 2.5 * mm, y + BAR_H / 2 - 2.4, sub)


def _box(c, x, y, w, h):
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.5)
    c.rect(x, y, w, h, stroke=1, fill=0)


def _section(c, x, y_top, w, h, title, sub=None):
    _bar(c, x, y_top - BAR_H, w, title, sub)
    inside_h = h - BAR_H
    _box(c, x, y_top - BAR_H - inside_h, w, inside_h)
    return (x + PAD, y_top - BAR_H - PAD, w - 2 * PAD, inside_h - 2 * PAD)


def _title(c, x, y, label):
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(ACCENT)
    c.drawString(x, y, label)


def _sub(c, x, y, label):
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(TEXT)
    c.drawString(x, y, label)


def _small(c, x, y, txt, fill=MUTED, size=FS_TINY, bold=False):
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    c.setFillColor(fill)
    c.drawString(x, y, txt)


def _line(c, x, y, w, label=None):
    cur_x = x
    if label:
        c.setFont("Helvetica", FS_TINY)
        c.setFillColor(MUTED)
        c.drawString(x, y + 1.4, label)
        cur_x = x + c.stringWidth(label, "Helvetica", FS_TINY) + 1.5 * mm
    c.setStrokeColor(BORDER_DARK)
    c.setLineWidth(0.4)
    c.line(cur_x, y, x + w, y)


def _boxes(c, x, y, n=8, w=4 * mm, h=4 * mm, gap=0.5 * mm):
    for i in range(n):
        c.setStrokeColor(BORDER_DARK)
        c.setLineWidth(0.3)
        c.rect(x + i * (w + gap), y, w, h, stroke=1, fill=0)


def _cb(c, x, y, label=None, num=None, size=FS_LBL, gap=1.4 * mm,
        round_=True):
    r = 1.1 * mm
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.4)
    if round_:
        c.circle(x + r, y + r, r, stroke=1, fill=0)
    else:
        c.rect(x, y - 0.2, 2 * r, 2 * r, stroke=1, fill=0)
    tx = x + 2 * r + gap
    if num is not None:
        c.setFont("Helvetica", size - 0.5)
        c.setFillColor(MUTED)
        c.drawString(tx, y - 0.3, f"{num:02d}")
        tx += 3.2 * mm
    if label is not None:
        c.setFont("Helvetica", size)
        c.setFillColor(TEXT)
        c.drawString(tx, y - 0.3, label)
        tx += c.stringWidth(label, "Helvetica", size) + 1.0 * mm
    return tx


def _cb_list(c, x, y, items, row_h=ROW_H, size=FS_LBL, numbered=True,
             start=1):
    for i, label in enumerate(items):
        _cb(c, x, y - i * row_h, label=label, size=size,
            num=(start + i) if numbered else None)
    return y - (len(items) - 1) * row_h


def _write_lines(c, x, y_top, w, n_lines, line_gap=5 * mm):
    for i in range(n_lines):
        ly = y_top - (i + 1) * line_gap + 1
        c.setStrokeColor(BORDER)
        c.setLineWidth(0.3)
        c.line(x, ly, x + w, ly)


def _head(c, page_num, total, page_title):
    c.setFillColor(ACCENT)
    c.rect(MARGIN, PAGE_H - MARGIN - 11 * mm, PAGE_W - 2 * MARGIN,
           11 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(MARGIN + 3 * mm, PAGE_H - MARGIN - 7 * mm,
                 "Zentrales Notfallprotokoll (zEH) — Blanko / Backup")
    c.setFont("Helvetica", 7.5)
    c.drawString(MARGIN + 3 * mm, PAGE_H - MARGIN - 10 * mm, page_title)
    c.drawRightString(PAGE_W - MARGIN - 3 * mm, PAGE_H - MARGIN - 7 * mm,
                      f"Seite {page_num} von {total}")
    c.setFont("Helvetica", 6.5)
    c.drawRightString(PAGE_W - MARGIN - 3 * mm, PAGE_H - MARGIN - 10 * mm,
                      "bei System-Ausfall handschriftlich · später ins System nachtragen")


def _page_1(c):
    _head(c, 1, 4, "Patient · Einsatz · Notfallgeschehen · Bewusstsein / GCS / Pupillen")
    inner_w = PAGE_W - 2 * MARGIN
    cur_y = PAGE_H - MARGIN - 11 * mm - GAP

    sec_h = 48 * mm
    cur_y -= sec_h
    half_w = (inner_w - GAP) / 2
    ix, iy, iw, _ = _section(c, MARGIN, cur_y + sec_h, half_w, sec_h,
                              "1. Patient", "Personalia")
    yy = iy
    for label in ("Name", "Vorname"):
        _small(c, ix, yy, label)
        _line(c, ix + 18 * mm, yy - 0.6, iw - 18 * mm)
        yy -= 6.5 * mm
    _small(c, ix, yy, "geb. am")
    _line(c, ix + 18 * mm, yy - 0.6, 32 * mm)
    _cb(c, ix + 56 * mm, yy - 0.8, label="m")
    _cb(c, ix + 64 * mm, yy - 0.8, label="w")
    _cb(c, ix + 72 * mm, yy - 0.8, label="divers")
    yy -= 6.5 * mm
    for label in ("Stammnr.", "Adresse", "Telefon"):
        _small(c, ix, yy, label)
        _line(c, ix + 18 * mm, yy - 0.6, iw - 18 * mm)
        yy -= 6.5 * mm

    ix, iy, iw, _ = _section(c, MARGIN + half_w + GAP, cur_y + sec_h,
                              half_w, sec_h, "2. Einsatz",
                              "Zeit · Ort · Einsatzkräfte")
    yy = iy
    _small(c, ix, yy, "Datum")
    _line(c, ix + 18 * mm, yy - 0.6, 30 * mm)
    _small(c, ix + 52 * mm, yy, "Uhrzeit")
    _line(c, ix + 68 * mm, yy - 0.6, iw - 68 * mm)
    yy -= 6.5 * mm
    _small(c, ix, yy, "Einsatzort")
    _line(c, ix + 18 * mm, yy - 0.6, iw - 18 * mm)
    yy -= 6.5 * mm
    _small(c, ix, yy, "Alarm durch")
    _line(c, ix + 20 * mm, yy - 0.6, 28 * mm)
    _small(c, ix + 52 * mm, yy, "Lfd. Nr.")
    _line(c, ix + 68 * mm, yy - 0.6, iw - 68 * mm)
    yy -= 6.5 * mm
    _small(c, ix, yy, "Einsatzkraft 1")
    _line(c, ix + 25 * mm, yy - 0.6, iw - 25 * mm)
    yy -= 6.5 * mm
    _small(c, ix, yy, "Einsatzkraft 2")
    _line(c, ix + 25 * mm, yy - 0.6, iw - 25 * mm)

    cur_y -= GAP
    sec_h = 48 * mm
    cur_y -= sec_h
    ix, iy, iw, _ = _section(c, MARGIN, cur_y + sec_h, inner_w, sec_h,
                              "3. Notfallgeschehen · Anamnese · Erstbefund",
                              "Beschwerdebeginn, Unfallzeitpunkt, Vormedikation, Vorbehandlung")
    _write_lines(c, ix, iy, iw, n_lines=8, line_gap=5 * mm)

    cur_y -= GAP
    sec_h = cur_y - MARGIN - 6 * mm
    cur_y -= sec_h
    ix, iy, iw, _ = _section(c, MARGIN, cur_y + sec_h, inner_w, sec_h,
                              "4. Erstbefund (Teil 1)",
                              "Bewusstsein · Glasgow-Coma-Scale · Pupillen")
    col_w = (iw - 2 * GAP) / 3
    cx = ix
    _title(c, cx, iy, "Bewusstseinslage")
    yy = iy - 4.5 * mm
    _cb_list(c, cx, yy, ["orientiert", "desorientiert", "getrübt",
                          "bewusstlos", "narkotisiert / sediert"], start=1)

    cx = ix + col_w + GAP
    _title(c, cx, iy, "Glasgow-Coma-Scale (GCS)")
    _small(c, cx + 50 * mm, iy + 0.5, "Summe:")
    _boxes(c, cx + 64 * mm, iy - 2, n=2, w=3.5 * mm)
    yy = iy - 5 * mm
    for title, opts in [
        ("Augen öffnen", [("spontan", 4), ("auf Aufforderung", 3),
                           ("auf Schmerz", 2), ("keine", 1)]),
        ("verbale Reaktion", [("orientiert", 5), ("desorientiert", 4),
                               ("inadäquate Äußerung", 3),
                               ("unverständliche Laute", 2), ("keine", 1)]),
        ("motorische Reaktion", [("auf Aufforderung", 6),
                                  ("auf Schmerz gezielt", 5),
                                  ("normale Beugeabwehr", 4),
                                  ("abnorme Beugeabwehr", 3),
                                  ("Strecksynergismen", 2), ("keine", 1)]),
    ]:
        _sub(c, cx, yy, title)
        yy -= 3.6 * mm
        for label, score in opts:
            _cb(c, cx, yy, label=f"{label} ({score})")
            yy -= 3.6 * mm
        yy -= 1 * mm

    cx = ix + 2 * (col_w + GAP)
    _title(c, cx, iy, "Pupillenweite")
    _small(c, cx + 28 * mm, iy + 0.5, "re / li")
    yy = iy - 5 * mm
    for label in ("eng", "mittel", "weit", "entrundet",
                  "nicht beurteilbar"):
        _cb(c, cx, yy, label="re", gap=0.4 * mm)
        _cb(c, cx + 9 * mm, yy, label="li", gap=0.4 * mm)
        _small(c, cx + 20 * mm, yy - 0.2, label, fill=TEXT, size=FS_LBL)
        yy -= 4.5 * mm
    yy -= 1 * mm
    _title(c, cx, yy, "Lichtreaktion fehlt")
    yy -= 4 * mm
    _cb(c, cx, yy, label="rechts")
    _cb(c, cx + 22 * mm, yy, label="links")
    yy -= ROW_H + 1 * mm
    _cb(c, cx, yy, label="Meningismus")
    yy -= ROW_H + 1 * mm
    _small(c, cx, yy, "Zeitpunkt Erhebung:")
    _line(c, cx + 32 * mm, yy - 0.6, col_w - 32 * mm)


def _page_2(c):
    _head(c, 2, 4, "Erstbefund Teil 2 (Vitalwerte · Atmung · Schmerz) · Verlauf")
    inner_w = PAGE_W - 2 * MARGIN
    cur_y = PAGE_H - MARGIN - 11 * mm - GAP

    sec_h = 70 * mm
    cur_y -= sec_h
    half_w = (inner_w - GAP) / 2
    ix, iy, iw, _ = _section(c, MARGIN, cur_y + sec_h, half_w, sec_h,
                              "4.2 Vitalwerte / Messwerte",
                              "Erstbefund")
    yy = iy
    for label, unit in [("RR sys/dia", "mmHg"), ("Puls", "/min"),
                         ("SpO₂", "%"), ("BZ", "mmol/l"),
                         ("AF (Atemfreq.)", "/min"), ("Temperatur", "°C"),
                         ("GCS", "3–15")]:
        _small(c, ix, yy, label, fill=TEXT, size=7)
        _boxes(c, ix + 30 * mm, yy - 1, n=6, w=4 * mm, h=4.5 * mm)
        _small(c, ix + 30 * mm + 6 * 4.5 * mm, yy, unit)
        yy -= 7 * mm
    yy -= 1 * mm
    _title(c, ix, yy, "Schmerz (NRS 0–10)")
    yy -= 4.5 * mm
    _boxes(c, ix, yy - 1, n=2, w=4 * mm, h=4.5 * mm)
    _small(c, ix + 12 * mm, yy, "Wert eintragen")

    ix, iy, iw, _ = _section(c, MARGIN + half_w + GAP, cur_y + sec_h,
                              half_w, sec_h, "4.3 Atmung / Haut / Psyche",
                              "Mehrfachauswahl")
    yy = iy
    _title(c, ix, yy, "Atmung")
    yy -= 4.2 * mm
    yy = _cb_list(c, ix, yy, ["spontan/frei", "Atemnot",
                               "Hyperventilation", "Atemstillstand"],
                  start=1) - ROW_H - 1 * mm
    _title(c, ix, yy, "Haut")
    yy -= 4.2 * mm
    yy = _cb_list(c, ix, yy, ["kein Befund", "blass", "zyanotisch",
                               "kaltschweißig"], start=1) - ROW_H - 1 * mm
    _title(c, ix, yy, "Psychischer Befund")
    yy -= 4.2 * mm
    _cb_list(c, ix, yy, ["unauffällig", "Psychose",
                          "v. a. Suizidalität"], start=1)

    cur_y -= GAP
    sec_h = cur_y - MARGIN - 4 * mm
    cur_y -= sec_h
    ix, iy, iw, ih = _section(c, MARGIN, cur_y + sec_h, inner_w, sec_h,
                                "5. Verlauf",
                                "Zeitlicher Verlauf während des Einsatzes")
    _write_lines(c, ix, iy, iw, n_lines=int(ih / (5.5 * mm)) - 1,
                  line_gap=5.5 * mm)


def _page_3(c):
    _head(c, 3, 4, "Maßnahmen · Verdachtsdiagnose · Übergabe-Messung")
    inner_w = PAGE_W - 2 * MARGIN
    cur_y = PAGE_H - MARGIN - 11 * mm - GAP

    sec_h = 72 * mm
    cur_y -= sec_h
    ix, iy, iw, _ = _section(c, MARGIN, cur_y + sec_h, inner_w, sec_h,
                              "6. Durchgeführte Maßnahmen",
                              "Mehrfachauswahl · Details als Freitext")
    col_w = (iw - 2 * GAP) / 3
    cols = [ix + i * (col_w + GAP) for i in range(3)]
    _cb_list(c, cols[0], iy - 2, [
        "Wundversorgung / Verband", "Blutstillung / Druckverband",
        "Kühlung", "Ruhigstellung / Schienung",
        "Lagerung", "Wärmeerhalt"], start=1)
    _cb_list(c, cols[1], iy - 2, [
        "Sauerstoffgabe", "Beatmung", "Herzdruckmassage",
        "AED angewendet", "Absaugen", "stabile Seitenlage"], start=1)
    _cb_list(c, cols[2], iy - 2, [
        "Medikamentengabe", "Zugang gelegt", "Monitoring",
        "psychische Betreuung", "Krisenintervention", "Sonstiges"],
        start=1)
    _small(c, ix, iy - 30 * mm, "Weitere Maßnahmen / Medikamente mit Dosis:",
           fill=TEXT, bold=True, size=7)
    _write_lines(c, ix, iy - 31 * mm, iw, n_lines=5, line_gap=5 * mm)

    cur_y -= GAP
    sec_h = 30 * mm
    cur_y -= sec_h
    ix, iy, iw, _ = _section(c, MARGIN, cur_y + sec_h, inner_w, sec_h,
                              "7. Verdachtsdiagnose(n)", "")
    _write_lines(c, ix, iy, iw, n_lines=4, line_gap=5.5 * mm)

    cur_y -= GAP
    sec_h = cur_y - MARGIN - 4 * mm
    cur_y -= sec_h
    ix, iy, iw, _ = _section(c, MARGIN, cur_y + sec_h, inner_w, sec_h,
                              "8. Messwerte bei Übergabe",
                              "zweite Messung")
    half = (iw - GAP) / 2
    yy = iy
    for label, unit in [("RR sys/dia", "mmHg"), ("Puls", "/min"),
                         ("SpO₂", "%"), ("AF", "/min"), ("GCS", "3–15")]:
        _small(c, ix, yy, label, fill=TEXT, size=7)
        _boxes(c, ix + 30 * mm, yy - 1, n=6, w=4 * mm, h=4.5 * mm)
        _small(c, ix + 30 * mm + 6 * 4.5 * mm, yy, unit)
        yy -= 7 * mm
    rx = ix + half + GAP
    _title(c, rx, iy, "Zustand bei Übergabe")
    yy2 = iy - 4.5 * mm
    _cb_list(c, rx, yy2, ["stabilisiert", "verbessert", "unverändert",
                           "verschlechtert"], start=1)
    yy2 -= 4 * ROW_H + 2 * mm
    _small(c, rx, yy2, "Uhrzeit Übergabe:")
    _line(c, rx + 28 * mm, yy2 - 0.6, half - 30 * mm)


def _page_4(c):
    _head(c, 4, 4, "Übergabe · Wiedervorstellung · Material · Unterschriften")
    inner_w = PAGE_W - 2 * MARGIN
    cur_y = PAGE_H - MARGIN - 11 * mm - GAP

    sec_h = 52 * mm
    cur_y -= sec_h
    half_w = (inner_w - GAP) / 2
    ix, iy, iw, _ = _section(c, MARGIN, cur_y + sec_h, half_w, sec_h,
                              "9. Übergabe an", "eine Option")
    _cb_list(c, ix, iy - 2, [
        "Notarzt", "RTW", "Klinik — Notaufnahme", "Hausarzt",
        "vor Ort belassen", "zurück zur Veranstaltung",
        "Abholung durch Eltern", "verlässt Veranstaltung",
        "Sonstige"], start=1, row_h=4.6 * mm)

    ix, iy, iw, _ = _section(c, MARGIN + half_w + GAP, cur_y + sec_h,
                              half_w, sec_h, "10. Wiedervorstellung",
                              "Patient einbestellen")
    yy = iy
    _small(c, ix, yy, "Datum")
    _line(c, ix + 16 * mm, yy - 0.6, 32 * mm)
    yy -= 7 * mm
    _title(c, ix, yy, "Wann am Tag")
    yy -= 4.5 * mm
    _cb_list(c, ix, yy, ["morgens", "mittags", "abends",
                          "nach Programmende"], start=1)
    yy -= 4 * ROW_H + 2 * mm
    _title(c, ix, yy, "Infektion bekannt")
    _cb(c, ix + 30 * mm, yy - 0.8, label="ja")
    _cb(c, ix + 40 * mm, yy - 0.8, label="nein")
    _cb(c, ix + 54 * mm, yy - 0.8, label="V. a.")

    cur_y -= GAP
    sec_h = 30 * mm
    cur_y -= sec_h
    ix, iy, iw, _ = _section(c, MARGIN, cur_y + sec_h, inner_w, sec_h,
                              "11. Verbrauchtes Material", "")
    _write_lines(c, ix, iy, iw, n_lines=4, line_gap=5.5 * mm)

    cur_y -= GAP
    sec_h = cur_y - MARGIN - 2 * mm
    cur_y -= sec_h
    ix, iy, iw, ih = _section(c, MARGIN, cur_y + sec_h, inner_w, sec_h,
                                "12. Unterschriften",
                                "Nachweis der durchgeführten Behandlung — Pflicht")
    half = (iw - GAP) / 2
    for i, x_off in enumerate((ix, ix + half + GAP)):
        c.setFont("Helvetica-Bold", 8)
        c.setFillColor(ACCENT)
        c.drawString(x_off, iy, f"Einsatzkraft {i + 1}")
        _small(c, x_off + 22 * mm, iy + 0.5, "Name (Druckschrift):")
        _line(c, x_off + 48 * mm, iy - 0.6, half - 48 * mm)
        sig_top = iy - 5 * mm
        sig_h = ih - 9 * mm
        c.setStrokeColor(BORDER_DARK)
        c.setLineWidth(0.4)
        c.rect(x_off, sig_top - sig_h, half, sig_h, stroke=1, fill=0)
        c.setFont("Helvetica", 6.5)
        c.setFillColor(MUTED)
        c.drawString(x_off + 1.5 * mm, sig_top - sig_h + 1.5 * mm,
                     "Unterschrift")
        c.drawRightString(x_off + half - 1.5 * mm,
                          sig_top - sig_h + 1.5 * mm,
                          "Datum / Uhrzeit: _______________")


def render_blanko_zeh() -> bytes:
    """4-seitiges Blanko-Notfallprotokoll (Ankreuzfelder, Tool-Design)."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    c.setTitle("zEH-Blanko-Protokoll")
    for draw in (_page_1, _page_2, _page_3, _page_4):
        draw(c)
        c.showPage()
    c.save()
    return buf.getvalue()


def render_blanko_deh() -> bytes:
    """Dezentraler Einsatzbericht als Blanko — nutzt den normalen
    dEH-Renderer mit leeren Feldern (Schreiblinien statt Werten)."""
    from pdf_export import render_protocol_pdf
    blank = {
        "id": 0,
        "laufende_nr": "________________",
        "patient_name": "",
        "patient_geburtsdatum": "",
        "patient_stammnummer": "",
        "unfall_datum_uhrzeit": "",
        "unfallort": "",
        "unfallhergang": "",
        "art_umfang_verletzung": "",
        "name_zeugen": "",
        "eh_datum_uhrzeit": "",
        "name_ersthelfer": "",
        "art_weise_massnahmen": "",
        "verbrauchtes_material": "",
        "author_full_name": "",
        "author_username": "",
        "created_at": "",
    }
    return render_protocol_pdf(blank, comments=[])
