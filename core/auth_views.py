"""Django session endpoints for local PostgreSQL accounts."""
import logging

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.otp_service import (
    CooldownError,
    SendLimitError,
    issue_challenge,
    normalize_phone,
)
from core.twilio_client import send_sms

logger = logging.getLogger("core")


def _send_otp_sms(phone: str, code: str) -> tuple[bool, str | None]:
    """Deliver the OTP via Twilio. The code is only ever placed in the SMS
    body — it is never logged or returned in an API response."""
    message = (
        f"Your DDO verification code is {code}. "
        f"It expires in 5 minutes. Never share this code with anyone."
    )
    return send_sms(phone, message)


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CsrfView(APIView):
    """Set Django's CSRF cookie before a browser submits credentials."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        # Return the masked token as JSON as well as setting the CSRF cookie.
        # This works when the frontend is aidoccall.com and the API is hosted
        # at api.aidoccall.com, where JavaScript cannot read an API host-only
        # cookie directly.
        return Response({"success": True, "csrfToken": get_token(request)})


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    """
    POST /api/auth/login/ - Login with Django session authentication.

    Request body:
    {
        "email": "user@example.com",
        "password": "password123"
    }
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        """Login user and create session.

        Password-only for every role: SMS OTP is a registration-time control
        (mobile verification), never a login second factor.
        """
        email = str(request.data.get("email") or "").strip()
        password = request.data.get("password")

        if not email or not password:
            return Response({
                'success': False,
                'message': 'Email and password are required'
            }, status=status.HTTP_400_BAD_REQUEST)

        # The custom user model authenticates by email.  Passwords are passed
        # only to Django's authentication backends and are never logged.
        user = authenticate(request, username=email, password=password)

        if user is not None:
            login(request, user)
            return Response({
                'success': True,
                'message': 'Login successful',
                "data": {
                    "id": user.id,
                    "email": user.email,
                    "full_name": user.full_name,
                    "role": user.role,
                }
            })

        return Response({
            'success': False,
            'message': 'Invalid email or password'
        }, status=status.HTTP_401_UNAUTHORIZED)


class LogoutView(APIView):
    """End the current Django browser session."""

    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response({"success": True, "message": "Logged out"})


def _issue_otp_or_error(phone, purpose, **kwargs):
    """Shared request-path helper: issue a challenge and send the SMS.

    Returns (challenge, None) or (None, Response). A failed SMS delivery
    answers 503 — fail closed, since an undelivered code must never look
    like a sent one.
    """
    try:
        challenge = issue_challenge(phone, purpose, **kwargs)
    except (CooldownError, SendLimitError) as exc:
        return None, Response(
            {
                "success": False,
                "message": exc.message,
                "retry_after": getattr(exc, "retry_after", None),
            },
            status=exc.status,
        )

    ok, delivery_error = _send_otp_sms(phone, challenge["code"])
    if not ok:
        logger.error(
            "OTP SMS delivery failed (purpose=%s, error=%s)", purpose, delivery_error
        )
        return None, Response(
            {
                "success": False,
                "message": "Could not send the verification SMS right now. "
                           "Please try again in a minute.",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    logger.info("OTP issued (purpose=%s, request_id=%s)", purpose, challenge["request_id"])
    return challenge, None


@method_decorator(csrf_protect, name="dispatch")
class PatientOtpRequestView(APIView):
    """
    POST /api/auth/otp/request/ - send an SMS OTP to a patient's mobile.

    Body:
      {"phone": "+919876543210", "purpose": "register", "email": "p@x.com"}

    OTP is a registration-time control (mobile verification): the email must
    not belong to an existing account; the phone and email are bound to the
    challenge and verified at completion.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        body = request.data or {}
        purpose = str(body.get("purpose") or "").strip()
        phone = normalize_phone(str(body.get("phone") or ""))

        if purpose != "register":
            return Response(
                {"success": False, "message": "purpose must be 'register'"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if phone is None:
            return Response(
                {"success": False, "message": "A valid mobile number is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_model = get_user_model()
        email = str(body.get("email") or "").strip().lower()

        if not email:
            return Response(
                {"success": False, "message": "Email is required for registration"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if user_model.objects.filter(email__iexact=email).exists():
            return Response(
                {"success": False,
                 "message": "An account with this email already exists. Please sign in."},
                status=status.HTTP_409_CONFLICT,
            )
        challenge, error = _issue_otp_or_error(phone, "register", email=email)

        if error is not None:
            return error

        # Response carries only the request id and metadata — never the code.
        return Response({
            "success": True,
            "otp_sent": True,
            "request_id": challenge["request_id"],
            "phone_masked": challenge["phone_masked"],
            "expires_in": challenge["expires_in"],
            "resend_after": challenge["resend_after"],
        })
