"""
Mapping zwischen Formular-Feldern (HTML) und PDF-AcroForm-Feldern.
Befüllt das Original-PDF und gibt das Ergebnis als Bytes zurück. Hängt
am Ende — wenn vorhanden — eine Unterschriften-Seite an.
"""
import base64
import io
from datetime import datetime
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, BooleanObject

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

PDF_TEMPLATE = Path(__file__).resolve().parent / "pdf.pdf"


def _join(values, sep=", "):
    if values is None:
        return ""
    if isinstance(values, list):
        return sep.join(str(v) for v in values if v is not None and str(v).strip() != "")
    return str(values)


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return "" if v is None else str(v)


def _combine(primary, sonst, sep=", "):
    """Liste/String + freitext sonstiges zusammenführen."""
    parts = []
    if primary:
        parts.append(_join(primary, sep))
    if sonst:
        parts.append(str(sonst))
    return sep.join(p for p in parts if p)


def build_field_values(d):
    """Erzeugt Mapping PDF-Feldname -> String aus HTML-Daten."""
    g = lambda k: _first(d.get(k))
    m = lambda k: d.get(k) or []

    out = {
        # Patientendaten
        "patientendaten-vorname":      g("vorname"),
        "patientendaten-nachname":     g("nachname"),
        "patientendaten-straße":       g("strasse"),
        "patientendaten-plz":          g("plz"),
        "patientendaten-ort":          g("stadt"),
        "patientendaten-krankenkasse": g("krankenkasse"),
        "patientendaten-telefonnummer": g("telefon"),
        "patientendaten-geburtsdatum": g("geburtsdatum"),
        "patientendaten-geschlecht":   g("geschlecht"),

        # Rettungstechnische Daten
        "rt-einsatznummer": g("einsatznummer"),
        "rt-alarm":         g("alarm_durch"),
        "rt-stichwort":     g("einsatzstichwort"),
        "rt-einsatzort":    g("einsatzort"),
        "rt-datum":         g("datum"),
        "rt-zeit":          g("einsatzbeginn"),
        "rt-ende":          g("einsatzende"),
        "rt-ek1":           g("einsatzkraft1"),
        "rt-ek2":           g("einsatzkraft2"),

        # Notfalldaten
        "notfallsituation":         g("notfallsituation"),
        "notfallart":               _combine(m("notfallart"), g("notfallart_sonstige")),
        "notfallart-verletzungen":  g("verletzung"),

        # Erstbefund
        "bewusstseinslage": g("bewusstsein_1"),
        "kreislauf":        _join(m("kreislauf_1")),
        "erst-EKG":         g("ekg_1"),
        "schmerzen":        f"{g('schmerzen_grad_1')}".strip(),
        "nrs":              g("nrs_1"),
        "atmung":           g("atmung_1"),
        "psych":            _combine(m("psyche"), g("psyche_sonstiges")),
        "pupille-links":    _join(m("pupille_l")),
        "pupille-rechts":   _join(m("pupille_r")),
        "haut":             _combine(m("haut"), g("haut_sonstiges")),
        "erst-sonst":       g("erstbefund_sonstiges"),

        # Messwerte 1 (Erstbefund)
        "messwerte-zeit":   g("zeit_1"),
        "messwerte-rr-sys": g("rr_sys_1"),
        "messwerte-rr-dia": g("rr_dia_1"),
        "messwerte-puls":   g("puls_1"),
        "messwerte-af":     g("af_1"),
        "messwerte-hf":     g("hf_1"),
        "messwerte-spo2":   g("spo2_1"),
        "messwerte-etco2":  g("etco2_1"),
        "messwerte-bz":     g("bz_1"),
        "messwerte-temp":   g("temp_1"),
        "erst-GCS":         g("gcs_1"),

        # Diagnose & Maßnahmen
        "erstdiagnose": g("erstdiagnose"),
        "maßnahmen":   _combine(m("massnahme"), g("massnahmen_sonstiges")),

        # Verlauf
        "verlauf": g("verlauf"),

        # Übergabe
        "übergabe-an":  g("uebergabe_an"),
        "ergebnis":     g("ergebnis"),
        "infektion":    g("infektion"),
        "rt-begleit":   g("begleitung"),
        "rt-übergabe":  g("uebergabezeit"),

        # Messwerte 2 (Übergabe)
        "ab-messwerte-zeit":   g("zeit_2"),
        "ab-messwerte-rr-sys": g("rr_sys_2"),
        "ab-messwerte-rr-dia": g("rr_dia_2"),
        "ab-messwerte-puls":   g("puls_2"),
        "ab-messwerte-af":     g("af_2"),
        "ab-messwerte-hf":     g("hf_2"),
        "ab-messwerte-spo2":   g("spo2_2"),
        "ab-messwerte-etco2":  g("etco2_2"),
        "ab-messwerte-bz":     g("bz_2"),
        "ab-messwerte-temp":   g("temp_2"),
        "ab-gs":               g("gcs_2"),
        "ab-nrs":              g("nrs_2"),
        "ab-bewusstseinslage": g("bewusstsein_2"),
        "ab-kreislauf":        _join(m("kreislauf_2")),
        "ab-ekg":              g("ekg_2"),
        "ab-atmung":           g("atmung_2"),

        # Abschluss
        "Einsatzbeschreibung": g("einsatzbeschreibung"),
        "material":            g("material"),
    }
    # Strings statt None
    return {k: ("" if v is None else str(v)) for k, v in out.items()}


def _format_dt_short(value):
    if not value:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.replace("Z", ""), fmt.replace("Z", "")
                                     ).strftime("%d.%m.%Y %H:%M")
        except (ValueError, AttributeError):
            continue
    return str(value)


def _decode_data_url(data_url: str) -> bytes | None:
    if not data_url or not isinstance(data_url, str):
        return None
    if "," not in data_url:
        return None
    try:
        return base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        return None


def _is_valid_png(data: bytes) -> bool:
    try:
        from PIL import Image as PILImage
        with PILImage.open(io.BytesIO(data)) as im:
            im.verify()
        with PILImage.open(io.BytesIO(data)) as im:
            im.load()
        return True
    except Exception:
        return False


def _build_signature_page(data) -> bytes | None:
    """Erzeugt eine A4-Seite mit den beiden Unterschriften (PNG-DataURLs).
    Gibt None zurück, wenn keine Unterschrift vorhanden ist."""
    sigs = []
    for n in (1, 2):
        sig_data = _decode_data_url(_first(data.get(f"signature_einsatzkraft{n}")))
        if not sig_data or not _is_valid_png(sig_data):
            continue
        sigs.append({
            "n": n,
            "img_bytes": sig_data,
            "name": _first(data.get(f"einsatzkraft{n}")),
            "at": _first(data.get(f"signature_einsatzkraft{n}_at")),
            "by": _first(data.get(f"signature_einsatzkraft{n}_by")),
        })
    if not sigs:
        return None

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    accent = colors.HexColor("#7A1F2B")
    c.setFillColor(accent)
    c.rect(0, height - 18 * mm, width, 18 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(20 * mm, height - 12 * mm, "Unterschriften")
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, height - 16 * mm,
                 "Nachweis der durchgeführten Behandlung")

    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    block_h = 60 * mm
    top = height - 35 * mm

    for i, s in enumerate(sigs):
        y_label = top - i * (block_h + 18 * mm)
        c.setFont("Helvetica-Bold", 11)
        label = f"Einsatzkraft {s['n']}"
        if s["name"]:
            label += f" — {s['name']}"
        c.drawString(20 * mm, y_label, label)

        # Image area
        img_x = 20 * mm
        img_y = y_label - block_h - 4 * mm
        img_w = 150 * mm
        img_h = block_h
        c.setStrokeColor(colors.lightgrey)
        c.setLineWidth(0.5)
        c.rect(img_x, img_y, img_w, img_h, stroke=1, fill=0)
        try:
            img = ImageReader(io.BytesIO(s["img_bytes"]))
            c.drawImage(img, img_x + 1 * mm, img_y + 1 * mm,
                        width=img_w - 2 * mm, height=img_h - 2 * mm,
                        preserveAspectRatio=True, anchor="c", mask="auto")
        except Exception:
            c.setFont("Helvetica-Oblique", 9)
            c.setFillColor(colors.grey)
            c.drawString(img_x + 4 * mm, img_y + img_h / 2,
                         "(Unterschrift konnte nicht gerendert werden)")
            c.setFillColor(colors.black)

        meta_parts = []
        if s["at"]:
            meta_parts.append(f"am {_format_dt_short(s['at'])}")
        if s["by"]:
            meta_parts.append(f"erfasst von {s['by']}")
        if meta_parts:
            c.setFont("Helvetica-Oblique", 8)
            c.setFillColor(colors.grey)
            c.drawString(img_x, img_y - 4 * mm, " · ".join(meta_parts))
            c.setFillColor(colors.black)

    # Footer
    c.setFont("Helvetica", 7)
    c.setFillColor(colors.grey)
    c.drawString(20 * mm, 12 * mm,
                 "Unterschriften wurden digital im Erste-Hilfe-Camp-System erfasst.")
    c.showPage()
    c.save()
    return buf.getvalue()


def render_pdf(data):
    """Befüllt die Vorlage mit den Daten und gibt PDF-Bytes zurück.

    Hängt — falls Unterschriften vorhanden sind — eine Unterschriften-
    Seite ans Ende an.
    """
    if not PDF_TEMPLATE.exists():
        raise FileNotFoundError(f"PDF-Vorlage fehlt: {PDF_TEMPLATE}")

    reader = PdfReader(str(PDF_TEMPLATE))
    writer = PdfWriter(clone_from=reader)
    values = build_field_values(data)

    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, values)
        except Exception:
            # Seiten ohne Felder ignorieren
            pass

    # NeedAppearances damit Viewer die Werte rendern
    root = writer._root_object
    if "/AcroForm" in root:
        root["/AcroForm"].update({
            NameObject("/NeedAppearances"): BooleanObject(True)
        })

    # Unterschriften-Seite anhängen, falls vorhanden
    sig_pdf_bytes = _build_signature_page(data)
    if sig_pdf_bytes:
        sig_reader = PdfReader(io.BytesIO(sig_pdf_bytes))
        for page in sig_reader.pages:
            writer.add_page(page)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
