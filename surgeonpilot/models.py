"""
Read-only Django models mirroring tables owned by Supabase.

`managed = False` everywhere — schema migrations live under `supabase/migrations/`
and are applied through the Supabase SQL editor, not Django.

Generated with `python manage.py inspectdb <table>` against the Supabase
Postgres, then renamed for clarity.
"""
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
