"""
Views for AiSurgeonPilot endpoints migrated from direct Supabase calls.

Each view should return JSON in the exact same shape PostgREST returned, so
the frontend can be swapped over behind a feature flag without code changes
beyond the call site.
"""
from __future__ import annotations

from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.permissions import HasRole

from .models import Doctor
from .serializers import DoctorSerializer


class ClinicDoctorListView(ListAPIView):
    """Replaces the Supabase call on the Branding page:

        supabase.from('doc_doctors')
                .select('*')
                .eq('role', 'doctor')
                .eq('created_by', currentUser.id)
                .order('full_name')

    Authorization: requester must hold `clinical_admin` or `superadmin`.
    Filtering: only doctors *they* created (`created_by = request.user.id`).
    """

    serializer_class = DoctorSerializer
    permission_classes = [IsAuthenticated, HasRole("clinical_admin", "superadmin")]
    pagination_class = None  # plain array, mirroring PostgREST default response shape

    def get_queryset(self):
        return (
            Doctor.objects.filter(role="doctor", created_by=self.request.user.id)
            .order_by("full_name")
        )

    def list(self, request, *args, **kwargs):
        # Override to guarantee a plain array — DRF's default already does this
        # when pagination_class is None, but be explicit so the response shape
        # is locked down even if a developer adds pagination later.
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
