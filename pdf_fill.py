"""
Mapping zwischen Formular-Feldern (HTML) und PDF-AcroForm-Feldern.
Befüllt das Original-PDF und gibt das Ergebnis als Bytes zurück.
"""
import io
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, BooleanObject

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


def render_pdf(data):
    """Befüllt die Vorlage mit den Daten und gibt PDF-Bytes zurück."""
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

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
