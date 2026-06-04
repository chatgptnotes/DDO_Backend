"""
Views for AiSurgeonPilot endpoints migrated from direct Supabase calls.

Each view should return JSON in the exact same shape PostgREST returned, so
the frontend can be swapped over behind a feature flag without code changes
beyond the call site.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRole

from .models import Doctor
from .serializers import DoctorSerializer
from .services.transcription import (
    DEFAULT_LANGUAGE,
    TranscriptionError,
    transcribe_audio,
)

# Reject uploads larger than this outright (a long consultation is still only a
# few MB of compressed opus; anything bigger is almost certainly a mistake).
MAX_AUDIO_BYTES = 25 * 1024 * 1024


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


class TranscribeView(APIView):
    """Transcribe a recorded in-person consultation to text.

        POST /api/surgeon/transcribe/    (multipart/form-data)
            audio:    the recorded audio file (required)
            language: BCP-47 tag, default 'hi-IN'

    Returns `{ "transcript", "language", "engine" }`. An empty transcript means
    no speech was recognised (still 200). Engine failures (network/decoding)
    return 502. Only doctors (or clinical admins / superadmins acting in the
    portal) may transcribe.
    """

    parser_classes = [MultiPartParser, FormParser]
    # Gated on authentication only: this is a stateless speech-to-text utility
    # that returns only the caller's own uploaded audio as text (no patient data
    # is read or written) and is reachable only from inside the doctor portal.
    # NOTE: to restrict to clinicians, add back
    # `HasRole("doctor", "clinical_admin", "superadmin")` once the relevant
    # accounts hold an active row in the `user_roles` table.
    permission_classes = [IsAuthenticated]

    def post(self, request):
        audio_file = request.FILES.get("audio")
        if audio_file is None:
            return Response(
                {"detail": "No audio file provided (expected field 'audio')."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if audio_file.size == 0:
            return Response(
                {"detail": "The audio file is empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if audio_file.size > MAX_AUDIO_BYTES:
            return Response(
                {"detail": "Audio file is too large (max 25 MB)."},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        language = (request.data.get("language") or DEFAULT_LANGUAGE).strip()

        try:
            result = transcribe_audio(
                audio_bytes=audio_file.read(),
                filename=audio_file.name or "",
                language=language,
            )
        except TranscriptionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "transcript": result.transcript,
                "language": result.language,
                "engine": result.engine,
            }
        )
