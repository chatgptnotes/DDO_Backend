"""Local PostgreSQL Clinic Admin creation service.

The legacy portal calls Clinic Admin profiles ``admin_clinical``.  New role
assignments use the canonical ``user_roles.role = clinical_admin`` value while
the profile keeps the legacy value for compatibility with the current portal.
"""
from __future__ import annotations

import uuid

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection, transaction
from django.utils import timezone
from rest_framework import serializers

from core.models import LocalUser, UserRole
from core.gotrue_local import create_gotrue_user

from .models import Doctor


LEGACY_CLINIC_ADMIN_PROFILE_ROLE = "admin_clinical"
CANONICAL_CLINIC_ADMIN_ROLE = "clinical_admin"


class ClinicAdminCreateSerializer(serializers.Serializer):
    fullName = serializers.CharField(max_length=255, trim_whitespace=True)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    organizationName = serializers.CharField(max_length=255, trim_whitespace=True)
    designation = serializers.CharField(max_length=255, trim_whitespace=True)
    department = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    address = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    city = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    pincode = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_email(self, value):
        return value.strip().lower()

    def validate(self, attrs):
        user = LocalUser(email=attrs["email"], full_name=attrs["fullName"])
        try:
            validate_password(attrs["password"], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        return attrs

    @staticmethod
    def _optional(value):
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _local_requester(request) -> tuple[str, str | None]:
        """Identity of the requester in THIS local database.

        Portal JWTs carry the remote Supabase auth uuid, which need not exist
        locally. Resolve by the verified token's email and return:
          (doc_profile_id, auth_user_id)
        ``doc_profile_id`` feeds ``doc_doctors.created_by`` (no FK); the auth
        uuid feeds ``user_roles.granted_by`` (FK to the LOCAL auth.users —
        NULL when the requester has no local auth row, which the column
        permits and existing rows already use).
        """
        token_subject = str(request.user.id)
        token_email = getattr(request.user, "email", None)
        if token_email:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id::text, user_id::text
                      FROM public.doc_doctors
                     WHERE lower(email) = lower(%s) AND role = 'superadmin'
                     LIMIT 1
                    """,
                    [token_email],
                )
                row = cursor.fetchone()
            if row:
                return row[0], row[1]
        return token_subject, None

    def create(self, validated_data):
        requester_doc_id, requester_auth_id = self._local_requester(
            self.context["request"]
        )
        email = validated_data["email"]

        with transaction.atomic():
            # Serialize same-email registrations even though legacy tables do
            # not yet have a case-insensitive unique index.
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        [email],
                    )

            if LocalUser.objects.filter(email__iexact=email).exists() or Doctor.objects.filter(email__iexact=email).exists():
                raise serializers.ValidationError({"email": "A user with this email already exists."})

            user = LocalUser.objects.create_user(
                email=email,
                password=validated_data["password"],
                full_name=validated_data["fullName"],
                phone=self._optional(validated_data.get("phone")),
                role="admin",
            )

            # Give the admin a real auth identity in the local database:
            # ``doc_doctors.user_id`` has an FK to auth.users, so the profile
            # cannot be created for an account that only exists in
            # public.users. The on_auth_user_created trigger's ON CONFLICT
            # (email) merges auth_user_id into the public.users row just
            # created — it does not duplicate it.
            auth_user_id = uuid.uuid4()
            create_gotrue_user(
                user_id=auth_user_id,
                email=email,
                password=validated_data["password"],
                full_name=validated_data["fullName"],
                phone=self._optional(validated_data.get("phone")) or "",
                role="admin",
            )
            user.auth_user_id = auth_user_id
            user.save(update_fields=["auth_user_id"])

            now = timezone.now()
            # Clinic information remains on the existing profile columns.
            # ``clinic_id`` and role ``scope_id`` deliberately remain NULL:
            # Clinic Admin creation does not use the optional Stripe tenant.
            doctor = Doctor.objects.create(
                id=uuid.uuid4(),
                user_id=auth_user_id,
                email=email,
                full_name=validated_data["fullName"],
                phone=self._optional(validated_data.get("phone")),
                clinic_name=validated_data["organizationName"],
                clinic_address=self._optional(validated_data.get("address")),
                designation=validated_data["designation"],
                department=self._optional(validated_data.get("department")),
                city=self._optional(validated_data.get("city")),
                state=self._optional(validated_data.get("state")),
                pincode=self._optional(validated_data.get("pincode")),
                role=LEGACY_CLINIC_ADMIN_PROFILE_ROLE,
                consultation_type="medical",
                is_verified=True,
                # Phase 2B activation is not used for local-only users.  A
                # true value would send them to the old Supabase activation
                # endpoint, where no matching auth.users account exists.
                must_change_password=False,
                created_by=requester_doc_id,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            UserRole.objects.create(
                user=user,
                role=CANONICAL_CLINIC_ADMIN_ROLE,
                is_active=True,
                granted_by=requester_auth_id,
            )

        return {"user": user, "doctor": doctor}
