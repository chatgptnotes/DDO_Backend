"""
Read-only Django models mirroring tables owned by Supabase.

`managed = False` everywhere — schema migrations live under `supabase/migrations/`
and are applied through the Supabase SQL editor, not Django.

Generated with `python manage.py inspectdb <table>` against the Supabase
Postgres, then renamed for clarity.
"""
from __future__ import annotations

from django.db import models


class Doctor(models.Model):
    """Mirror of `public.doc_doctors`."""

    id = models.UUIDField(primary_key=True)
    user_id = models.UUIDField()
    email = models.TextField(unique=True)
    full_name = models.TextField()
    specialization = models.TextField(blank=True, null=True)
    qualification = models.TextField(blank=True, null=True)
    experience_years = models.IntegerField(blank=True, null=True)
    clinic_name = models.TextField(blank=True, null=True)
    clinic_address = models.TextField(blank=True, null=True)
    phone = models.TextField(blank=True, null=True)
    profile_image = models.TextField(blank=True, null=True)
    bio = models.TextField(blank=True, null=True)
    consultation_fee = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    online_fee = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    booking_slug = models.TextField(unique=True, blank=True, null=True)
    stripe_account_id = models.TextField(blank=True, null=True)
    is_verified = models.BooleanField(blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)
    standard_meeting_link = models.TextField(blank=True, null=True)
    meeting_link = models.TextField(blank=True, null=True)
    zoom_access_token = models.TextField(blank=True, null=True)
    zoom_refresh_token = models.TextField(blank=True, null=True)
    zoom_token_expires_at = models.DateTimeField(blank=True, null=True)
    zoom_user_id = models.TextField(blank=True, null=True)
    zoom_connected_at = models.DateTimeField(blank=True, null=True)
    role = models.TextField(blank=True, null=True)
    must_change_password = models.BooleanField(blank=True, null=True)
    created_by = models.UUIDField(blank=True, null=True)
    is_active = models.BooleanField(blank=True, null=True)
    designation = models.CharField(max_length=255, blank=True, null=True)
    department = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    pincode = models.CharField(max_length=20, blank=True, null=True)
    international_consultation_fee = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    international_online_fee = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    consultation_fee_inr = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    consultation_fee_usd = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    online_fee_inr = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    online_fee_usd = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    followup_window_days = models.IntegerField(blank=True, null=True)
    followup_discount_pct = models.IntegerField(blank=True, null=True)
    consultation_type = models.TextField()
    clinic_id = models.UUIDField(blank=True, null=True)
    clinic_logo_url = models.TextField(blank=True, null=True)
    brand_primary_color = models.TextField(blank=True, null=True)
    brand_secondary_color = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "doc_doctors"

    def __str__(self) -> str:
        return self.full_name or self.email


class AdamritSyncJob(models.Model):
    """Durable queue entry for a server-only Adamrit import."""

    id = models.UUIDField(primary_key=True)
    requested_by_id = models.UUIDField()
    status = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)
    result_summary = models.JSONField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "adamrit_sync_jobs"


# DPDP Data Deletion Models


class DpdpRetentionRule(models.Model):
    """Retention configuration for each data type per DPDP requirements."""

    id = models.UUIDField(primary_key=True)
    table_name = models.TextField(unique=True)
    retention_years = models.IntegerField(blank=True, null=True)
    delete_cascade = models.BooleanField(default=True)
    description = models.TextField(blank=True, null=True)
    priority = models.IntegerField(default=100)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "doc_dpdp_retention_rules"


class DpdpDeletionRequest(models.Model):
    """Tracks automated and manual patient data deletion requests."""

    id = models.UUIDField(primary_key=True)
    reference_number = models.TextField(unique=True)
    patient_id = models.UUIDField(blank=True, null=True)
    patient_email = models.TextField(blank=True, null=True)
    request_type = models.TextField(default="auto_retention")
    reason = models.TextField(blank=True, null=True)
    status = models.TextField(default="pending")
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    tables_to_delete = models.JSONField(default=list)
    deletion_summary = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.UUIDField(blank=True, null=True)
    grievance_id = models.UUIDField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "doc_dpdp_deletion_requests"


class DpdpDeletionAudit(models.Model):
    """Audit log of all deletion actions (retained indefinitely for compliance)."""

    id = models.UUIDField(primary_key=True)
    deletion_request_id = models.UUIDField(blank=True, null=True)
    table_name = models.TextField()
    record_id = models.UUIDField(blank=True, null=True)
    record_type = models.TextField(blank=True, null=True)
    deleted_fields = models.JSONField(default=list, blank=True)
    record_summary = models.TextField(blank=True, null=True)
    execution_time = models.DateTimeField(auto_now_add=True)
    executed_by = models.TextField(blank=True, null=True)
    status = models.TextField()
    error_details = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "doc_dpdp_deletion_audit"


# DPDP Patient Consent Models


class DpdpProcessingPurpose(models.Model):
    """Central registry of lawful purposes for patient data processing."""

    purpose_code = models.TextField(primary_key=True)
    purpose_name = models.TextField()
    legal_basis = models.TextField()  # contract, consent, legal_obligation, vital_interest, public_interest, legitimate_interest
    is_mandatory = models.BooleanField(default=True)
    abdm_purpose_map = models.TextField(blank=True, null=True)
    processing_categories = models.JSONField(default=list, blank=True)
    data_categories = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "doc_dpdp_processing_purposes"


class PatientConsentPreference(models.Model):
    """Tracks patient consent preferences for optional processing purposes."""

    id = models.UUIDField(primary_key=True)
    patient_id = models.UUIDField()
    purpose_code = models.TextField()
    consent_granted = models.BooleanField(default=False)
    granted_at = models.DateTimeField(blank=True, null=True)
    revoked_at = models.DateTimeField(blank=True, null=True)
    last_updated_at = models.DateTimeField(auto_now=True)
    consent_source = models.TextField(default="manual")  # manual, abdm, implicit, legacy
    consent_metadata = models.JSONField(default=dict, blank=True)
    tenant_id = models.UUIDField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "doc_patient_consent_preferences"
        unique_together = [["patient_id", "purpose_code"]]
