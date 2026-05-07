"""
Read-only Django models for aidoccall.com clinical-flow tables.

`managed = False` everywhere — Supabase owns the schema.
Generated via `python manage.py inspectdb <table>` and cleaned up.
"""
from django.db import models


class PatientDoctorSelection(models.Model):
    """Mirror of `public.doc_patient_doctor_selections` — patient's saved doctors."""

    id = models.UUIDField(primary_key=True)
    # Use raw UUIDFields rather than ForeignKey so the serializer naturally
    # outputs `patient_id` and `doctor_id` exactly as PostgREST does.
    patient_id = models.UUIDField()
    doctor_id = models.UUIDField()
    is_primary_doctor = models.BooleanField(blank=True, null=True)
    selection_reason = models.TextField(blank=True, null=True)
    status = models.TextField(blank=True, null=True)
    selected_at = models.DateTimeField(blank=True, null=True)
    is_favorite = models.BooleanField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "doc_patient_doctor_selections"
        unique_together = (("patient_id", "doctor_id"),)
