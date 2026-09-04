"""Serializers for patient creation and management."""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from .models import DocPatient, LocalUser, PatientProfile, UserRole


class PatientRegistrationSerializer(serializers.Serializer):
    """Create a PostgreSQL-backed patient account and portal profile."""

    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True, write_only=True, trim_whitespace=False)
    full_name = serializers.CharField(required=True, allow_blank=False, trim_whitespace=True, max_length=255)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=20)
    is_indian_resident = serializers.BooleanField(required=True)

    # Accepted for backwards-compatible API validation. Fields represented by
    # doc_patients are persisted there; no core_patientprofile is created.
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    gender = serializers.ChoiceField(choices=["male", "female", "other"], required=False, allow_null=True)
    address = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    city = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    state = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    postal_code = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    emergency_contact_name = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    emergency_contact_phone = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    blood_group = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    medical_history = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    allergies = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate_email(self, value):
        value = value.strip().lower()
        if LocalUser.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def validate(self, attrs):
        candidate = LocalUser(email=attrs.get("email"), full_name=attrs.get("full_name", ""))
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        return attrs

    def create(self, validated_data):
        """Create user, canonical patient profile, and role in one transaction."""
        password = validated_data.pop("password")
        date_of_birth = validated_data.pop("date_of_birth", None)
        gender = validated_data.pop("gender", None)
        address = validated_data.pop("address", None)
        blood_group = validated_data.pop("blood_group", None)

        # These fields belong only to the retired core_patientprofile design.
        # Continue accepting them for endpoint compatibility, but never create
        # a duplicate profile record during registration.
        for field in (
            "city", "state", "postal_code", "emergency_contact_name",
            "emergency_contact_phone", "medical_history", "allergies",
        ):
            validated_data.pop(field, None)

        with transaction.atomic():
            user = LocalUser.objects.create_user(
                email=validated_data["email"],
                password=password,
                full_name=validated_data["full_name"],
                phone=validated_data.get("phone", ""),
                role="patient",
                is_indian_resident=validated_data["is_indian_resident"],
            )

            profile = (
                DocPatient.objects.select_for_update()
                .filter(email__iexact=user.email)
                .order_by("created_at", "id")
                .first()
            )
            if profile is None:
                name_parts = user.full_name.split(maxsplit=1)
                profile = DocPatient.objects.create(
                    user=user,
                    email=user.email,
                    first_name=name_parts[0],
                    last_name=name_parts[1] if len(name_parts) > 1 else "",
                    phone_number=user.phone,
                    date_of_birth=date_of_birth,
                    gender=gender,
                    blood_group=blood_group,
                    address=address,
                    registration_step=1,
                    registration_completed=False,
                    intake_form_completed=False,
                    is_indian_resident=user.is_indian_resident,
                )
            elif profile.user_id is None:
                # Preserve historic clinical/profile fields and attach the new account.
                profile.user = user
                profile.save(update_fields=["user"])
            else:
                raise serializers.ValidationError(
                    {"email": "A patient profile with this email is already linked to an account."}
                )

            if not UserRole.objects.filter(user=user, role="patient", is_active=True).exists():
                UserRole.objects.create(user=user, role="patient", is_active=True)

        return user


class PatientProfileSerializer(serializers.ModelSerializer):
    """Legacy serializer retained for existing non-registration endpoints."""

    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_full_name = serializers.CharField(source="user.full_name", read_only=True)
    user_phone = serializers.CharField(source="user.phone", read_only=True)

    class Meta:
        model = PatientProfile
        fields = [
            "id", "user", "user_email", "user_full_name", "user_phone",
            "date_of_birth", "gender", "address", "city", "state",
            "postal_code", "country", "emergency_contact_name",
            "emergency_contact_phone", "blood_group", "medical_history",
            "allergies", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
