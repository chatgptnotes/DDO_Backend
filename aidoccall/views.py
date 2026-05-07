"""
Views for aidoccall.com endpoints migrated from direct Supabase calls.
"""
from __future__ import annotations

import logging

from django.db import connection
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRole
from core.supabase_admin import SupabaseAdminError
from surgeonpilot.models import Doctor

from .models import PatientDoctorSelection
from .serializers import CreateDoctorSerializer, PatientDoctorSelectionSerializer
from .services.doctors import create_doctor_profile

logger = logging.getLogger(__name__)


def _resolve_patient_id_for_user(user_id: str) -> str | None:
    """Map a Supabase auth user_id to their `doc_patients.id`.

    Returns None if the user has no patient row — caller should treat as empty list.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id FROM public.doc_patients WHERE user_id = %s LIMIT 1",
            [str(user_id)],
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


class PatientSelectedDoctorsView(ListAPIView):
    """Replaces aidoccall.com/src/services/patientService.js#getSelectedDoctors.

    Original Supabase call:

        supabase.from('doc_patient_doctor_selections')
                .select('*, doctor:doctor_id(id, full_name, ...)')
                .eq('patient_id', patientId)
                .order('is_favorite', { ascending: false })

    Security: `patient_id` is NOT taken from the URL. We derive the requester's
    `doc_patients.id` from their JWT, so a user can only see their own
    selections. Avoids the failure mode where a manipulated URL exposes
    another patient's data.
    """

    serializer_class = PatientDoctorSelectionSerializer
    permission_classes = [
        IsAuthenticated,
        HasRole("patient", "superadmin", "clinical_admin"),
    ]
    pagination_class = None

    def get_queryset(self):
        patient_id = _resolve_patient_id_for_user(self.request.user.id)
        if not patient_id:
            return PatientDoctorSelection.objects.none()

        # PostgREST orders DESC for "is_favorite descending" (true > false), so
        # match exactly with `-is_favorite`.
        qs = PatientDoctorSelection.objects.filter(patient_id=patient_id).order_by(
            "-is_favorite"
        )
        # Materialize so we can attach pre-fetched doctor data without re-querying.
        rows = list(qs)
        if rows:
            doctor_ids = {r.doctor_id for r in rows}
            doctors_by_id = {d.id: d for d in Doctor.objects.filter(id__in=doctor_ids)}
            for r in rows:
                r._doctor_cache = doctors_by_id.get(r.doctor_id)
        return rows

    def list(self, request, *args, **kwargs):
        rows = self.get_queryset()
        serializer = self.get_serializer(rows, many=True)
        return Response(serializer.data)


class CreateDoctorView(APIView):
    """`POST /api/aidoccall/admin/doctors/` — clinical_admin onboards a doctor.

    Replaces the legacy frontend path that called Supabase Auth directly and
    returned "email already exists" when the email belonged to any existing
    user (including the calling admin themselves).

    The handler is idempotent: existing users get the doctor role attached;
    new emails get an invite link via Supabase Auth Admin. Either way, a
    `doc_doctors` row is created or updated for the same `user_id`.
    """

    permission_classes = [
        IsAuthenticated,
        HasRole("clinical_admin", "superadmin"),
    ]

    def post(self, request):
        serializer = CreateDoctorSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = dict(serializer.validated_data)
        scope_id = payload.pop("scope_id", None)

        try:
            result = create_doctor_profile(
                caller_user_id=str(request.user.id),
                payload=payload,
                scope_id=str(scope_id) if scope_id else None,
            )
        except SupabaseAdminError as exc:
            logger.error("Supabase admin call failed: %s", exc)
            return Response(
                {"detail": "Could not provision auth user", "error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(result.as_dict(), status=status.HTTP_201_CREATED)
