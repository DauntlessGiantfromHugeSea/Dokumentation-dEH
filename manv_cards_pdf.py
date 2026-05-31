"""MANV-Anhängekarten als A5-PDF, doppelseitig druckbar (1 Karte pro Seite).

Pro Karte 2 PDF-Seiten:
  Vorderseite: Personalia + 4 Sichtungs-Slots + Transport + ID + QR
  Rückseite:   Kurzdiagnose + Body-Skizze + Zustand + Erst-Therapie + Bemerkungen

Bei N Karten ergibt das 2×N Seiten. Beim Druck im Duplex-Modus (Lange
Kante) entstehen so doppelseitige A5-Karten in DRK-Optik.
"""

import io
import os
from datetime import datetime as _dt

import segno
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas

ACCENT = colors.HexColor("#B3261E")          # DRK-Rot
ACCENT_DARK = colors.HexColor("#7A1F2B")
BORDER = colors.HexColor("#333333")
BORDER_LIGHT = colors.HexColor("#888888")
BG_HEAD = colors.HexColor("#E0E0E0")
BG_SOFT = colors.HexColor("#F4F1ED")
TEXT_MUTED = colors.HexColor("#555555")

S_LABEL = ParagraphStyle("Lbl", fontName="Helvetica-Bold",
                          fontSize=8, leading=10)
S_LABEL_SM = ParagraphStyle("LblSm", fontName="Helvetica",
                             fontSize=6, leading=7, textColor=TEXT_MUTED)
S_TXT = ParagraphStyle("Txt", fontName="Helvetica", fontSize=8, leading=10)


def _draw_border_box(c, x, y, w, h, label=None, label_de_en=None,
                      bg=None, line_w=0.5):
    if bg:
        c.setFillColor(bg)
        c.rect(x, y, w, h, stroke=0, fill=1)
    c.setLineWidth(line_w)
    c.setStrokeColor(BORDER)
    c.rect(x, y, w, h, stroke=1, fill=0)
    if label:
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawString(x + 2, y + h - 8, label)
        if label_de_en:
            c.setFont("Helvetica", 5.5)
            c.setFillColor(TEXT_MUTED)
            c.drawString(x + 2, y + h - 14, label_de_en)


def _draw_red_cross(c, cx, cy, size):
    """Rotes Kreuz wie auf der DRK-Karte."""
    c.setFillColor(ACCENT)
    arm = size * 0.32
    half = size / 2
    # Senkrechter Balken
    c.rect(cx - arm/2, cy - half, arm, size, stroke=0, fill=1)
    # Waagrechter Balken
    c.rect(cx - half, cy - arm/2, size, arm, stroke=0, fill=1)


def _draw_qr(c, x, y, size, content):
    """QR-Code als ReportLab-Bild mit segno → PNG-Stream."""
    qr = segno.make(content, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=10, border=0)
    buf.seek(0)
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(buf), x, y, width=size, height=size,
                preserveAspectRatio=True, mask='auto')


_BODY_IMG_PATHS = (
    os.path.join(os.path.dirname(__file__), "static", "manv_body.png"),
    os.path.join(os.path.dirname(__file__), "static", "manv_body.jpg"),
)


def _draw_body_silhouette(c, x, y, w, h):
    """Körper-Skizze (vorne + hinten) zum Markieren. Bevorzugt die echte
    Anatomie-Grafik aus static/manv_body.png; fällt auf simple Rechtecke
    zurück wenn die Datei fehlt."""
    for p in _BODY_IMG_PATHS:
        if os.path.exists(p):
            try:
                c.drawImage(ImageReader(p), x, y, width=w, height=h,
                             preserveAspectRatio=True, mask='auto')
                return
            except Exception:
                pass
    # Fallback: einfache Stick-Figuren
    c.setLineWidth(0.5)
    c.setStrokeColor(BORDER)
    # Linke Figur (vorne)
    fig_w = w / 2 - 4
    cx = x + fig_w / 2
    head_r = fig_w * 0.18
    head_cy = y + h - head_r - 2
    c.circle(cx, head_cy, head_r, stroke=1, fill=0)
    # Torso
    torso_w = fig_w * 0.55
    torso_h = h * 0.40
    torso_y = head_cy - head_r - torso_h - 1
    c.rect(cx - torso_w/2, torso_y, torso_w, torso_h, stroke=1, fill=0)
    # Arme (zwei schmale Rechtecke)
    arm_w = fig_w * 0.13
    arm_h = torso_h * 0.92
    c.rect(cx - torso_w/2 - arm_w - 1, torso_y + 2, arm_w, arm_h, stroke=1, fill=0)
    c.rect(cx + torso_w/2 + 1, torso_y + 2, arm_w, arm_h, stroke=1, fill=0)
    # Beine
    leg_w = torso_w * 0.42
    leg_h = h * 0.30
    leg_y = torso_y - leg_h - 1
    c.rect(cx - torso_w/2 + 1, leg_y, leg_w, leg_h, stroke=1, fill=0)
    c.rect(cx + 0.5, leg_y, leg_w, leg_h, stroke=1, fill=0)
    c.setFont("Helvetica", 5.5)
    c.setFillColor(TEXT_MUTED)
    c.drawCentredString(cx, y + 1, "vorne")

    # Rechte Figur (hinten) — gleiche Form
    cx2 = x + w / 2 + 2 + fig_w / 2
    head_cy2 = y + h - head_r - 2
    c.circle(cx2, head_cy2, head_r, stroke=1, fill=0)
    c.rect(cx2 - torso_w/2, torso_y, torso_w, torso_h, stroke=1, fill=0)
    c.rect(cx2 - torso_w/2 - arm_w - 1, torso_y + 2, arm_w, arm_h, stroke=1, fill=0)
    c.rect(cx2 + torso_w/2 + 1, torso_y + 2, arm_w, arm_h, stroke=1, fill=0)
    c.rect(cx2 - torso_w/2 + 1, leg_y, leg_w, leg_h, stroke=1, fill=0)
    c.rect(cx2 + 0.5, leg_y, leg_w, leg_h, stroke=1, fill=0)
    c.drawCentredString(cx2, y + 1, "hinten")


def _draw_card_front(c, card, event, base_url):
    """A5 portrait, 148×210mm. Layout in mm-Koordinaten von unten-links."""
    W, H = A5
    margin = 6 * mm
    inner_w = W - 2 * margin
    # ----- Header: Roter Streifen mit Logo, Titel, Patienten-Nr/QR -----
    header_h = 18 * mm
    header_y = H - margin - header_h
    # Roter Hintergrund-Streifen
    c.setFillColor(colors.HexColor("#FAEDEB"))
    c.rect(margin, header_y, inner_w, header_h, stroke=0, fill=1)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.rect(margin, header_y, inner_w, header_h, stroke=1, fill=0)
    # Linkes Kreuz
    _draw_red_cross(c, margin + 8 * mm, header_y + header_h / 2, 8 * mm)
    # Titel
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(W / 2, header_y + header_h - 6 * mm,
                         "Anhängekarte für Verletzte / Kranke")
    c.setFont("Helvetica", 6.5)
    c.setFillColor(TEXT_MUTED)
    c.drawCentredString(W / 2, header_y + header_h - 10 * mm,
                         "Registration card for injured/sick persons — "
                         "Fiche d'enregistrement pour blessés/malades")
    # Patienten-Nr-Box + QR rechts
    qr_size = 18 * mm
    qr_x = W - margin - qr_size - 2
    qr_y = header_y + (header_h - qr_size) / 2 - 1
    # Box um QR + ID
    id_box_w = qr_size + 32 * mm
    id_box_x = qr_x - 32 * mm
    c.setStrokeColor(ACCENT)
    c.setLineWidth(1.0)
    c.rect(id_box_x, qr_y - 1, id_box_w, qr_size + 2, stroke=1, fill=0)
    # QR-Code
    qr_url = f"{base_url}/manv/scan/{card['qr_token']}"
    _draw_qr(c, qr_x, qr_y, qr_size, qr_url)
    # ID-Klartext links neben QR
    c.setFont("Helvetica-Bold", 6.5)
    c.setFillColor(ACCENT_DARK)
    c.drawString(id_box_x + 2, qr_y + qr_size - 4, "Patienten-Nr.")
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(colors.black)
    c.drawString(id_box_x + 2, qr_y + qr_size / 2 + 1, card["card_no"])
    c.setFont("Helvetica", 5.5)
    c.setFillColor(TEXT_MUTED)
    c.drawString(id_box_x + 2, qr_y + qr_size / 2 - 4, "Scan → Detail")
    # Rechtes Kreuz wäre redundant; wir lassen es weg (QR übernimmt)

    # ----- Personalia-Block -----
    cur_y = header_y - 2
    block_h = 30 * mm
    cur_y -= block_h
    _draw_border_box(c, margin, cur_y, inner_w, block_h)
    # Name + Patienten-Nr-Spalte
    name_w = inner_w * 0.62
    sex_w = inner_w - name_w
    # Trennlinie zur Geschlecht-Spalte
    c.setLineWidth(0.4)
    c.line(margin + name_w, cur_y, margin + name_w, cur_y + block_h)
    # Name
    c.setFont("Helvetica-Bold", 7); c.setFillColor(colors.black)
    c.drawString(margin + 2, cur_y + block_h - 6, "Name")
    c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
    c.drawString(margin + 2, cur_y + block_h - 11, "Name / Nom")
    if card.get("name"):
        c.setFont("Helvetica", 11); c.setFillColor(colors.black)
        c.drawString(margin + 26 * mm, cur_y + block_h - 8, card["name"])
    c.setLineWidth(0.3); c.setStrokeColor(BORDER_LIGHT)
    c.line(margin + 2, cur_y + block_h - 13.5, margin + name_w - 2,
            cur_y + block_h - 13.5)
    # Vorname
    c.setFont("Helvetica-Bold", 7); c.setFillColor(colors.black)
    c.drawString(margin + 2, cur_y + block_h - 18, "Vorname")
    c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
    c.drawString(margin + 2, cur_y + block_h - 23, "First name / Prénom")
    if card.get("vorname"):
        c.setFont("Helvetica", 11); c.setFillColor(colors.black)
        c.drawString(margin + 26 * mm, cur_y + block_h - 20, card["vorname"])
    c.line(margin + 2, cur_y + block_h - 25.5, margin + name_w - 2,
            cur_y + block_h - 25.5)
    # Geburtsdatum
    c.setFont("Helvetica-Bold", 7); c.setFillColor(colors.black)
    c.drawString(margin + 2, cur_y + 4, "Geburtsdatum / Alter")
    c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
    c.drawString(margin + 2, cur_y + -0.5, "Date of birth / age")
    if card.get("geburtsdatum"):
        c.setFont("Helvetica", 10); c.setFillColor(colors.black)
        c.drawString(margin + 36 * mm, cur_y + 3, str(card["geburtsdatum"]))
    # Geschlecht-Spalte (♂ / ♀)
    c.setFont("Helvetica-Bold", 16)
    cx = margin + name_w + sex_w / 4
    c.drawCentredString(cx, cur_y + block_h - 14, "♂")
    c.setFont("Helvetica", 6); c.drawCentredString(cx, cur_y + block_h - 22, "m")
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(margin + name_w + sex_w * 3 / 4,
                         cur_y + block_h - 14, "♀")
    c.setFont("Helvetica", 6)
    c.drawCentredString(margin + name_w + sex_w * 3 / 4,
                         cur_y + block_h - 22, "f")
    # Selektion einkreisen
    if card.get("geschlecht") in ("m", "w", "f"):
        c.setStrokeColor(ACCENT); c.setLineWidth(1.5)
        if card["geschlecht"] == "m":
            c.circle(cx, cur_y + block_h - 16, 6 * mm, stroke=1, fill=0)
        else:
            c.circle(margin + name_w + sex_w * 3 / 4,
                      cur_y + block_h - 16, 6 * mm, stroke=1, fill=0)
        c.setStrokeColor(BORDER)
    # Datum (Karte ausgegeben)
    c.setFont("Helvetica-Bold", 7); c.setFillColor(colors.black)
    c.drawString(margin + name_w + 2, cur_y + 4, "Datum")
    c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
    c.drawString(margin + name_w + 2, cur_y - 0.5, "Date")

    # ----- Sichtungs-Tabelle -----
    cur_y -= 2
    sicht_h = 50 * mm
    cur_y -= sicht_h
    cols = 5  # Kategorie + 4 Sichtungen
    col_w = inner_w / cols
    # Außenrahmen
    _draw_border_box(c, margin, cur_y, inner_w, sicht_h)
    # Header-Zeile
    head_h = 8 * mm
    c.setFillColor(BG_HEAD)
    c.rect(margin, cur_y + sicht_h - head_h, inner_w, head_h, stroke=0, fill=1)
    c.setStrokeColor(BORDER); c.setLineWidth(0.4)
    c.line(margin, cur_y + sicht_h - head_h, margin + inner_w,
            cur_y + sicht_h - head_h)
    headers = [("Sichtung\nKategorie", "Sorting / Triage\nCategory"),
                ("1. Sichtung", "Uhrzeit / Name"),
                ("2. Sichtung", "Uhrzeit / Name"),
                ("3. Sichtung", "Uhrzeit / Name"),
                ("4. Sichtung", "Uhrzeit / Name")]
    for i, (de, en) in enumerate(headers):
        x0 = margin + i * col_w
        if i > 0:
            c.line(x0, cur_y, x0, cur_y + sicht_h)
        c.setFont("Helvetica-Bold", 7); c.setFillColor(colors.black)
        c.drawString(x0 + 2, cur_y + sicht_h - 4, de.split("\n")[0])
        c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
        c.drawString(x0 + 2, cur_y + sicht_h - 7.5, en.split("\n")[0])
    # 5 Kategorie-Zeilen: I, II, III, IV + Tot
    row_h = (sicht_h - head_h) / 5
    cat_colors = {"I": colors.HexColor("#FCE4E4"),
                  "II": colors.HexColor("#FFF6D5"),
                  "III": colors.HexColor("#E5F4EA"),
                  "IV": colors.HexColor("#DFE9F4"),
                  "tot": colors.HexColor("#222222")}
    cat_labels = [("I", "I"), ("II", "II"), ("III", "III"),
                  ("IV", "IV"), ("tot", "")]
    for i, (key, label) in enumerate(cat_labels):
        row_y = cur_y + sicht_h - head_h - (i + 1) * row_h
        # Kategorie-Spalte einfärben
        c.setFillColor(cat_colors[key])
        c.rect(margin, row_y, col_w, row_h, stroke=0, fill=1)
        c.setFillColor(colors.black if key != "tot" else colors.white)
        c.setFont("Helvetica-Bold", 14)
        c.drawCentredString(margin + col_w / 2, row_y + row_h / 2 - 4, label)
        # Trennlinie unten
        c.setStrokeColor(BORDER); c.setLineWidth(0.3)
        if i < len(cat_labels) - 1:
            c.line(margin, row_y, margin + inner_w, row_y)
    # Bereits eingetragene Sichtungen einkreisen
    import json as _json
    try:
        sichtungen = _json.loads(card.get("sichtungen_json") or "[]")
    except Exception:
        sichtungen = []
    cat_to_row = {"I": 0, "II": 1, "III": 2, "IV": 3, "tot": 4}
    for idx, s in enumerate(sichtungen[:4]):
        ri = cat_to_row.get(s.get("kategorie"))
        if ri is None:
            continue
        row_y = cur_y + sicht_h - head_h - (ri + 1) * row_h
        col_x = margin + (idx + 1) * col_w
        c.setStrokeColor(ACCENT); c.setLineWidth(1.2)
        c.circle(col_x + col_w / 2, row_y + row_h / 2, row_h / 2 - 2,
                  stroke=1, fill=0)
        c.setFillColor(colors.black); c.setFont("Helvetica", 6)
        t = (s.get("time") or "")[11:16]
        n = (s.get("name") or "")[:14]
        c.drawCentredString(col_x + col_w / 2, row_y + row_h / 2 + 1, t)
        c.drawCentredString(col_x + col_w / 2, row_y + row_h / 2 - 5, n)

    # ----- Transport + Innenliegende Suchdienstkarte -----
    cur_y -= 2
    trans_h = 24 * mm
    cur_y -= trans_h
    _draw_border_box(c, margin, cur_y, inner_w, trans_h)
    half_w = inner_w / 2
    c.setLineWidth(0.4); c.setStrokeColor(BORDER)
    c.line(margin + half_w, cur_y + trans_h / 2,
            margin + inner_w, cur_y + trans_h / 2)
    c.line(margin + half_w, cur_y, margin + half_w, cur_y + trans_h)
    # Transportmittel
    c.setFont("Helvetica-Bold", 7); c.setFillColor(colors.black)
    c.drawString(margin + 2, cur_y + trans_h - 4, "Transportmittel")
    c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
    c.drawString(margin + 2, cur_y + trans_h - 8, "Transportation")
    if card.get("transport_mittel"):
        c.setFont("Helvetica", 9); c.setFillColor(colors.black)
        c.drawString(margin + 2, cur_y + trans_h / 2 + 2,
                      card["transport_mittel"])
    # Transportziel
    c.setFont("Helvetica-Bold", 7); c.setFillColor(colors.black)
    c.drawString(margin + half_w + 2, cur_y + trans_h - 4, "Transportziel")
    c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
    c.drawString(margin + half_w + 2, cur_y + trans_h - 8, "Destination")
    if card.get("transport_ziel"):
        c.setFont("Helvetica", 9); c.setFillColor(colors.black)
        c.drawString(margin + half_w + 2, cur_y + trans_h / 2 + 2,
                      card["transport_ziel"])
    # Untere Hälfte: Liegend / Sitzend / Arzt / isoliert / Prio
    c.setFont("Helvetica-Bold", 7); c.setFillColor(colors.black)
    c.drawString(margin + 2, cur_y + trans_h / 2 - 4, "Transport:")
    options = [
        ("liegend", card.get("transport_art") == "liegend"),
        ("sitzend", card.get("transport_art") == "sitzend"),
        ("Notarzt", bool(card.get("transport_mit_arzt"))),
        ("isoliert", bool(card.get("transport_isoliert"))),
        ("Prio a", card.get("transport_prio") == "a"),
        ("Prio b", card.get("transport_prio") == "b"),
    ]
    ox = margin + 20 * mm
    oy = cur_y + 3
    for label, set_ in options:
        c.setFont("Helvetica", 7); c.setFillColor(colors.black)
        # Box
        c.setStrokeColor(BORDER); c.setLineWidth(0.4)
        c.rect(ox, oy, 3 * mm, 3 * mm, stroke=1, fill=0)
        if set_:
            c.setFillColor(ACCENT)
            c.setFont("Helvetica-Bold", 8)
            c.drawCentredString(ox + 1.5 * mm, oy + 0.5, "✓")
            c.setFillColor(colors.black)
            c.setFont("Helvetica", 7)
        c.drawString(ox + 4 * mm, oy + 0.8, label)
        ox += 22 * mm

    # ----- Footer-Quelle / Hinweis -----
    c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
    c.drawString(margin, margin - 2,
                  f"Erste Hilfe — MANV „{event['name']}" "  ·  "
                  f"Karte {card['card_no']}  ·  "
                  f"erzeugt {_dt.now().strftime('%d.%m.%Y %H:%M')}")


def _draw_card_back(c, card, event):
    W, H = A5
    margin = 6 * mm
    inner_w = W - 2 * margin

    # ----- Kurz-Diagnose-Block (oben) -----
    cur_y = H - margin
    block_h = 70 * mm
    cur_y -= block_h
    _draw_border_box(c, margin, cur_y, inner_w, block_h)
    # Titel
    c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.black)
    c.drawString(margin + 2, cur_y + block_h - 4, "Kurz-Diagnose")
    c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
    c.drawString(margin + 2, cur_y + block_h - 8, "short diagnose / diagnostic bref")
    # Body-Skizze links (40% Breite)
    body_w = inner_w * 0.42
    body_h = block_h - 14
    body_x = margin + 2
    body_y = cur_y + 2
    _draw_body_silhouette(c, body_x, body_y, body_w, body_h)
    # Diagnose-Kategorien rechts
    diag_x = margin + body_w + 6
    diag_w = inner_w - body_w - 8
    diag_items = [
        ("Verletzung", "injury / blessure", bool(card.get("diag_verletzung"))),
        ("Verbrennung", "burn / brûlure", bool(card.get("diag_verbrennung"))),
        ("Erkrankung", "disease / maladie", bool(card.get("diag_erkrankung"))),
        ("Vergiftung", "intoxication", bool(card.get("diag_vergiftung"))),
        ("Verstrahlung", "radiation", bool(card.get("diag_verstrahlung"))),
        ("Psyche", "psychic condition", bool(card.get("diag_psyche"))),
    ]
    di_h = (block_h - 14) / len(diag_items)
    for i, (de, en, set_) in enumerate(diag_items):
        iy = cur_y + block_h - 12 - (i + 1) * di_h
        c.setStrokeColor(BORDER_LIGHT); c.setLineWidth(0.3)
        c.line(diag_x, iy + di_h, diag_x + diag_w, iy + di_h)
        if set_:
            c.setFillColor(colors.HexColor("#FCE4E4"))
            c.rect(diag_x, iy, diag_w, di_h, stroke=0, fill=1)
        c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.black)
        c.drawString(diag_x + 2, iy + di_h - 5, de)
        c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
        c.drawString(diag_x + 2, iy + di_h - 9, en)
        # Häkchen rechts
        if set_:
            c.setFillColor(ACCENT); c.setFont("Helvetica-Bold", 11)
            c.drawRightString(diag_x + diag_w - 2, iy + 2, "✓")
    # Diag-Lokalisation
    if card.get("diag_lokalisation"):
        c.setFont("Helvetica", 7); c.setFillColor(colors.black)
        c.drawString(margin + 2, cur_y - 2,
                      f"Lokalisation: {card['diag_lokalisation']}")
    cur_y -= 6

    # ----- Zustand/Uhrzeit + Erst-Therapie nebeneinander -----
    zt_h = 40 * mm
    cur_y -= zt_h
    half = inner_w / 2
    # Zustand links
    _draw_border_box(c, margin, cur_y, half - 2, zt_h)
    c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.black)
    c.drawString(margin + 2, cur_y + zt_h - 4, "Zustand / Uhrzeit")
    c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
    c.drawString(margin + 2, cur_y + zt_h - 8, "state / time")
    rows = [("Bewusstsein", "consciousness", card.get("bewusstsein")),
            ("Atmung", "respiration", card.get("atmung")),
            ("Kreislauf", "circulation", card.get("kreislauf"))]
    rh = (zt_h - 12) / 3
    for i, (de, en, val) in enumerate(rows):
        ry = cur_y + zt_h - 12 - (i + 1) * rh
        c.setStrokeColor(BORDER_LIGHT); c.setLineWidth(0.3)
        c.line(margin, ry + rh, margin + half - 2, ry + rh)
        c.setFont("Helvetica-Bold", 7); c.setFillColor(colors.black)
        c.drawString(margin + 2, ry + rh - 4, de)
        c.setFont("Helvetica", 5); c.setFillColor(TEXT_MUTED)
        c.drawString(margin + 2, ry + rh - 8, en)
        # oB / ↓ Optionen
        c.setFont("Helvetica", 7); c.setFillColor(colors.black)
        ox = margin + 22 * mm
        for opt_lbl, opt_val in (("o.B.", "oB"), ("↓", "reduziert")):
            c.rect(ox, ry + 3, 2.5 * mm, 2.5 * mm, stroke=1, fill=0)
            if val == opt_val:
                c.setFillColor(ACCENT); c.setFont("Helvetica-Bold", 8)
                c.drawCentredString(ox + 1.25 * mm, ry + 3.5, "✓")
                c.setFillColor(colors.black); c.setFont("Helvetica", 7)
            c.drawString(ox + 4 * mm, ry + 3.5, opt_lbl)
            ox += 12 * mm
    # Erst-Therapie rechts
    th_x = margin + half + 2
    th_w = half - 2
    _draw_border_box(c, th_x, cur_y, th_w, zt_h)
    c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.black)
    c.drawString(th_x + 2, cur_y + zt_h - 4, "Erst-Therapie")
    c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
    c.drawString(th_x + 2, cur_y + zt_h - 8, "first therapy")
    th_items = [
        ("Infusion", "infusion", bool(card.get("th_infusion"))),
        ("Analgetika", "analgesics", bool(card.get("th_analgetika"))),
        ("Antidote", "antidots", bool(card.get("th_antidote"))),
        ("sonstige", "other drugs", bool(card.get("th_sonstige"))),
    ]
    th_rh = (zt_h - 12) / len(th_items)
    for i, (de, en, set_) in enumerate(th_items):
        ty = cur_y + zt_h - 12 - (i + 1) * th_rh
        c.setStrokeColor(BORDER_LIGHT); c.setLineWidth(0.3)
        c.line(th_x, ty + th_rh, th_x + th_w, ty + th_rh)
        c.setFont("Helvetica-Bold", 7); c.setFillColor(colors.black)
        c.drawString(th_x + 2, ty + th_rh - 4, de)
        c.setFont("Helvetica", 5); c.setFillColor(TEXT_MUTED)
        c.drawString(th_x + 2, ty + th_rh - 8, en)
        c.rect(th_x + th_w - 8 * mm, ty + 2, 3 * mm, 3 * mm, stroke=1, fill=0)
        if set_:
            c.setFillColor(ACCENT); c.setFont("Helvetica-Bold", 8)
            c.drawCentredString(th_x + th_w - 6.5 * mm, ty + 2.5, "✓")
    cur_y -= 4

    # ----- Bemerkungen-Box (Rest des Blattes) -----
    bemerk_h = cur_y - margin - 2
    cur_y = margin + 2
    _draw_border_box(c, margin, cur_y, inner_w, bemerk_h)
    c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.black)
    c.drawString(margin + 2, cur_y + bemerk_h - 4, "Bemerkungen")
    c.setFont("Helvetica", 5.5); c.setFillColor(TEXT_MUTED)
    c.drawString(margin + 2, cur_y + bemerk_h - 8, "notes / remarques")
    if card.get("bemerkungen"):
        # mehrzeilig
        c.setFont("Helvetica", 8); c.setFillColor(colors.black)
        text_obj = c.beginText(margin + 3, cur_y + bemerk_h - 14)
        for line in str(card["bemerkungen"]).splitlines()[:14]:
            text_obj.textLine(line[:90])
        c.drawText(text_obj)
    else:
        # Schreib-Linien
        c.setStrokeColor(BORDER_LIGHT); c.setLineWidth(0.3)
        line_y = cur_y + bemerk_h - 14
        while line_y > cur_y + 4:
            c.line(margin + 3, line_y, margin + inner_w - 3, line_y)
            line_y -= 6


def render_manv_cards_pdf(*, event, cards, base_url):
    """Pro Karte 2 PDF-Seiten (vorne + hinten). Duplex-Druck → fertige
    doppelseitige A5-Karte."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A5)
    c.setTitle(f"MANV-Karten {event['card_prefix']}")
    for card in cards:
        _draw_card_front(c, card, event, base_url)
        c.showPage()
        _draw_card_back(c, card, event)
        c.showPage()
    c.save()
    return buf.getvalue()


# ============ Übersichtsprotokoll / Sichtung (A4 landscape) ============

from reportlab.lib.pagesizes import A4 as _A4, landscape as _landscape

_SK_COLORS = {
    "I":   colors.HexColor("#F4A8A8"),   # rot
    "II":  colors.HexColor("#FFE680"),   # gelb
    "III": colors.HexColor("#A6E2A6"),   # grün
    "IV":  colors.HexColor("#A6CDF4"),   # blau
    "tot": colors.HexColor("#3A3A3A"),   # schwarz (EX)
}


def _ueb_header(c, event, page_num, total_pages, meta_block=False):
    """Header oben auf jeder Seite + optionaler Meta-Block (Seite 1)."""
    W, H = _landscape(_A4)
    margin = 10 * mm
    inner_w = W - 2 * margin
    # Titelzeile
    c.setStrokeColor(BORDER); c.setLineWidth(0.6)
    title_y = H - margin - 14 * mm
    c.rect(margin, title_y, inner_w, 14 * mm, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 11); c.setFillColor(colors.black)
    c.drawString(margin + 3, title_y + 4, "Übersichtsprotokoll / Sichtung")
    c.setFont("Helvetica", 9); c.setFillColor(TEXT_MUTED)
    c.drawRightString(W - margin - 3, title_y + 4,
                       f"Seite {page_num} von {total_pages}")
    cur_y = title_y - 2

    if meta_block:
        # Rote Titelleiste
        bar_h = 8 * mm
        cur_y -= bar_h
        c.setFillColor(ACCENT)
        c.rect(margin, cur_y, inner_w, bar_h, stroke=0, fill=1)
        c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 10)
        sst = event.get("card_prefix") or ""
        c.drawCentredString(W / 2, cur_y + 2,
                             f"Übersichtsprotokoll für die Sichtungsstelle „{event.get('name','')}“"
                             f" ({sst})")
        # Meta-Block
        meta_h = 28 * mm
        cur_y -= meta_h
        c.setStrokeColor(BORDER); c.setLineWidth(0.5)
        c.rect(margin, cur_y, inner_w, meta_h, stroke=1, fill=0)
        # Sichtungsort + Einsatznummer
        col_w = inner_w / 2
        c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.black)
        c.drawString(margin + 3, cur_y + meta_h - 5, "Sichtungsort")
        opts = ["Schadensraum", "Patientenablage", "Eingang BHP",
                "Ausgang BHP", "____________"]
        ox = margin + 3; oy = cur_y + meta_h - 12
        c.setFont("Helvetica", 8)
        for opt in opts:
            c.rect(ox, oy - 2, 3 * mm, 3 * mm, stroke=1, fill=0)
            c.drawString(ox + 4 * mm, oy, opt)
            ox += 28 * mm
        # Einsatznummer rechts oben
        c.setFont("Helvetica-Bold", 8)
        c.drawString(margin + col_w + 4, cur_y + meta_h - 5, "Einsatznummer")
        c.setFont("Helvetica", 9); c.setFillColor(colors.black)
        c.drawString(margin + col_w + 4, cur_y + meta_h - 11,
                      f"MANV-{event['id']:03d} · {event.get('card_prefix','')}")
        # Mittlere Zeile: Name Sichtender / Qualifikation / Name Protokoll / Beginn
        mid_y = cur_y + meta_h - 18
        c.setStrokeColor(BORDER_LIGHT); c.setLineWidth(0.3)
        c.line(margin, mid_y, margin + inner_w, mid_y)
        c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.black)
        c.drawString(margin + 3, mid_y - 4, "Name des Sichtenden")
        c.drawString(margin + col_w + 4, mid_y - 4, "Qualifikation / Funktion")
        c.setFont("Helvetica", 7); c.setFillColor(TEXT_MUTED)
        c.drawString(margin + col_w + 4, mid_y - 9,
                      "[ ] LNA   [ ] NA   [ ] RA (Vorsichtung)")
        # Untere Zeile
        bot_y = cur_y + 3
        c.setStrokeColor(BORDER_LIGHT); c.setLineWidth(0.3)
        c.line(margin, bot_y + 6, margin + inner_w, bot_y + 6)
        c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.black)
        c.drawString(margin + 3, bot_y, "Name Protokollführer")
        c.drawString(margin + col_w + 4, bot_y, "Beginn der Sichtung")
        c.setFont("Helvetica", 9)
        c.drawString(margin + col_w + 38 * mm, bot_y,
                      str(event.get("started_at") or "")[:16])
        cur_y -= 2
    return cur_y  # y where the patient table can start


def _ueb_patient_table_header(c, top_y, col_widths, col_x):
    """Header-Zeilen der Patientenübersicht-Tabelle. Liefert y nach Header."""
    W, H = _landscape(_A4)
    margin = 10 * mm
    inner_w = W - 2 * margin
    # Gelbe Leiste "Patientenübersicht"
    bar_h = 6 * mm
    bar_y = top_y - bar_h
    c.setFillColor(colors.HexColor("#FFE680"))
    c.rect(margin, bar_y, inner_w, bar_h, stroke=0, fill=1)
    c.setStrokeColor(BORDER); c.setLineWidth(0.4)
    c.rect(margin, bar_y, inner_w, bar_h, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 9); c.setFillColor(colors.black)
    c.drawCentredString(W / 2, bar_y + 1.5, "Patientenübersicht")
    # Spalten-Header
    head_h = 12 * mm
    head_y = bar_y - head_h
    c.setFillColor(colors.HexColor("#F4F1ED"))
    c.rect(margin, head_y, inner_w, head_h, stroke=1, fill=1)
    headers = ["Nr. / Label", "♂♀", "Kategorie", "Sofort.\nTransport",
                "Ziel-Einrichtung", "Diagnose", "Transport-\nmittel", "Zeit"]
    cumulative = margin
    for i, (w, label) in enumerate(zip(col_widths, headers)):
        if i > 0:
            c.line(cumulative, head_y, cumulative, head_y + head_h)
        c.setFont("Helvetica-Bold", 7.5); c.setFillColor(colors.black)
        for ln_idx, ln in enumerate(label.split("\n")):
            c.drawString(cumulative + 2, head_y + head_h - 5 - ln_idx * 4, ln)
        cumulative += w
    # Sub-Header für Kategorie: I/II/III/IV/EX-Markierungen
    cat_x_start = col_x[2]
    cat_w = col_widths[2]
    sub_h = 4 * mm
    sub_y = head_y
    cell_w = cat_w / 5
    cats = [("I", _SK_COLORS["I"]), ("II", _SK_COLORS["II"]),
            ("III", _SK_COLORS["III"]), ("IV", _SK_COLORS["IV"]),
            ("EX", _SK_COLORS["tot"])]
    for i, (lbl, col) in enumerate(cats):
        x0 = cat_x_start + i * cell_w
        c.setFillColor(col)
        c.rect(x0, sub_y, cell_w, sub_h, stroke=1, fill=1)
        c.setFillColor(colors.white if lbl == "EX" else colors.black)
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(x0 + cell_w / 2, sub_y + 1, lbl)
    return head_y


def _ueb_patient_row(c, x_start, y_top, row_h, col_widths, col_x, card):
    """Zeichne eine Patient-Zeile. Liefert y nach der Zeile."""
    y = y_top - row_h
    # Außen-Linie
    c.setStrokeColor(BORDER); c.setLineWidth(0.3)
    c.line(x_start, y, x_start + sum(col_widths), y)
    # Spaltentrenner
    cum = x_start
    for w in col_widths[:-1]:
        cum += w
        c.line(cum, y, cum, y + row_h)
    # Spalte 1: Karten-Nr.
    c.setFont("Helvetica-Bold", 9); c.setFillColor(colors.black)
    c.drawString(x_start + 3, y + row_h - 6, card.get("card_no", ""))
    # Spalte 2: Geschlecht ♂/♀
    gx = col_x[1] + col_widths[1] / 2
    c.setFont("Helvetica-Bold", 12)
    gesch = (card.get("geschlecht") or "").lower()
    color_m = ACCENT if gesch == "m" else BORDER_LIGHT
    color_f = ACCENT if gesch in ("w", "f") else BORDER_LIGHT
    c.setFillColor(color_m); c.drawCentredString(gx, y + row_h - 6, "♂")
    c.setFillColor(color_f); c.drawCentredString(gx, y + 3, "♀")
    # Spalte 3: Kategorie I/II/III/IV/EX
    cat_w = col_widths[2]
    cell_w = cat_w / 5
    cat = (card.get("sichtung_kategorie") or "").upper()
    cat_map = {"I": 0, "II": 1, "III": 2, "IV": 3, "TOT": 4}
    for i, (lbl, col_key) in enumerate([("I","I"),("II","II"),("III","III"),
                                          ("IV","IV"),("EX","tot")]):
        x0 = col_x[2] + i * cell_w
        c.setFillColor(_SK_COLORS[col_key])
        c.rect(x0, y, cell_w, row_h, stroke=1, fill=1)
    # Markierung
    if cat in cat_map:
        idx = cat_map[cat]
        x0 = col_x[2] + idx * cell_w + cell_w / 2
        c.setStrokeColor(ACCENT_DARK); c.setLineWidth(1.6)
        c.circle(x0, y + row_h / 2, min(row_h, cell_w) / 2 - 2,
                  stroke=1, fill=0)
    # Spalte 4: Sofortiger Transport (ja/nein)
    ja_x = col_x[3] + 3
    c.setFont("Helvetica", 8); c.setFillColor(colors.black)
    c.rect(ja_x, y + row_h - 7, 2.5 * mm, 2.5 * mm, stroke=1, fill=0)
    c.drawString(ja_x + 3 * mm, y + row_h - 6, "ja")
    c.rect(ja_x, y + 3, 2.5 * mm, 2.5 * mm, stroke=1, fill=0)
    c.drawString(ja_x + 3 * mm, y + 4, "nein")
    # Markieren wenn prio == 'a'
    if (card.get("transport_prio") or "").lower() == "a":
        c.setFillColor(ACCENT); c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(ja_x + 1.25 * mm, y + row_h - 6.5, "✓")
    elif card.get("transport_mittel") or card.get("transport_ziel"):
        c.setFillColor(ACCENT); c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(ja_x + 1.25 * mm, y + 3.5, "✓")
    # Spalte 5: Ziel-Einrichtung
    c.setFont("Helvetica", 8); c.setFillColor(colors.black)
    if card.get("transport_ziel"):
        c.drawString(col_x[4] + 3, y + row_h - 6,
                      str(card["transport_ziel"])[:25])
    # Spalte 6: Diagnose — sammele Flags + Lokalisation
    diag_parts = []
    for k, lbl in [("diag_verletzung","Verletzung"),
                    ("diag_verbrennung","Verbrennung"),
                    ("diag_erkrankung","Erkrankung"),
                    ("diag_vergiftung","Vergiftung"),
                    ("diag_verstrahlung","Verstrahlung"),
                    ("diag_psyche","Psyche")]:
        if card.get(k):
            diag_parts.append(lbl)
    diag_str = ", ".join(diag_parts)
    if card.get("diag_lokalisation"):
        diag_str += (" — " if diag_str else "") + card["diag_lokalisation"]
    if card.get("bemerkungen"):
        diag_str += (" — " if diag_str else "") + card["bemerkungen"]
    if diag_str:
        c.setFont("Helvetica", 8); c.setFillColor(colors.black)
        # Bei langen Strings auf zwei Zeilen umbrechen
        max_chars = 50
        if len(diag_str) <= max_chars:
            c.drawString(col_x[5] + 3, y + row_h - 6, diag_str)
        else:
            c.drawString(col_x[5] + 3, y + row_h - 5, diag_str[:max_chars])
            c.drawString(col_x[5] + 3, y + row_h - 10,
                          diag_str[max_chars:max_chars*2])
    # Spalte 7: Transport-Mittel
    if card.get("transport_mittel"):
        c.setFont("Helvetica", 8); c.setFillColor(colors.black)
        c.drawString(col_x[6] + 3, y + row_h - 6,
                      str(card["transport_mittel"])[:14])
    # Spalte 8: Transport-Zeit (updated_at wenn transportiert)
    if card.get("status") in ("transportiert", "abgeschlossen"):
        t = str(card.get("updated_at") or "")[11:16]
        if t:
            c.setFont("Helvetica", 8); c.setFillColor(colors.black)
            c.drawString(col_x[7] + 3, y + row_h - 6, t)
    return y


def _ueb_gesamtuebersicht(c, y_top, event, cards):
    """Footer-Block am Ende der letzten Seite: Counts pro SK + Doku-Ende."""
    W, H = _landscape(_A4)
    margin = 10 * mm
    inner_w = W - 2 * margin
    # Titel-Leiste
    bar_h = 6 * mm
    bar_y = y_top - bar_h
    c.setFillColor(colors.HexColor("#FFE680"))
    c.rect(margin, bar_y, inner_w, bar_h, stroke=0, fill=1)
    c.setStrokeColor(BORDER); c.setLineWidth(0.4)
    c.rect(margin, bar_y, inner_w, bar_h, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 9); c.setFillColor(colors.black)
    c.drawCentredString(W / 2, bar_y + 1.5, "Gesamtübersicht")
    # Counts pro Kategorie
    counts = {"I":0, "II":0, "III":0, "IV":0, "tot":0, "—":0}
    for card in cards:
        k = (card.get("sichtung_kategorie") or "—")
        counts[k] = counts.get(k, 0) + 1
    total = sum(counts.values())
    # Linie: Anzahl Patienten + Anzahl Betroffene
    info_h = 8 * mm
    info_y = bar_y - info_h
    c.setStrokeColor(BORDER); c.setLineWidth(0.4)
    c.rect(margin, info_y, inner_w, info_h, stroke=1, fill=0)
    half = inner_w / 2
    c.line(margin + half, info_y, margin + half, info_y + info_h)
    c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.black)
    c.drawString(margin + 3, info_y + info_h - 3, "Anzahl Patienten")
    c.drawString(margin + half + 4, info_y + info_h - 3,
                  "Anzahl Betroffene (geschätzt)")
    c.setFont("Helvetica", 11)
    c.drawString(margin + 38 * mm, info_y + 2, str(total))
    # SK-Boxen
    sk_h = 9 * mm
    sk_y = info_y - sk_h
    cells = [("SK I", "I", _SK_COLORS["I"], colors.black),
              ("SK II", "II", _SK_COLORS["II"], colors.black),
              ("SK III", "III", _SK_COLORS["III"], colors.black),
              ("SK IV", "IV", _SK_COLORS["IV"], colors.black),
              ("EX", "tot", _SK_COLORS["tot"], colors.white)]
    cw = inner_w / 5
    for i, (lbl, key, bg, fg) in enumerate(cells):
        cx = margin + i * cw
        c.setFillColor(bg)
        c.rect(cx, sk_y, cw, sk_h, stroke=1, fill=1)
        c.setFillColor(fg); c.setFont("Helvetica-Bold", 9)
        c.drawString(cx + 3, sk_y + sk_h - 4, lbl)
        c.setFont("Helvetica-Bold", 14)
        c.drawRightString(cx + cw - 4, sk_y + 2, str(counts.get(key, 0)))
    # Dokumentationsende
    end_bar_y = sk_y - 6 * mm
    c.setFillColor(colors.HexColor("#FFE680"))
    c.rect(margin, end_bar_y, inner_w, 6 * mm, stroke=1, fill=1)
    c.setStrokeColor(BORDER); c.setLineWidth(0.4)
    c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(W / 2, end_bar_y + 1.5, "Dokumentationsende")
    end_h = 10 * mm
    end_y = end_bar_y - end_h
    c.rect(margin, end_y, inner_w, end_h, stroke=1, fill=0)
    third = inner_w / 3
    c.line(margin + third, end_y, margin + third, end_y + end_h)
    c.line(margin + 2 * third, end_y, margin + 2 * third, end_y + end_h)
    c.setFont("Helvetica-Bold", 8); c.setFillColor(colors.black)
    c.drawString(margin + 3, end_y + end_h - 3, "Datum")
    c.drawString(margin + third + 4, end_y + end_h - 3, "Uhrzeit")
    c.drawString(margin + 2 * third + 4, end_y + end_h - 3, "Unterschrift")
    # Datum/Uhrzeit aus updated_at der letzten Karte
    if cards:
        last_ts = max((str(c.get("updated_at") or "") for c in cards),
                       default="")
        c.setFont("Helvetica", 10)
        c.drawString(margin + 18 * mm, end_y + 3, last_ts[:10])
        c.drawString(margin + third + 22 * mm, end_y + 3, last_ts[11:16])


def render_uebersichtsprotokoll_pdf(*, event, cards):
    """Übersichtsprotokoll / Sichtung als A4-landscape-PDF.
    Patientenliste pro Seite passend, am Ende Gesamtübersicht."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=_landscape(_A4))
    c.setTitle(f"MANV-Übersichtsprotokoll {event.get('card_prefix','')}")
    W, H = _landscape(_A4)
    margin = 10 * mm
    inner_w = W - 2 * margin

    # Spaltenbreiten der Patiententabelle (Summe = inner_w = 277mm)
    col_widths = [22 * mm, 12 * mm, 50 * mm, 18 * mm, 36 * mm,
                  98 * mm, 26 * mm, 15 * mm]
    col_x = []
    cum = margin
    for w in col_widths:
        col_x.append(cum)
        cum += w

    # Wie viele Patienten passen pro Seite?
    # Seite 1 ist enger (Meta-Block), Seiten 2+ haben mehr Platz.
    row_h = 14 * mm
    cards_per_first = 4
    cards_per_other = 8
    # Auf der letzten Seite muss die Gesamtübersicht (~38mm) Platz haben.
    cards_per_last = 4

    if not cards:
        # Kein Patient → trotzdem Seite 1 mit Meta-Block + leere Tabelle
        total_pages = 1
        cur_y = _ueb_header(c, event, 1, total_pages, meta_block=True)
        head_bottom = _ueb_patient_table_header(c, cur_y, col_widths, col_x)
        # 4 Leerzeilen für handschriftliche Einträge
        cy = head_bottom
        for _ in range(cards_per_first):
            cy = _ueb_patient_row(c, margin, cy, row_h, col_widths, col_x, {})
        _ueb_gesamtuebersicht(c, cy - 4, event, cards)
        c.save()
        return buf.getvalue()

    # Berechne Seitenanzahl
    remaining = len(cards)
    pages = []
    # Seite 1: bis cards_per_first
    pages.append(cards[:cards_per_first])
    remaining = cards[cards_per_first:]
    # Mittlere Seiten: je cards_per_other
    while len(remaining) > cards_per_last:
        pages.append(remaining[:cards_per_other])
        remaining = remaining[cards_per_other:]
    # Letzte Seite mit Gesamtübersicht
    pages.append(remaining)
    total_pages = len(pages)

    for pi, page_cards in enumerate(pages):
        is_first = (pi == 0)
        is_last = (pi == total_pages - 1)
        cur_y = _ueb_header(c, event, pi + 1, total_pages,
                             meta_block=is_first)
        head_bottom = _ueb_patient_table_header(c, cur_y, col_widths, col_x)
        cy = head_bottom
        for card in page_cards:
            cy = _ueb_patient_row(c, margin, cy, row_h, col_widths,
                                    col_x, card)
        # Wenn letzte Seite und genug Platz: Leerzeilen + Gesamtübersicht
        if is_last:
            # Mindestens 1 Leerzeile damit Tabellenrand sauber ist
            while cy - row_h > margin + 50 * mm and len(page_cards) < cards_per_last:
                cy = _ueb_patient_row(c, margin, cy, row_h, col_widths,
                                        col_x, {})
                page_cards = page_cards + [None]
            _ueb_gesamtuebersicht(c, cy - 4, event, cards)
        c.showPage()
    c.save()
    return buf.getvalue()
