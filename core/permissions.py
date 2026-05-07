"""
DRF permission classes for role-based access.

Usage:

    class PatientListView(APIView):
        permission_classes = [IsAuthenticated, HasRole("doctor", "clinical_admin")]

    class AdminOnlyView(APIView):
        permission_classes = [IsAuthenticated, HasRole("superadmin")]

`HasRole(*roles)` allows the request if the user holds ANY of the listed
roles. Combine multiple HasRole instances if you need ALL.
"""
from __future__ import annotations

from rest_framework.permissions import BasePermission

from .roles import has_role


class HasRole(BasePermission):
    message = "You do not have the required role."

    def __init__(self, *roles: str) -> None:
        if not roles:
            raise ValueError("HasRole requires at least one role")
        self.roles = roles

    def __call__(self) -> "HasRole":
        # DRF instantiates permission classes with no args; we want a
        # parametrised instance, so HasRole("doctor") returns self when called.
        return self

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return False
        return has_role(user.id, *self.roles)
