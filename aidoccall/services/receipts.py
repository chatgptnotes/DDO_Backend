"""
On-demand PDF receipts for paid appointments.

`fetch_receipt_data` loads everything a receipt needs in one query
(appointment + doctor + latest succeeded Stripe intent);
`build_receipt_pdf` renders it with reportlab. Nothing is persisted —
the bytes go straight into the HTTP response.

Scope: a *payment receipt*, deliberately not a tax invoice (no GSTIN /
billing-address chain — those fields don't exist in the data model yet).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from io import BytesIO
from xml.sax.saxutils import escape as _xml_escape

from django.db import connection
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)

# Brand palette (mirrors the patient portal's #2b7ab9 primary).
_BRAND = colors.HexColor("#2b7ab9")
_INK = colors.HexColor("#1e293b")
_MUTED = colors.HexColor("#64748b")
_LINE = colors.HexColor("#e2e8f0")

_RECEIPT_SQL = """
    SELECT a.id,
           a.payment_status,
           a.amount,
           a.currency,
           a.payment_id,
           a.appointment_date,
           a.start_time,
           a.visit_type,
           a.patient_name,
           a.patient_email,
           a.updated_at,
           d.full_name AS doctor_name,
           d.specialization AS doctor_specialization,
           d.clinic_name AS clinic_name,
           d.clinic_address AS clinic_address,
           pi.stripe_payment_intent_id,
           pi.succeeded_at
      FROM doc_appointments a
      LEFT JOIN doc_doctors d ON d.id = a.doctor_id
      LEFT JOIN LATERAL (
          SELECT stripe_payment_intent_id, succeeded_at
            FROM doc_payment_intents
           WHERE appointment_id = a.id
             AND status = 'succeeded'
           ORDER BY created_at DESC
           LIMIT 1
      ) pi ON true
     WHERE a.id = %s
       AND a.patient_id = %s
     LIMIT 1
"""


def _single_row(cur) -> dict | None:
    columns = [c[0] for c in cur.description]
    rows = cur.fetchall()
    return dict(zip(columns, rows[0])) if rows else None


def fetch_receipt_data(appointment_id: str, patient_id: str) -> dict | None:
    """Everything the receipt renders, scoped to the owning patient.

    Returns None when the appointment does not exist OR belongs to another
    patient — the caller answers 404 either way, matching the other
    appointment endpoints (never leak existence).
    """
    with connection.cursor() as cur:
        cur.execute(_RECEIPT_SQL, [appointment_id, patient_id])
        row = _single_row(cur)
    if row is None:
        return None

    return {
        "appointment_id": str(row["id"]),
        "payment_status": row["payment_status"],
        # doc_appointments.amount is in MAJOR units (snapshot from fees.py).
        "amount": row["amount"],
        "currency": row["currency"] or "inr",
        "appointment_date": row["appointment_date"],
        "start_time": row["start_time"],
        "visit_type": row["visit_type"] or "online",
        "patient_name": row["patient_name"],
        "patient_email": row["patient_email"],
        "doctor_name": row["doctor_name"],
        "doctor_specialization": row["doctor_specialization"],
        "clinic_name": row["clinic_name"],
        "clinic_address": row["clinic_address"],
        # payment_id is written by the Stripe webhook; legacy confirm-payment
        # rows have none, so fall back to the latest succeeded intent.
        "payment_reference": row["payment_id"] or row["stripe_payment_intent_id"] or "",
        # doc_appointments has no paid_at: the webhook's succeeded_at is the
        # source of truth, updated_at the best fallback (legacy path).
        "paid_at": row["succeeded_at"] or row["updated_at"],
    }


_VISIT_LABELS = {"online": "Online consultation", "physical": "In-person visit"}


def _text(value) -> str:
    text = "" if value is None else str(value).strip()
    return _xml_escape(text) if text else "-"


def _fmt_money(amount, currency) -> str:
    try:
        return f"{str(currency or 'inr').upper()} {float(amount):,.2f}"
    except (TypeError, ValueError):
        return f"{str(currency or 'inr').upper()} -"


def _fmt_date(value) -> str:
    if isinstance(value, (datetime, date)):
        return value.strftime("%d %b %Y")
    return str(value or "-")


def _fmt_paid_at(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y at %H:%M")
    return _fmt_date(value)


def _fmt_time(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    if hasattr(value, "strftime"):  # datetime.time
        return value.strftime("%H:%M")
    return str(value or "-")[:5]


def build_receipt_pdf(data: dict) -> bytes:
    """Render the receipt dict (see fetch_receipt_data) as a PDF."""
    receipt_no = data["appointment_id"][:8].upper()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        title=f"Payment receipt {receipt_no}",
        author=_text(data.get("clinic_name")) if data.get("clinic_name") else "AiDocCall",
    )

    title_style = ParagraphStyle(
        "title", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=_INK
    )
    muted_style = ParagraphStyle(
        "muted", fontName="Helvetica", fontSize=9.5, leading=13, textColor=_MUTED
    )
    meta_style = ParagraphStyle(
        "meta", fontName="Helvetica", fontSize=9.5, leading=13,
        textColor=_MUTED, alignment=2,
    )
    name_style = ParagraphStyle(
        "name", fontName="Helvetica-Bold", fontSize=11, leading=15, textColor=_INK
    )
    body_style = ParagraphStyle(
        "body", fontName="Helvetica", fontSize=10, leading=14, textColor=_INK
    )
    label_style = ParagraphStyle(
        "label", fontName="Helvetica", fontSize=9.5, leading=14, textColor=_MUTED
    )
    total_style = ParagraphStyle(
        "total", fontName="Helvetica", fontSize=10, leading=16,
        textColor=_INK, alignment=2,
    )
    footer_style = ParagraphStyle(
        "footer", fontName="Helvetica-Oblique", fontSize=8.5, leading=12, textColor=_MUTED
    )

    visit_label = _VISIT_LABELS.get(
        str(data.get("visit_type") or "").lower(), data.get("visit_type") or "-"
    )
    appointment_line = (
        f"{_fmt_date(data.get('appointment_date'))} at {_fmt_time(data.get('start_time'))}"
    )
    doctor_line = _text(data.get("doctor_name"))
    specialization_line = _text(data.get("doctor_specialization"))
    clinic_name = _text(data.get("clinic_name"))
    clinic_address = _text(data.get("clinic_address"))

    # Header: title block left, receipt meta right.
    header = Table(
        [
            [
                [Paragraph("Payment Receipt", title_style), Spacer(1, 2),
                 Paragraph(clinic_name if clinic_name != "-" else "AiDocCall", muted_style)],
                [
                    Paragraph(f"Receipt no: {receipt_no}", meta_style),
                    Paragraph(f"Paid on: {_xml_escape(_fmt_paid_at(data.get('paid_at')))}", meta_style),
                ],
            ]
        ],
        colWidths=[doc.width * 0.6, doc.width * 0.4],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    # Parties: who paid, who was paid.
    parties = Table(
        [[
            [
                Paragraph("PAID BY", muted_style), Spacer(1, 3),
                Paragraph(_text(data.get("patient_name")), name_style),
                Paragraph(_text(data.get("patient_email")), muted_style),
            ],
            [
                Paragraph("PAID TO", muted_style), Spacer(1, 3),
                Paragraph(doctor_line, name_style),
                Paragraph(specialization_line, muted_style),
                Paragraph(clinic_name, muted_style),
                Paragraph(clinic_address, muted_style),
            ],
        ]],
        colWidths=[doc.width * 0.5, doc.width * 0.5],
    )
    parties.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    # Line items.
    detail_rows = [
        ["Doctor", doctor_line],
        ["Specialization", specialization_line],
        ["Appointment", _xml_escape(appointment_line)],
        ["Visit type", _xml_escape(str(visit_label))],
        ["Payment reference", _text(data.get("payment_reference"))],
    ]
    details = Table(
        [[Paragraph(label, label_style), Paragraph(value, body_style)]
         for label, value in detail_rows],
        colWidths=[40 * mm, doc.width - 40 * mm],
    )
    details.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, _LINE),
    ]))

    total = Paragraph(
        f"Total paid&nbsp;&nbsp;<font size=16 color=#1e293b><b>"
        f"{_xml_escape(_fmt_money(data.get('amount'), data.get('currency')))}"
        f"</b></font>",
        total_style,
    )

    story = [
        header,
        Spacer(1, 8),
        HRFlowable(width="100%", thickness=1.2, color=_BRAND),
        Spacer(1, 14),
        parties,
        Spacer(1, 16),
        details,
        Spacer(1, 10),
        HRFlowable(width="100%", thickness=0.8, color=_LINE),
        Spacer(1, 8),
        total,
        Spacer(1, 22),
        HRFlowable(width="100%", thickness=0.5, color=_LINE),
        Spacer(1, 6),
        Paragraph(
            "This document is a payment receipt for a booked consultation and is "
            "not a tax invoice. Generated on "
            f"{_xml_escape(datetime.now().strftime('%d %b %Y at %H:%M'))}.",
            footer_style,
        ),
    ]

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    logger.info("Built receipt PDF for appointment %s (%d bytes)", data.get("appointment_id"), len(pdf_bytes))
    return pdf_bytes
