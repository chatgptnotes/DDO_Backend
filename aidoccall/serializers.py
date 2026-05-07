"""
Serializers for aidoccall.com clinical-flow endpoints.

Output JSON matches Supabase REST byte-for-byte during migration so the
frontend doesn't notice the swap.
"""
from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from surgeonpilot.serializers import SupabaseDateTimeField
from surgeonpilot.models import Doctor

from .models import PatientDoctorSelection


class _NestedDoctorSerializer(serializers.ModelSerializer):
    """Subset of `doc_doctors` fields embedded in selected-doctors response.

    Mirrors the Supabase select clause:

        doctor:doctor_id (
            id, full_name, specialization, clinic_name, clinic_address,
            experience_years, consultation_fee, online_fee, is_verified
        )
    """

    class Meta:
        model = Doctor
        fields = [
            "id",
            "full_name",
            "specialization",
            "clinic_name",
            "clinic_address",
            "experience_years",
            "consultation_fee",
            "online_fee",
            "is_verified",
        ]


class PatientDoctorSelectionSerializer(serializers.ModelSerializer):
    """Mirror of Supabase `select('*, doctor:doctor_id (...)')` response."""

    selected_at = SupabaseDateTimeField(allow_null=True, required=False)
    doctor = serializers.SerializerMethodField()

    class Meta:
        model = PatientDoctorSelection
        fields = [
            "id",
            "patient_id",
            "doctor_id",
            "is_primary_doctor",
            "selection_reason",
            "status",
            "selected_at",
            "is_favorite",
            "doctor",
        ]

    def get_doctor(self, obj: PatientDoctorSelection) -> dict | None:
        # Pre-fetched doctor is attached by the view via select_related-ish lookup;
        # fall back to a fetch if absent (defensive).
        doctor_obj = getattr(obj, "_doctor_cache", None)
        if doctor_obj is None:
            doctor_obj = Doctor.objects.filter(id=obj.doctor_id).first()
        if doctor_obj is None:
            return None
        return _NestedDoctorSerializer(doctor_obj).data


class CreateDoctorSerializer(serializers.Serializer):
    """Validates the payload for `POST /api/aidoccall/admin/doctors/`."""

    email = serializers.EmailField()
    full_name = serializers.CharField(max_length=255)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    specialization = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    qualification = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    experience_years = serializers.IntegerField(
        min_value=0, max_value=80, required=False
    )
    clinic_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    clinic_address = serializers.CharField(required=False, allow_blank=True)
    consultation_fee = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, min_value=Decimal("0")
    )
    online_fee = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, min_value=Decimal("0")
    )
    bio = serializers.CharField(required=False, allow_blank=True)
    scope_id = serializers.UUIDField(required=False, allow_null=True)
