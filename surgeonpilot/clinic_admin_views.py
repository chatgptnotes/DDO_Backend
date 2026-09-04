"""Clinic Admin creation endpoint backed only by local PostgreSQL."""
from __future__ import annotations

import logging

from django.db import IntegrityError, connection
from rest_framework import status
from rest_framework import serializers
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.roles import has_role

from .clinic_admin import ClinicAdminCreateSerializer

logger = logging.getLogger(__name__)


class IsSuperAdminOrLegacySuperAdmin(BasePermission):
    """Accept canonical roles and the current legacy SuperAdmin profile.

    Existing SuperAdmins predate ``user_roles``.  The narrowly-scoped legacy
    check is a migration bridge, not a general role fallback.
    """

    message = "SuperAdmin access is required."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if has_role(str(user.id), "superadmin"):
            return True
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM public.doc_doctors
                WHERE user_id = %s AND role = 'superadmin' AND COALESCE(is_active, true) = true
                LIMIT 1
                """,
                [str(user.id)],
            )
            if cursor.fetchone() is not None:
                return True
            # Portal JWTs carry the remote Supabase auth uuid, which need not
            # exist in this local database. Fall back to the verified token's
            # email to find the requester's local superadmin profile.
            email = getattr(user, "email", None)
            if not email:
                return False
            cursor.execute(
                """
                SELECT 1 FROM public.doc_doctors
                WHERE lower(email) = lower(%s)
                  AND role = 'superadmin' AND COALESCE(is_active, true) = true
                LIMIT 1
                """,
                [email],
            )
            return cursor.fetchone() is not None


class ClinicAdminCreateView(APIView):
    """POST /api/surgeon/clinic-admins/"""

    permission_classes = [IsAuthenticated, IsSuperAdminOrLegacySuperAdmin]

    def post(self, request):
        serializer = ClinicAdminCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        try:
            result = serializer.save()
        except serializers.ValidationError as exc:
            return Response(
                {"success": False, "error": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except IntegrityError:
            # A concurrent database uniqueness violation must not disclose
            # account details and the surrounding transaction has rolled back.
            return Response(
                {"success": False, "error": "A user with this email already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Clinic Admin creation failed")
            return Response(
                {"success": False, "error": "Unable to create Clinic Admin."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "success": True,
                "admin": {
                    "id": str(result["doctor"].id),
                    "user_id": str(result["user"].id),
                    "email": result["user"].email,
                    "full_name": result["user"].full_name,
                    "clinic_id": None,
                    "role": "clinical_admin",
                },
            },
            status=status.HTTP_201_CREATED,
        )
