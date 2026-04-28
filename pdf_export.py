"""PDF rendering for individual Einsatzberichte using ReportLab."""

from __future__ import annotations

import io
from typing import Iterable, Mapping

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from models import format_dt


ACCENT = colors.HexColor("#7A1F2B")


def _styles():
    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle(
            "Title", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=18, textColor=ACCENT, spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle", parent=base["Normal"], fontSize=10,
            textColor=colors.grey, spaceAfter=12,
        ),
        "section": ParagraphStyle(
            "Section", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=12, textColor=colors.white, leading=16,
        ),
        "label": ParagraphStyle(
            "Label", parent=base["Normal"], fontName="Helvetica",
            fontSize=8, textColor=colors.grey, leading=10,
        ),
        "value": ParagraphStyle(
            "Value", parent=base["Normal"], fontName="Helvetica",
            fontSize=10, leading=13,
        ),
        "comment": ParagraphStyle(
            "Comment", parent=base["Normal"], fontName="Helvetica",
            fontSize=9, leading=12,
        ),
        "comment_meta": ParagraphStyle(
            "CommentMeta", parent=base["Normal"], fontName="Helvetica-Oblique",
            fontSize=8, textColor=colors.grey, leading=10, spaceAfter=2,
        ),
    }
    return styles


def _section_header(title: str, styles) -> Table:
    tbl = Table([[Paragraph(title, styles["section"])]], colWidths=[170 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return tbl


def _kv_row(pairs: Iterable[tuple[str, str]], styles, col_widths) -> Table:
    """Single row of label/value cells."""
    label_cells = [Paragraph(label, styles["label"]) for label, _ in pairs]
    value_cells = [Paragraph((value or "—").replace("\n", "<br/>"),
                              styles["value"]) for _, value in pairs]
    data = [label_cells, value_cells]
    tbl = Table(data, colWidths=col_widths)
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.4, colors.lightgrey),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4F4F4")),
    ]))
    return tbl


def _full_row(label: str, value: str, styles) -> Table:
    """Full-width label/value block, value can be multi-line."""
    data = [
        [Paragraph(label, styles["label"])],
        [Paragraph((value or "—").replace("\n", "<br/>"), styles["value"])],
    ]
    tbl = Table(data, colWidths=[170 * mm])
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.4, colors.lightgrey),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4F4F4")),
    ]))
    return tbl


def render_protocol_pdf(protocol: Mapping, comments: Iterable[Mapping]) -> bytes:
    """Render a single Einsatzbericht to PDF and return raw bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"Einsatzbericht #{protocol['id']}",
    )
    styles = _styles()
    story: list = []

    story.append(Paragraph(f"Einsatzbericht #{protocol['id']}", styles["title"]))
    story.append(Paragraph(
        "Dokumentation von Erste-Hilfe-Leistungen (gem. DGUV Information 1)",
        styles["subtitle"],
    ))

    # Patient
    story.append(_section_header("Name der verletzten bzw. erkrankten Person", styles))
    story.append(_kv_row([
        ("Name", protocol["patient_name"]),
        ("Geburtsdatum", format_dt(protocol["patient_geburtsdatum"])),
        ("Stammnummer", protocol["patient_stammnummer"] or ""),
        ("Laufende Nr.", protocol["laufende_nr"] or ""),
        ("dEH", protocol["deh"] or ""),
    ], styles, col_widths=[40 * mm, 35 * mm, 35 * mm, 30 * mm, 30 * mm]))
    story.append(Spacer(1, 6))

    # Hergang
    story.append(_section_header(
        "Angaben zum Hergang des Unfalls bzw. des Gesundheitsschadens", styles))
    story.append(_kv_row([
        ("Datum und Uhrzeit", format_dt(protocol["unfall_datum_uhrzeit"])),
        ("Unfallort", protocol["unfallort"] or ""),
    ], styles, col_widths=[60 * mm, 110 * mm]))
    story.append(_full_row("Unfallhergang", protocol["unfallhergang"] or "", styles))
    story.append(_kv_row([
        ("Art und Umfang der Verletzung bzw. der Erkrankung",
         protocol["art_umfang_verletzung"] or ""),
        ("Name der Zeugen", protocol["name_zeugen"] or ""),
    ], styles, col_widths=[110 * mm, 60 * mm]))
    story.append(Spacer(1, 6))

    # Erste-Hilfe-Leistung
    story.append(_section_header("Erste Hilfe Leistung", styles))
    story.append(_kv_row([
        ("Datum und Uhrzeit", format_dt(protocol["eh_datum_uhrzeit"])),
        ("Name des Ersthelfers / der Ersthelferin",
         protocol["name_ersthelfer"] or ""),
    ], styles, col_widths=[60 * mm, 110 * mm]))
    story.append(_full_row(
        "Art und Weise der Erste-Hilfe-Maßnahmen",
        protocol["art_weise_massnahmen"] or "",
        styles,
    ))
    story.append(_full_row(
        "Verbrauchtes Material",
        protocol["verbrauchtes_material"] or "",
        styles,
    ))
    story.append(Spacer(1, 10))

    # Comments
    comments = list(comments)
    if comments:
        story.append(_section_header("Kommentare", styles))
        for c in comments:
            author = c["author_full_name"] or c["author_username"] or "—"
            meta = f"{author} · {format_dt(c['created_at'])}"
            story.append(Paragraph(meta, styles["comment_meta"]))
            story.append(Paragraph(
                (c["text"] or "").replace("\n", "<br/>"), styles["comment"],
            ))
            story.append(Spacer(1, 4))

    # Footer with metadata
    story.append(Spacer(1, 12))
    author = protocol["author_full_name"] or protocol["author_username"] or "—"
    story.append(Paragraph(
        f"Eintrag erstellt von {author} am {format_dt(protocol['created_at'])}",
        styles["comment_meta"],
    ))

    doc.build(story)
    return buf.getvalue()
