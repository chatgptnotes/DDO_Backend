"""Drive PatientReportUploadView directly with a stub patient session."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIRequestFactory, force_authenticate
from core.patient_views import PatientReportUploadView
import psycopg

PATIENT_ID = "565fae4d-c274-4924-994d-05e94d28c44e"  # Munna (local)

conn = psycopg.connect("postgresql://postgres:ddo%40123@localhost:5432/postgres")
cur = conn.cursor()
cur.execute("SELECT user_id FROM doc_patients WHERE id = %s::uuid", [PATIENT_ID])
user_id = cur.fetchone()[0]
conn.close()
print("patient user_id:", user_id)

pdf = SimpleUploadedFile("test report.pdf", b"%PDF-1.4 test receipt content", content_type="application/pdf")
factory = APIRequestFactory()
request = factory.post(
    "/api/patients/me/reports/upload/",
    data={"file": pdf, "fileType": "condition_report", "description": "Condition report: test"},
    format="multipart",
)
stub_user = type("U", (), {"id": user_id, "is_active": True, "is_anonymous": False, "is_authenticated": True})()
force_authenticate(request, user=stub_user)

response = PatientReportUploadView.as_view()(request)
print("status:", response.status_code)
data = response.data
print("file_url:", data["file_url"])
print("uploaded_by:", data["uploaded_by"], "| file_type:", data["file_type"])

stored = Path(data["file_url"])
abs_path = Path(__import__("django.conf", fromlist=["settings"]).settings.BASE_DIR) / "media" / stored
print("file exists on disk:", abs_path.exists(), "| size:", abs_path.stat().st_size if abs_path.exists() else 0)

# doctor visibility query sees it
conn = psycopg.connect("postgresql://postgres:ddo%40123@localhost:5432/postgres")
cur = conn.cursor()
cur.execute(
    """SELECT count(*) FROM doc_patient_reports
        WHERE (doctor_id = %s::uuid OR doctor_id IS NULL)
          AND (doc_patient_id = %s::uuid OR patient_id = %s::uuid)""",
    ["e3cd76d2-f327-4e75-974f-dfd05f02b225", PATIENT_ID, PATIENT_ID],
)
print("doctor documents query rows (uncommitted):", cur.fetchone()[0])
cur.execute("DELETE FROM doc_patient_reports WHERE id = %s::uuid", [str(data["id"])])
print("test row deleted:", cur.rowcount)
conn.close()

if abs_path.exists():
    abs_path.unlink()
    print("test file removed:", not abs_path.exists())
