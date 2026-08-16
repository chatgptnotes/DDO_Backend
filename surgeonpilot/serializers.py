"""
Serializers for AiSurgeonPilot endpoints.

The output JSON must match Supabase REST (PostgREST) byte-for-byte during
migration so the frontend doesn't notice the swap. PostgREST emits:

- snake_case field names → ModelSerializer default
- ISO-8601 datetimes WITH explicit offset (e.g. "2026-04-15T10:23:14.123456+00:00")
  rather than DRF's default "Z" suffix → use SupabaseDateTimeField below
- DECIMAL columns as strings → DRF DecimalField default
- UUID columns as strings → DRF UUIDField default
- All selected columns present (null preserved, never omitted) → ModelSerializer default
"""
from __future__ import annotations

from rest_framework import serializers

from .models import (
    DpdpDeletionRequest,
    DpdpProcessingPurpose,
    DpdpRetentionRule,
    Doctor,
    PatientConsentPreference,
)


class SupabaseDateTimeField(serializers.DateTimeField):
    """Match PostgREST output: ISO-8601 with explicit '+00:00' offset, never 'Z'."""

    def to_representation(self, value):
        if value is None:
            return None
        # Use Python's stdlib isoformat which produces "+00:00" rather than "Z".
        return value.isoformat()


class DoctorSerializer(serializers.ModelSerializer):
    """Serialize a `doc_doctors` row in the same shape Supabase REST returns."""

    created_at = SupabaseDateTimeField(allow_null=True, required=False)
    updated_at = SupabaseDateTimeField(allow_null=True, required=False)
    zoom_token_expires_at = SupabaseDateTimeField(allow_null=True, required=False)
    zoom_connected_at = SupabaseDateTimeField(allow_null=True, required=False)

    class Meta:
        model = Doctor
        fields = "__all__"


class DpdpRetentionRuleSerializer(serializers.ModelSerializer):
    """Serialize retention rules configuration."""

    created_at = SupabaseDateTimeField(allow_null=True, required=False)
    updated_at = SupabaseDateTimeField(allow_null=True, required=False)

    class Meta:
        model = DpdpRetentionRule
        fields = "__all__"


class DpdpDeletionRequestSerializer(serializers.ModelSerializer):
    """Serialize deletion request tracking."""

    created_at = SupabaseDateTimeField(allow_null=True, required=False)
    updated_at = SupabaseDateTimeField(allow_null=True, required=False)
    started_at = SupabaseDateTimeField(allow_null=True, required=False)
    completed_at = SupabaseDateTimeField(allow_null=True, required=False)

    class Meta:
        model = DpdpDeletionRequest
        fields = "__all__"


class DpdpProcessingPurposeSerializer(serializers.ModelSerializer):
    """Serialize processing purpose definitions."""

    created_at = SupabaseDateTimeField(allow_null=True, required=False)
    updated_at = SupabaseDateTimeField(allow_null=True, required=False)

    class Meta:
        model = DpdpProcessingPurpose
        fields = "__all__"


class PatientConsentPreferenceSerializer(serializers.ModelSerializer):
    """Serialize patient consent preferences."""

    created_at = SupabaseDateTimeField(allow_null=True, required=False)
    updated_at = SupabaseDateTimeField(allow_null=True, required=False)
    granted_at = SupabaseDateTimeField(allow_null=True, required=False)
    revoked_at = SupabaseDateTimeField(allow_null=True, required=False)
    last_updated_at = SupabaseDateTimeField(allow_null=True, required=False)

    class Meta:
        model = PatientConsentPreference
        fields = "__all__"

