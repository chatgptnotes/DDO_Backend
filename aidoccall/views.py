"""
Views for aidoccall.com endpoints migrated from direct Supabase calls.
"""
from __future__ import annotations

import logging

from django.db import connection
from django.db.models import Q
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
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


class DoctorDirectoryView(APIView):
    """`GET /api/aidoccall/doctors/` - patient-facing doctor directory.

    Replaces patientService.searchDoctors' Supabase-direct read of
    `doc_doctors`. Doctors live in the local PostgreSQL database.

    Query params:
      search        - case-insensitive substring on full_name
      specialization - case-insensitive substring on specialization
      verified=true - only is_verified doctors
      booking_slug  - exact match, for booking-page lookups
      id            - exact match, for id-based lookups

    Public directory data only - credential/token columns are never exposed.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    # Public profile/booking fields. Deliberately excludes zoom_access_token,
    # zoom_refresh_token, stripe_account_id, must_change_password, created_by.
    FIELDS = (
        "id",
        "user_id",
        "full_name",
        "email",
        "phone",
        "specialization",
        "qualification",
        "experience_years",
        "bio",
        "profile_image",
        "clinic_name",
        "clinic_address",
        "city",
        "state",
        "consultation_fee",
        "online_fee",
        "international_consultation_fee",
        "international_online_fee",
        "consultation_fee_inr",
        "consultation_fee_usd",
        "online_fee_inr",
        "online_fee_usd",
        "followup_window_days",
        "followup_discount_pct",
        "consultation_type",
        "booking_slug",
        "is_verified",
        "is_active",
        "created_at",
    )

    def get(self, request):
        queryset = (
            Doctor.objects.filter(role="doctor")
            .exclude(is_active=False)
            .exclude(Q(specialization__isnull=True) | Q(specialization=""))
        )

        search = (request.query_params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(full_name__icontains=search)

        specialization = (request.query_params.get("specialization") or "").strip()
        if specialization:
            queryset = queryset.filter(specialization__icontains=specialization)

        if request.query_params.get("verified") == "true":
            queryset = queryset.filter(is_verified=True)

        booking_slug = (request.query_params.get("booking_slug") or "").strip()
        if booking_slug:
            queryset = queryset.filter(booking_slug=booking_slug)

        doctor_id = (request.query_params.get("id") or "").strip()
        if doctor_id:
            queryset = queryset.filter(id=doctor_id)

        rows = queryset.order_by("full_name").values(*self.FIELDS)
        return Response(list(rows))


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
