#!/usr/bin/env python3
"""
One-way sync from the Adamrit Supabase project into the DDO project for
Dr. BK Murali.

The script is intentionally server-side only:
  - source project credentials are used to read Adamrit
  - target project credentials are used to upsert into DDO
  - all writes use service-role keys

Usage examples:

    python scripts/sync_adamrit_murali.py --dry-run
    python scripts/sync_adamrit_murali.py --report-md Adamrit_to_DDO_Sync_Report.md
    python scripts/sync_adamrit_murali.py --source-doctor "Dr. BK Murali" --target-doctor "Dr. BK Murali"

Expected environment:

    ADAMRIT_SUPABASE_URL
    ADAMRIT_SUPABASE_SERVICE_ROLE_KEY
    DDO_SUPABASE_URL
    DDO_SUPABASE_SERVICE_ROLE_KEY

Fallbacks:
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY

The target fallbacks let this script run inside the DDO backend environment
without duplicating secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PAGE_SIZE = 1000
DEFAULT_SOURCE_IDENTIFIER = "Dr. BK Murali"
DEFAULT_TARGET_IDENTIFIER = "Dr. BK Murali"


@dataclass(frozen=True)
class ProjectConfig:
    label: str
    url: str
    service_role_key: str
    anon_key: str | None = None


@dataclass
class SyncCounts:
    source_rows: dict[str, int] = field(default_factory=dict)
    target_rows: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    patient_user_ids_resolved: int = 0
    patient_user_ids_null: int = 0
    clinic_ids_resolved: int = 0
    clinic_ids_null: int = 0


class SupabaseRestClient:
    def __init__(self, config: ProjectConfig) -> None:
        self.config = config
        self.base_url = config.url.rstrip("/")
        self.service_role_key = config.service_role_key.strip()
        if not self.base_url or not self.service_role_key:
            raise ValueError(f"{config.label}: SUPABASE_URL and service-role key are required")

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: Any | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"

        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url=url, method=method, data=payload)
        req.add_header("apikey", self.service_role_key)
        req.add_header("Authorization", f"Bearer {self.service_role_key}")
        req.add_header("Accept", "application/json")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        if extra_headers:
            for key, value in extra_headers.items():
                req.add_header(key, value)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise RuntimeError(f"{self.config.label} {method} {path} failed: {exc.code} {body_text}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{self.config.label} {method} {path} failed: {exc.reason}") from exc

    def fetch_all(
        self,
        table: str,
        *,
        select: str = "*",
        filters: dict[str, str] | None = None,
        order: str | None = None,
        page_size: int = PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        offset = 0
        rows: list[dict[str, Any]] = []

        while True:
            query: dict[str, str] = {
                "select": select,
                "limit": str(page_size),
                "offset": str(offset),
            }
            if order:
                query["order"] = order
            if filters:
                query.update(filters)

            page = self._request("GET", f"/rest/v1/{table}", query=query)
            if not isinstance(page, list):
                raise RuntimeError(f"{self.config.label} returned unexpected payload for {table}: {page!r}")

            rows.extend(page)
            if len(page) < page_size:
                return rows
            offset += page_size

    def fetch_one(
        self,
        table: str,
        *,
        select: str = "*",
        filters: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        query: dict[str, str] = {"select": select, "limit": "1"}
        if filters:
            query.update(filters)
        page = self._request("GET", f"/rest/v1/{table}", query=query)
        if not page:
            return None
        if not isinstance(page, list):
            raise RuntimeError(f"{self.config.label} returned unexpected payload for {table}: {page!r}")
        return page[0]

    def fetch_doctors(self) -> list[dict[str, Any]]:
        return self.fetch_all(
            "doc_doctors",
            select="id,email,full_name,clinic_name,role,is_active",
            order="full_name.asc",
        )

    def fetch_clinics(self) -> list[dict[str, Any]]:
        return self.fetch_all("clinics", select="id,name,is_active", order="name.asc")

    def find_auth_user_by_email(self, email: str) -> str | None:
        normalized = email.strip().lower()
        if not normalized:
            return None

        page = 1
        per_page = 200
        while True:
            query = {
                "email": normalized,
                "page": str(page),
                "per_page": str(per_page),
            }
            data = self._request("GET", "/auth/v1/admin/users", query=query)
            if isinstance(data, dict):
                users = data.get("users") or data.get("data") or []
            elif isinstance(data, list):
                users = data
            else:
                users = []

            if not users:
                return None

            for user in users:
                user_email = str(user.get("email") or "").strip().lower()
                if user_email == normalized:
                    return str(user["id"])

            if len(users) < per_page:
                return None
            page += 1
            if page > 50:
                return None

    def upsert_rows(
        self,
        table: str,
        rows: list[dict[str, Any]],
        *,
        chunk_size: int = 200,
    ) -> int:
        if not rows:
            return 0

        total = 0
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start : start + chunk_size]
            self._request(
                "POST",
                f"/rest/v1/{table}",
                query={"on_conflict": "id"},
                body=chunk,
                extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            total += len(chunk)
        return total


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _normalize(text: Any) -> str:
    return str(text or "").strip().lower()


def _matches_identifier(row: dict[str, Any], identifier: str) -> bool:
    probe = _normalize(identifier)
    if not probe:
        return False
    return probe in {
        _normalize(row.get("id")),
        _normalize(row.get("email")),
        _normalize(row.get("full_name")),
    }


def _resolve_doctor(project: SupabaseRestClient, identifier: str, *, label: str) -> dict[str, Any]:
    doctors = project.fetch_doctors()
    matches = [row for row in doctors if _matches_identifier(row, identifier)]
    if not matches:
        available = ", ".join(str(row.get("full_name") or row.get("email") or row.get("id")) for row in doctors[:10])
        raise SystemExit(
            f"Could not resolve {label} doctor '{identifier}'. "
            f"Available examples: {available or 'none'}"
        )
    if len(matches) > 1:
        names = ", ".join(str(row.get("full_name") or row.get("email") or row.get("id")) for row in matches)
        raise SystemExit(f"Ambiguous {label} doctor '{identifier}': {names}")
    return matches[0]


def _format_in_clause(values: set[str]) -> str | None:
    items = [str(value) for value in sorted(values) if str(value)]
    if not items:
        return None
    return f"in.({','.join(items)})"


def _format_or_clause(*clauses: str | None) -> str | None:
    cleaned = [clause for clause in clauses if clause]
    if not cleaned:
        return None
    return f"({','.join(cleaned)})"


def _doctor_filter(doctor_id: str) -> dict[str, str]:
    return {"doctor_id": f"eq.{doctor_id}"}


def _patient_filter(patient_ids: set[str]) -> dict[str, str] | None:
    clause = _format_in_clause(patient_ids)
    if not clause:
        return None
    return {"patient_id": clause}


def _id_filter(column: str, ids: set[str]) -> dict[str, str] | None:
    clause = _format_in_clause(ids)
    if not clause:
        return None
    return {column: clause}


def _rewrite_rows(
    rows: list[dict[str, Any]],
    *,
    doctor_id: str | None = None,
    patient_user_lookup: dict[str, str | None] | None = None,
    clinic_id: str | None = None,
    force_refunded_by: str | None = None,
    counts: SyncCounts | None = None,
) -> list[dict[str, Any]]:
    rewritten: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if doctor_id is not None and "doctor_id" in item:
            item["doctor_id"] = doctor_id
        if "clinic_id" in item:
            if item.get("clinic_id") and clinic_id is not None:
                item["clinic_id"] = clinic_id
                if counts:
                    counts.clinic_ids_resolved += 1
            elif item.get("clinic_id"):
                item["clinic_id"] = None
                if counts:
                    counts.clinic_ids_null += 1
            elif counts:
                counts.clinic_ids_null += 1

        if force_refunded_by is not None and "refunded_by" in item:
            item["refunded_by"] = force_refunded_by

        if patient_user_lookup is not None and "user_id" in item:
            email = _normalize(item.get("email"))
            resolved = patient_user_lookup.get(email)
            if resolved:
                item["user_id"] = resolved
                if counts:
                    counts.patient_user_ids_resolved += 1
            else:
                item["user_id"] = None
                if counts:
                    counts.patient_user_ids_null += 1

        rewritten.append(item)
    return rewritten


def _count_and_collect_patient_ids(rows: list[dict[str, Any]], patient_ids: set[str]) -> None:
    for row in rows:
        patient_id = row.get("patient_id")
        if patient_id:
            patient_ids.add(str(patient_id))


def _count_and_collect_ids(rows: list[dict[str, Any]], key: str, ids: set[str]) -> None:
    for row in rows:
        value = row.get(key)
        if value:
            ids.add(str(value))


def _resolve_target_clinic_id(target: SupabaseRestClient, source_doctor: dict[str, Any]) -> str | None:
    clinic_name = str(source_doctor.get("clinic_name") or "").strip()
    if not clinic_name:
        return None
    clinics = target.fetch_clinics()
    matches = [row for row in clinics if _normalize(row.get("name")) == _normalize(clinic_name)]
    if not matches:
        return None
    return str(matches[0]["id"])


def _source_scope(
    source: SupabaseRestClient,
    source_doctor_id: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    set[str],
    set[str],
    set[str],
]:
    selections = source.fetch_all(
        "doc_patient_doctor_selections",
        filters=_doctor_filter(source_doctor_id),
        order="selected_at.asc",
    )
    appointments = source.fetch_all(
        "doc_appointments",
        filters=_doctor_filter(source_doctor_id),
        order="appointment_date.asc,start_time.asc",
    )
    consultation_notes = source.fetch_all(
        "doc_consultation_notes",
        filters=_doctor_filter(source_doctor_id),
        order="consultation_date.asc,created_at.asc",
    )
    prescriptions = source.fetch_all(
        "doc_prescriptions",
        filters=_doctor_filter(source_doctor_id),
        order="prescription_date.asc,created_at.asc",
    )
    followups = source.fetch_all(
        "doc_followups",
        filters=_doctor_filter(source_doctor_id),
        order="followup_date.asc,created_at.asc",
    )
    refunds = source.fetch_all(
        "doc_refunds",
        filters=_doctor_filter(source_doctor_id),
        order="created_at.asc",
    )

    patient_ids: set[str] = set()
    appointment_ids: set[str] = set()
    prescription_ids: set[str] = set()
    note_ids: set[str] = set()

    _count_and_collect_patient_ids(selections, patient_ids)
    _count_and_collect_patient_ids(appointments, patient_ids)
    _count_and_collect_patient_ids(consultation_notes, patient_ids)
    _count_and_collect_patient_ids(prescriptions, patient_ids)
    _count_and_collect_patient_ids(followups, patient_ids)
    _count_and_collect_patient_ids(refunds, patient_ids)

    _count_and_collect_ids(appointments, "id", appointment_ids)
    _count_and_collect_ids(consultation_notes, "id", note_ids)
    _count_and_collect_ids(prescriptions, "id", prescription_ids)
    _count_and_collect_ids(followups, "appointment_id", appointment_ids)
    _count_and_collect_ids(refunds, "appointment_id", appointment_ids)

    payment_intent_filters = None
    if appointment_ids or patient_ids:
        payment_intent_filters = _format_or_clause(
            f"appointment_id.{_format_in_clause(appointment_ids)}" if appointment_ids else None,
            f"patient_id.{_format_in_clause(patient_ids)}" if patient_ids else None,
        )
    payment_intents = (
        source.fetch_all(
            "doc_payment_intents",
            filters={"or": payment_intent_filters},
            order="created_at.asc",
        )
        if payment_intent_filters
        else []
    )

    _count_and_collect_patient_ids(payment_intents, patient_ids)
    _count_and_collect_ids(payment_intents, "appointment_id", appointment_ids)

    stripe_customers = (
        source.fetch_all(
            "doc_stripe_customers",
            filters=_patient_filter(patient_ids),
            order="created_at.asc",
        )
        if patient_ids
        else []
    )
    _count_and_collect_patient_ids(stripe_customers, patient_ids)

    patients = (
        source.fetch_all(
            "doc_patients",
            filters=_id_filter("id", patient_ids),
            order="created_at.asc",
        )
        if patient_ids
        else []
    )
    medical_history = (
        source.fetch_all(
            "doc_patient_medical_history",
            filters=_patient_filter(patient_ids),
            order="created_at.asc",
        )
        if patient_ids
        else []
    )
    prescription_items = (
        source.fetch_all(
            "doc_prescription_items",
            filters=_id_filter("prescription_id", prescription_ids),
            order="sort_order.asc,created_at.asc",
        )
        if prescription_ids
        else []
    )

    return (
        patients,
        selections,
        medical_history,
        appointments,
        consultation_notes,
        prescriptions,
        prescription_items,
        followups,
        refunds,
        payment_intents,
        stripe_customers,
        patient_ids,
        appointment_ids,
        note_ids,
    )


def _build_report(
    *,
    source_config: ProjectConfig,
    target_config: ProjectConfig,
    source_doctor: dict[str, Any],
    target_doctor: dict[str, Any],
    counts: SyncCounts,
    dry_run: bool,
) -> str:
    lines = [
        "# Adamrit to DDO Sync Report for Dr. BK Murali",
        "",
        f"- Generated: {datetime.utcnow().isoformat()}Z",
        f"- Mode: {'dry-run' if dry_run else 'sync'}",
        f"- Source project: {source_config.label}",
        f"- Target project: {target_config.label}",
        f"- Source doctor: {source_doctor.get('full_name')} ({source_doctor.get('email')})",
        f"- Target doctor: {target_doctor.get('full_name')} ({target_doctor.get('email')})",
        "",
        "## Counts",
    ]

    for table, count in counts.source_rows.items():
        lines.append(f"- Source {table}: {count}")
    for table, count in counts.target_rows.items():
        lines.append(f"- Target {table}: {count}")

    lines.extend(
        [
            "",
            "## Mapping Notes",
            f"- Patient user IDs resolved: {counts.patient_user_ids_resolved}",
            f"- Patient user IDs set to null: {counts.patient_user_ids_null}",
            f"- Clinic IDs resolved: {counts.clinic_ids_resolved}",
            f"- Clinic IDs set to null: {counts.clinic_ids_null}",
        ]
    )
    if counts.notes:
        lines.extend(["", "## Notes"])
        lines.extend(f"- {note}" for note in counts.notes)

    return "\n".join(lines).rstrip() + "\n"


def _run_sync(args: argparse.Namespace) -> int:
    source_config = ProjectConfig(
        label="Adamrit",
        url=_env("ADAMRIT_SUPABASE_URL") or _env("SOURCE_SUPABASE_URL") or "",
        service_role_key=_env("ADAMRIT_SUPABASE_SERVICE_ROLE_KEY")
        or _env("SOURCE_SUPABASE_SERVICE_ROLE_KEY")
        or "",
        anon_key=_env("ADAMRIT_SUPABASE_ANON_KEY") or _env("SOURCE_SUPABASE_ANON_KEY"),
    )
    target_config = ProjectConfig(
        label="DDO",
        url=_env("DDO_SUPABASE_URL") or _env("SUPABASE_URL") or "",
        service_role_key=_env("DDO_SUPABASE_SERVICE_ROLE_KEY")
        or _env("SUPABASE_SERVICE_ROLE_KEY")
        or "",
        anon_key=_env("DDO_SUPABASE_ANON_KEY") or _env("SUPABASE_ANON_KEY"),
    )

    source = SupabaseRestClient(source_config)
    target = SupabaseRestClient(target_config)

    source_doctor = _resolve_doctor(source, args.source_doctor, label="source")
    target_doctor = _resolve_doctor(target, args.target_doctor, label="target")
    target_clinic_id = _resolve_target_clinic_id(target, target_doctor) or _resolve_target_clinic_id(
        target,
        source_doctor,
    )

    (
        patients,
        selections,
        medical_history,
        appointments,
        consultation_notes,
        prescriptions,
        prescription_items,
        followups,
        refunds,
        payment_intents,
        stripe_customers,
        patient_ids,
        appointment_ids,
        note_ids,
    ) = _source_scope(source, str(source_doctor["id"]))

    counts = SyncCounts()
    counts.source_rows = {
        "doc_patients": len(patients),
        "doc_patient_doctor_selections": len(selections),
        "doc_patient_medical_history": len(medical_history),
        "doc_appointments": len(appointments),
        "doc_consultation_notes": len(consultation_notes),
        "doc_prescriptions": len(prescriptions),
        "doc_prescription_items": len(prescription_items),
        "doc_followups": len(followups),
        "doc_refunds": len(refunds),
        "doc_payment_intents": len(payment_intents),
        "doc_stripe_customers": len(stripe_customers),
    }

    patient_user_lookup: dict[str, str | None] = {}
    for patient in patients:
        email = _normalize(patient.get("email"))
        if email:
            patient_user_lookup[email] = target.find_auth_user_by_email(str(patient.get("email") or ""))

    if args.dry_run:
        counts.notes.append("Dry-run requested; no target rows were written.")

    mapped_patients = _rewrite_rows(
        patients,
        patient_user_lookup=patient_user_lookup,
        counts=counts,
    )
    mapped_selections = _rewrite_rows(selections, doctor_id=str(target_doctor["id"]))
    mapped_medical_history = _rewrite_rows(medical_history)
    mapped_appointments = _rewrite_rows(appointments, doctor_id=str(target_doctor["id"]))
    mapped_notes = _rewrite_rows(consultation_notes, doctor_id=str(target_doctor["id"]))
    mapped_prescriptions = _rewrite_rows(prescriptions, doctor_id=str(target_doctor["id"]))
    mapped_prescription_items = _rewrite_rows(prescription_items)
    mapped_followups = _rewrite_rows(followups, doctor_id=str(target_doctor["id"]))
    mapped_refunds = _rewrite_rows(
        refunds,
        doctor_id=str(target_doctor["id"]),
        force_refunded_by=str(target_doctor["id"]),
    )
    mapped_payment_intents = _rewrite_rows(
        payment_intents,
        clinic_id=target_clinic_id,
        counts=counts,
    )
    mapped_stripe_customers = _rewrite_rows(stripe_customers)

    if not args.dry_run:
        counts.target_rows["doc_patients"] = target.upsert_rows("doc_patients", mapped_patients)
        counts.target_rows["doc_patient_doctor_selections"] = target.upsert_rows(
            "doc_patient_doctor_selections",
            mapped_selections,
        )
        counts.target_rows["doc_patient_medical_history"] = target.upsert_rows(
            "doc_patient_medical_history",
            mapped_medical_history,
        )
        counts.target_rows["doc_appointments"] = target.upsert_rows("doc_appointments", mapped_appointments)
        counts.target_rows["doc_consultation_notes"] = target.upsert_rows(
            "doc_consultation_notes",
            mapped_notes,
        )
        counts.target_rows["doc_prescriptions"] = target.upsert_rows("doc_prescriptions", mapped_prescriptions)
        counts.target_rows["doc_prescription_items"] = target.upsert_rows(
            "doc_prescription_items",
            mapped_prescription_items,
        )
        counts.target_rows["doc_followups"] = target.upsert_rows("doc_followups", mapped_followups)
        counts.target_rows["doc_refunds"] = target.upsert_rows("doc_refunds", mapped_refunds)
        counts.target_rows["doc_payment_intents"] = target.upsert_rows(
            "doc_payment_intents",
            mapped_payment_intents,
        )
        counts.target_rows["doc_stripe_customers"] = target.upsert_rows(
            "doc_stripe_customers",
            mapped_stripe_customers,
        )
    else:
        counts.target_rows = dict(counts.source_rows)

    report = _build_report(
        source_config=source_config,
        target_config=target_config,
        source_doctor=source_doctor,
        target_doctor=target_doctor,
        counts=counts,
        dry_run=args.dry_run,
    )

    if args.report_md:
        Path(args.report_md).write_text(report, encoding="utf-8")
        print(f"Wrote report: {args.report_md}")

    print(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync Adamrit patient data into DDO for Dr. BK Murali."
    )
    parser.add_argument(
        "--source-doctor",
        default=DEFAULT_SOURCE_IDENTIFIER,
        help="Source doctor identifier (id, email, or full name).",
    )
    parser.add_argument(
        "--target-doctor",
        default=DEFAULT_TARGET_IDENTIFIER,
        help="Target doctor identifier (id, email, or full name).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count and map records without writing to the target project.",
    )
    parser.add_argument(
        "--report-md",
        default="",
        help="Write a markdown summary report to this file path.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return _run_sync(args)
    except Exception as exc:  # noqa: BLE001 - this is a batch sync tool
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
