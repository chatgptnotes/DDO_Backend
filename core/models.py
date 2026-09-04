"""
Local Django models for patient storage (not Supabase mirrors).

`LocalUser` maps onto the shared `public.users` table (UUID primary key) —
the same table the legacy stack populated — instead of a Django-defaulted
`core_localuser` table. `PatientProfile` keeps extended patient info.
"""
import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models


class LocalUserManager(BaseUserManager):
    """Manager for LocalUser — users are created by email (no username column)."""

    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("An email address is required")
        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("role", "admin")
        return self.create_user(email, password, **extra_fields)


class LocalUser(AbstractBaseUser):
    """
    App-level user stored in `public.users` (UUID ids, `auth_user_id` link to
    Supabase auth). Replaces Supabase Auth for local patient creation.

    Extends AbstractBaseUser (not AbstractUser) because `users` has no
    username/is_staff/date_joined columns. Credentials stay Django-hashed in
    `users.password`; legacy rows with NULL password simply cannot log in.
    """
    ROLE_CHOICES = [
        ('patient', 'Patient'),
        ('doctor', 'Doctor'),
        ('admin', 'Admin'),
        ('telecaller', 'Telecaller'),
    ]

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list = []

    objects = LocalUserManager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    auth_user_id = models.UUIDField(blank=True, null=True)
    email = models.EmailField(unique=True, blank=True, null=True)
    full_name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    role = models.CharField(max_length=50, choices=ROLE_CHOICES, default='patient')
    is_indian_resident = models.BooleanField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # AbstractBaseUser supplies `password` (CharField 128) and `last_login`
    # (nullable DateTimeField). `users.password` is nullable text (legacy rows
    # are NULL), so widen it; the `last_login` column is added by migration 0003.
    password = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "users"
        verbose_name = "user"
        verbose_name_plural = "users"

    def __str__(self):
        return f"{self.email} ({self.role})"


class PatientProfile(models.Model):
    """
    Extended patient information stored locally.
    """
    GENDER_CHOICES = [
        ('male', 'Male'),
        ('female', 'Female'),
        ('other', 'Other'),
    ]

    user = models.OneToOneField(LocalUser, on_delete=models.CASCADE, related_name='patient_profile')
    date_of_birth = models.DateField(blank=True, null=True)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    postal_code = models.CharField(max_length=20, blank=True, null=True)
    country = models.CharField(max_length=100, default='India')
    emergency_contact_name = models.CharField(max_length=255, blank=True, null=True)
    emergency_contact_phone = models.CharField(max_length=20, blank=True, null=True)
    blood_group = models.CharField(max_length=5, blank=True, null=True)
    medical_history = models.TextField(blank=True, null=True)
    allergies = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Patient: {self.user.email}"


class DocPatient(models.Model):
    """Canonical patient-portal profile in the existing ``doc_patients`` table."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        LocalUser,
        db_column="user_id",
        on_delete=models.SET_NULL,
        related_name="doc_patient_profiles",
        blank=True,
        null=True,
        db_constraint=False,
    )
    email = models.EmailField(blank=True, null=True)
    first_name = models.TextField(blank=True, null=True)
    last_name = models.TextField(blank=True, null=True)
    phone_number = models.TextField(blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True)
    gender = models.TextField(blank=True, null=True)
    blood_group = models.TextField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    registration_step = models.IntegerField(blank=True, null=True)
    registration_completed = models.BooleanField(blank=True, null=True)
    is_indian_resident = models.BooleanField(blank=True, null=True)
    intake_form_completed = models.BooleanField(blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "doc_patients"


class UserRole(models.Model):
    """Mapping for the existing PostgreSQL ``user_roles`` ledger."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        LocalUser,
        db_column="user_id",
        on_delete=models.CASCADE,
        related_name="role_assignments",
        db_constraint=False,
    )
    role = models.CharField(max_length=50)
    scope_id = models.UUIDField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.UUIDField(blank=True, null=True)
    revoked_at = models.DateTimeField(blank=True, null=True)
    revoked_by = models.UUIDField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "user_roles"
