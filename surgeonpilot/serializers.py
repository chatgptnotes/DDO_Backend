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

from .models import Doctor


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
