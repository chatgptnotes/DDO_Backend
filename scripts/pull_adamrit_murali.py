"""Pull Adamrit records for Murali's visits into the DDO Supabase project."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from typing import Any


PAGE_SIZE = 1000
MURALI = "Murali"
TARGET_DOCTOR = "Dr.B.K. Murali"


class Rest:
    def __init__(self, url: str, key: str, label: str) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.label = label

    def request(self, method: str, table: str, query: dict[str, str] | None = None,
                body: Any = None, headers: dict[str, str] | None = None) -> Any:
        url = f"{self.url}/rest/v1/{table}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        payload = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, method=method, data=payload)
        req.add_header("apikey", self.key)
        req.add_header("Authorization", f"Bearer {self.key}")
        req.add_header("Accept", "application/json")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                raw = response.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"{self.label} {table}: HTTP {exc.code} {detail}") from exc

    def all(self, table: str, select: str = "*", filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            query = {"select": select, "limit": str(PAGE_SIZE), "offset": str(offset)}
            query.update(filters or {})
            page = self.request("GET", table, query)
            if not isinstance(page, list):
                raise RuntimeError(f"{self.label} {table}: expected list")
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows
            offset += PAGE_SIZE

    def upsert(self, table: str, rows: list[dict[str, Any]]) -> int:
        for start in range(0, len(rows), 200):
            self.request(
                "POST", table, {"on_conflict": "id"}, rows[start:start + 200],
                {"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
        return len(rows)


def in_filter(column: str, values: set[str]) -> dict[str, str] | None:
    values = {str(value) for value in values if value}
    return {column: f"in.({','.join(sorted(values))})"} if values else None


def all_by_ids(client: Rest, table: str, column: str, ids: set[str]) -> list[dict[str, Any]]:
    """Avoid REST URL limits when a doctor has many linked patients or visits."""
    result: list[dict[str, Any]] = []
    values = sorted(str(value) for value in ids if value)
    for start in range(0, len(values), 200):
        result.extend(client.all(table, "*", in_filter(column, set(values[start:start + 200]))))
    return result


def stable_id(namespace: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"adamrit:{namespace}:{value}"))


def name_parts(name: str) -> tuple[str, str]:
    parts = [part for part in str(name or "Unknown Patient").split() if part]
    return (parts[0], " ".join(parts[1:]) or "Patient")


def normalize_gender(value: Any) -> str:
    value = str(value or "other").lower()
    return "male" if value in {"m", "male", "man"} else "female" if value in {"f", "female", "woman"} else "other"


def run_sync(*, dry_run: bool = False) -> dict[str, Any]:
    """Import Dr. Murali's Adamrit visits into DDO and return a safe summary."""
    source = Rest(os.environ["ADAMRIT_SUPABASE_URL"], os.environ["ADAMRIT_SUPABASE_SERVICE_ROLE_KEY"], "Adamrit")
    target_url = os.environ.get("DDO_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    target_key = os.environ.get("DDO_SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not target_url or not target_key:
        raise RuntimeError("DDO Supabase URL and service-role key are required")
    target = Rest(target_url, target_key, "DDO")

    target_doctors = target.all("doc_doctors", "id,full_name,email", {"full_name": "ilike.*B.K.*Murali*"})
    if len(target_doctors) != 1:
        raise RuntimeError(f"Expected one target Dr. BK Murali account, found {len(target_doctors)}")
    target_doctor_id = str(target_doctors[0]["id"])

    visits = source.all("visits", "*", {"appointment_with": "ilike.*Murali*"})
    visit_ids = {str(row["id"]) for row in visits if row.get("id")}
    patient_ids = {str(row["patient_id"]) for row in visits if row.get("patient_id")}
    patients = all_by_ids(source, "patients", "id", patient_ids) if patient_ids else []
    medical = all_by_ids(source, "visit_medical_data", "visit_id", visit_ids) if visit_ids else []
    visit_diagnoses = all_by_ids(source, "visit_diagnoses", "visit_id", visit_ids) if visit_ids else []
    diagnosis_ids = {str(row["diagnosis_id"]) for row in visit_diagnoses if row.get("diagnosis_id")}
    diagnoses = all_by_ids(source, "diagnoses", "id", diagnosis_ids) if diagnosis_ids else []
    prescriptions = all_by_ids(source, "prescriptions", "visit_id", visit_ids) if visit_ids else []
    prescription_ids = {str(row["id"]) for row in prescriptions if row.get("id")}
    prescription_items = all_by_ids(source, "prescription_items", "prescription_id", prescription_ids) if prescription_ids else []
    followups = [row for row in visits if row.get("follow_up_date")]
    payments = all_by_ids(source, "patient_payment_transactions", "patient_id", patient_ids) if patient_ids else []

    patient_by_id = {str(row.get("id")): row for row in patients}
    existing_target_patients = target.all("doc_patients", "id,email")
    target_id_by_email = {str(row.get("email")).strip().lower(): str(row["id"]) for row in existing_target_patients if row.get("email")}
    patient_id_map: dict[str, str] = {}
    used_emails = set(target_id_by_email)
    source_emails_seen: set[str] = set()
    patient_rows: list[dict[str, Any]] = []
    for patient in patients:
        source_id = str(patient.get("id"))
        email = str(patient.get("email") or f"adamrit-{source_id}@invalid.local").strip().lower()
        if email in source_emails_seen or (email in used_emails and email not in target_id_by_email):
            email = f"adamrit-duplicate-{source_id}@invalid.local"
        source_emails_seen.add(str(patient.get("email") or f"adamrit-{source_id}@invalid.local").strip().lower())
        used_emails.add(email)
        target_id = target_id_by_email.get(email, source_id)
        patient_id_map[source_id] = target_id
        first, last = name_parts(patient.get("name"))
        patient_rows.append({
            "id": target_id, "email": email, "first_name": first, "last_name": last, "phone_number": patient.get("phone"),
            "date_of_birth": patient.get("date_of_birth") or "1900-01-01", "gender": normalize_gender(patient.get("gender")),
            "blood_group": patient.get("blood_group"), "address": patient.get("address"), "profile_image_url": patient.get("patient_photo"),
            "source": "adamrit", "is_active": True,
        })
    appointment_rows: list[dict[str, Any]] = []
    for visit in visits:
        patient = patient_by_id.get(str(visit.get("patient_id")), {})
        first, last = name_parts(patient.get("name") or visit.get("patient_name"))
        visit_id = str(visit["id"])
        appointment_rows.append({
            "id": visit_id, "doctor_id": target_doctor_id, "patient_id": patient_id_map.get(str(patient.get("id"))),
            "patient_name": patient.get("name") or f"{first} {last}", "patient_email": patient.get("email") or f"adamrit-{visit.get('patient_id')}@invalid.local",
            "patient_phone": patient.get("phone"), "appointment_date": visit.get("visit_date") or visit.get("admission_date") or datetime.utcnow().date().isoformat(),
            "start_time": "00:00:00", "end_time": "00:30:00", "visit_type": "physical", "status": "completed",
            "payment_status": "paid" if visit.get("bill_paid") else "pending", "amount": 0, "notes": visit.get("comments") or visit.get("reason_for_visit"),
        })

    selections = [{"id": stable_id("doctor-selection", str(row["id"])), "patient_id": row["id"], "doctor_id": target_doctor_id, "is_primary_doctor": True, "status": "active", "selection_reason": "Imported from Adamrit Murali visits"} for row in patient_rows]
    medical_rows = []
    visit_by_id = {str(v.get("id")): v for v in visits}
    diagnosis_by_id = {str(d.get("id")): d for d in diagnoses}
    for row in medical:
        source_patient = visit_by_id.get(str(row.get("visit_id")), {}).get("patient_id")
        medical_rows.append({"id": row.get("id"), "patient_id": patient_id_map.get(str(source_patient)), "condition_name": row.get("primary_diagnosis") or row.get("secondary_diagnosis") or "Medical history", "condition_type": "ongoing", "notes": json.dumps(row, default=str), "is_current": True})
    note_rows = []
    for row in medical:
        visit = visit_by_id.get(str(row.get("visit_id")), {})
        note_rows.append({"id": stable_id("consultation", str(row.get("visit_id"))), "doctor_id": target_doctor_id, "patient_id": patient_id_map.get(str(visit.get("patient_id"))), "appointment_id": row.get("visit_id"), "consultation_date": visit.get("visit_date") or datetime.utcnow().date().isoformat(), "diagnosis": row.get("primary_diagnosis"), "examination_findings": row.get("examination_findings"), "history_of_present_illness": row.get("medical_history"), "treatment_plan": row.get("treatment_plan"), "additional_notes": row.get("notes"), "vitals": row.get("vital_signs") or {}})
    for row in visit_diagnoses:
        visit = visit_by_id.get(str(row.get("visit_id")), {})
        diagnosis = diagnosis_by_id.get(str(row.get("diagnosis_id")), {})
        note_rows.append({"id": stable_id("visit-diagnosis", str(row.get("id"))), "doctor_id": target_doctor_id, "patient_id": patient_id_map.get(str(visit.get("patient_id"))), "appointment_id": row.get("visit_id"), "consultation_date": visit.get("visit_date") or datetime.utcnow().date().isoformat(), "diagnosis": diagnosis.get("name") or diagnosis.get("description"), "additional_notes": row.get("notes"), "vitals": {"is_primary": row.get("is_primary")}})
    prescription_rows = [{"id": row.get("id"), "doctor_id": target_doctor_id, "patient_id": patient_id_map.get(str(row.get("patient_id"))), "appointment_id": row.get("visit_id"), "prescription_date": row.get("prescription_date") or datetime.utcnow().date().isoformat(), "diagnosis": row.get("diagnosis"), "notes": row.get("notes")} for row in prescriptions]
    item_rows = [{"id": row.get("id"), "prescription_id": row.get("prescription_id"), "medication_name": row.get("medicine_name") or row.get("generic_name") or row.get("brand_name") or "Unknown", "dosage": row.get("dosage_timing") or "Not specified", "frequency": row.get("dosage_frequency") or "Not specified", "duration": str(row.get("duration_days") or ""), "instructions": row.get("special_instructions"), "quantity": str(row.get("quantity_prescribed") or "") } for row in prescription_items]
    followup_rows = [{"id": stable_id("followup", str(row["id"])), "doctor_id": target_doctor_id, "patient_id": patient_id_map.get(str(row.get("patient_id"))), "appointment_id": row.get("id"), "followup_date": row.get("follow_up_date"), "notes": row.get("follow_up_notes"), "status": "pending"} for row in followups]
    counts = {"patients": len(patient_rows), "appointments": len(appointment_rows), "medical_history": len(medical_rows), "consultation_notes": len(note_rows), "prescriptions": len(prescription_rows), "prescription_items": len(item_rows), "followups": len(followup_rows), "payment_rows_detected_not_imported": len(payments), "diagnosis_links": len(visit_diagnoses), "diagnosis_catalog_rows": len(diagnoses)}
    result = {
        "source_counts": {
            "visits": len(visits),
            "patients": len(patients),
            "visit_medical_data": len(medical),
            "visit_diagnoses": len(visit_diagnoses),
            "prescriptions": len(prescriptions),
            "prescription_items": len(prescription_items),
        },
        "target_counts": {},
        "notes": [
            "Source scope: Adamrit visits whose appointment_with contains Murali.",
            f"Payment rows detected but not imported: {len(payments)}.",
        ],
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    for table, rows in [("doc_patients", patient_rows), ("doc_patient_doctor_selections", selections), ("doc_patient_medical_history", medical_rows), ("doc_appointments", appointment_rows), ("doc_consultation_notes", note_rows), ("doc_prescriptions", prescription_rows), ("doc_prescription_items", item_rows), ("doc_followups", followup_rows)]:
        result["target_counts"][table] = target.upsert(table, rows)
    return result


def sync_adamrit_murali() -> dict[str, Any]:
    """Worker entry point for the real Adamrit schema (visits/patients/doctors)."""
    return run_sync()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run_sync(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
