"""
View-level tests for the paid-appointment receipt endpoint.

The DB boundary is mocked (`_resolve_patient_id_for_user`,
`fetch_receipt_data`, `build_receipt_pdf`) so we exercise authn / gating /
response shape on the sqlite test settings — same approach as the
payments app tests. `build_receipt_pdf` also gets one real render test.
"""
from __future__ import annotations

import base64
import re
import zlib
from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import patch

# NOTE: no @pytest.mark.django_db — pytest-django's test-DB creation currently
# fails repo-wide on sqlite (a migration uses Postgres-only DDL), and this
# endpoint is fully testable with the DB boundary mocked. pytest-django would
# flag any accidental DB access, which is the hygiene we want here.

APPOINTMENT_ID = "1d6d4a52-0000-0000-0000-0000000000aa"
RECEIPT_URL = f"/api/aidoccall/appointments/{APPOINTMENT_ID}/receipt/"

PAID_RECEIPT = {
    "appointment_id": APPOINTMENT_ID,
    "payment_status": "paid",
    "amount": Decimal("500.00"),
    "currency": "inr",
    "appointment_date": date(2026, 9, 4),
    "start_time": time(9, 30),
    "visit_type": "online",
    "patient_name": "Test Patient",
    "patient_email": "patient@example.com",
    "doctor_name": "Dr Aarti Rao",
    "doctor_specialization": "Cardiology",
    "clinic_name": "Heart Clinic",
    "clinic_address": "12 MG Road, Pune",
    "payment_reference": "pi_3ABC",
    "paid_at": datetime(2026, 9, 3, 14, 30),
}


def _get_receipt(client, auth_header):
    return client.get(RECEIPT_URL, **auth_header(sub="user-1"))


def test_receipt_requires_auth(client):
    response = client.get(RECEIPT_URL)
    assert response.status_code == 401


def test_receipt_400_without_patient_profile(client, auth_header):
    with patch(
        "aidoccall.appointment_views._resolve_patient_id_for_user",
        return_value=None,
    ):
        response = _get_receipt(client, auth_header)
    assert response.status_code == 400


def test_receipt_404_for_foreign_or_missing_appointment(client, auth_header):
    # fetch_receipt_data scopes by patient_id, so another patient's
    # appointment is indistinguishable from a missing one: both 404.
    with patch(
        "aidoccall.appointment_views._resolve_patient_id_for_user",
        return_value="pid-1",
    ), patch(
        "aidoccall.appointment_views.fetch_receipt_data",
        return_value=None,
    ):
        response = _get_receipt(client, auth_header)
    assert response.status_code == 404
    assert response.json()["detail"] == "Appointment not found."


def test_receipt_409_until_paid(client, auth_header):
    pending = {**PAID_RECEIPT, "payment_status": "pending"}
    with patch(
        "aidoccall.appointment_views._resolve_patient_id_for_user",
        return_value="pid-1",
    ), patch(
        "aidoccall.appointment_views.fetch_receipt_data",
        return_value=pending,
    ):
        response = _get_receipt(client, auth_header)
    assert response.status_code == 409
    assert "paid" in response.json()["detail"]


def test_receipt_streams_pdf_when_paid(client, auth_header):
    with patch(
        "aidoccall.appointment_views._resolve_patient_id_for_user",
        return_value="pid-1",
    ), patch(
        "aidoccall.appointment_views.fetch_receipt_data",
        return_value=PAID_RECEIPT,
    ), patch(
        "aidoccall.appointment_views.build_receipt_pdf",
        return_value=b"%PDF-mock-bytes",
    ) as build_pdf:
        response = _get_receipt(client, auth_header)

    build_pdf.assert_called_once_with(PAID_RECEIPT)
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"] == 'attachment; filename="receipt-1d6d4a52.pdf"'
    assert response.content == b"%PDF-mock-bytes"


def _decompressed_streams(pdf_bytes: bytes) -> str:
    """Concatenate the PDF's content streams as text.

    reportlab wraps streams as ASCII85-then-Flate; handle both layers and
    fall back to raw bytes for anything else (e.g. metadata streams).
    """
    chunks = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", pdf_bytes, re.DOTALL):
        raw = match.group(1).strip()
        if raw.endswith(b"~>"):
            try:
                raw = base64.a85decode(raw, adobe=True)
            except ValueError:
                pass
        try:
            chunks.append(zlib.decompress(raw).decode("latin-1"))
        except zlib.error:
            chunks.append(raw.decode("latin-1", "ignore"))
    return "\n".join(chunks)


def test_build_receipt_pdf_renders_a_real_pdf():
    from aidoccall.services.receipts import build_receipt_pdf

    pdf_bytes = build_receipt_pdf(PAID_RECEIPT)

    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 1000
    # Content sanity: the money line and the receipt number must be in there.
    page_text = _decompressed_streams(pdf_bytes)
    assert "INR 500.00" in page_text
    assert "1D6D4A52" in page_text
