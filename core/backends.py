"""
Custom authentication backends for local session login.

`GotrueAuthBackend` verifies credentials against `auth.users.encrypted_password`
(bcrypt, GoTrue-format) — the same table Supabase Auth uses — and returns the
mirrored `public.users` row (Django's LocalUser) for the session. This keeps
auth.users the single credential store while public.users stays a profile
mirror, exactly like the hosted Supabase setup.

`django.contrib.auth.backends.ModelBackend` (listed after it in settings)
still handles the accounts migrated from the old core_localuser table, whose
PBKDF2 hashes live in public.users.password.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend

class GotrueAuthBackend(BaseBackend):
    """authenticate(username=<email>, password=<plain>) against auth.users."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None
        # bcrypt is only needed for legacy GoTrue-shaped password hashes.
        # Keep new Django-hashed public.users accounts sign-in capable even if
        # a development environment has not installed that optional package.
        try:
            from . import gotrue_local
        except ImportError:
            return None
        if not gotrue_local.verify_gotrue_credentials(username, password):
            return None

        user_model = get_user_model()
        try:
            return user_model.objects.get(email__iexact=username)
        except user_model.DoesNotExist:
            # auth row exists but the public.users mirror is missing.
            return None

    def get_user(self, user_id):
        user_model = get_user_model()
        try:
            return user_model.objects.get(pk=user_id)
        except user_model.DoesNotExist:
            return None
