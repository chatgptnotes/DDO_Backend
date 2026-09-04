"""
Top-level API endpoints provided by the `core` app:

- HealthView (`GET /api/health/`)  — public liveness check; never auth.
- MeView     (`GET /api/me/`)      — returns the authenticated user + roles.
"""
from __future__ import annotations

from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import roles as roles_module


class HealthView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"status": "ok", "service": "backend", "version": "0.1.0"})


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        roles = roles_module.list_roles(user.id)
        active = roles_module.get_active_role(request, roles)
        if active is None and roles:
            active = roles[0]
        return Response(
            {
                "id": user.id,
                "email": user.email,
                "full_name": getattr(user, "full_name", ""),
                "role": getattr(user, "role", None) or (roles[0] if roles else None),
                "roles": roles,
                "active_role": active,
            }
        )
