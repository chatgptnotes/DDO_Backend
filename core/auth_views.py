"""Django session endpoints for local PostgreSQL accounts."""
import logging
import re

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.otp_service import (
    PURPOSE_LOGIN,
    CooldownError,
    SendLimitError,
    issue_challenge,
    normalize_phone,
    verify_challenge,
)
from core.twilio_client import send_sms

logger = logging.getLogger("core")

OTP_STATUS_HTTP = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "invalid": status.HTTP_400_BAD_REQUEST,
    "expired": status.HTTP_400_BAD_REQUEST,
    "used": status.HTTP_400_BAD_REQUEST,
    "locked": status.HTTP_429_TOO_MANY_REQUESTS,
}

OTP_ERROR_MESSAGES = {
    "not_found": "Verification request not found or expired. Please request a new code.",
    "invalid": "Incorrect code. Please try again.",
    "expired": "This code has expired. Please request a new one.",
    "used": "This code was already used. Please request a new one.",
    "locked": "Too many incorrect attempts. Please request a new code.",
}


def _send_otp_sms(phone, code):
    """Deliver the OTP via Twilio. The code is only ever placed in the SMS
    body — never logged, never returned in an API response."""
    message = (
        f"Your DDO verification code is {code}. "
        f"It expires in 5 minutes. Never share this code with anyone."
    )
    return send_sms(phone, message)

from core.otp_service import (
    CODE_TTL_SECONDS,
    PURPOSE_LOGIN,
    RESEND_COOLDOWN_SECONDS,
    CooldownError,
    OtpError,
    SendLimitError,
    issue_challenge,
    mask_phone,
    normalize_phone,
    verify_challenge,
)
from core.twilio_client import send_sms

logger = logging.getLogger("core")

OTP_STATUS_HTTP = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "invalid": status.HTTP_400_BAD_REQUEST,
    "expired": status.HTTP_400_BAD_REQUEST,
    "used": status.HTTP_400_BAD_REQUEST,
    "locked": status.HTTP_429_TOO_MANY_REQUESTS,
}

OTP_ERROR_MESSAGES = {
    "not_found": "Verification request not found or expired. Please request a new code.",
    "invalid": "Incorrect code. Please try again.",
    "expired": "This code has expired. Please request a new one.",
    "used": "This code was already used. Please request a new one.",
    "locked": "Too many incorrect attempts. Please request a new code.",
}


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
        """Login user and create session."""
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
            # Patients must pass a second factor (SMS OTP) before a session is
            # created. Staff/admin keep the password-only flow.
            if getattr(user, "role", "") == "patient":
                phone = normalize_phone(getattr(user, "phone", "") or "")
                if phone is None:
                    # No registered mobile → nothing to send the OTP to.
                    # Legacy accounts fall back to password-only auth.
                    login(request, user)
                    return Response({
                        "success": True,
                        "message": "Login successful",
                        "phone_on_file": False,
                        "data": {
                            "id": user.id,
                            "email": user.email,
                            "full_name": user.full_name,
                            "role": user.role,
                        },
                    })

                try:
                    challenge = issue_challenge(
                        phone, PURPOSE_LOGIN, user_id=str(user.id)
                    )
                except (CooldownError, SendLimitError) as exc:
                    return Response({
                        'success': False,
                        'otp_required': True,
                        'message': exc.message,
                        'retry_after': getattr(exc, 'retry_after', None),
                    }, status=exc.status)

                logger.info("Patient login OTP issued (request_id=%s)", challenge["request_id"])
                return Response({
                    'success': True,
                    'otp_required': True,
                    'request_id': challenge["request_id"],
                    'phone_masked': challenge["phone_masked"],
                    'expires_in': challenge["expires_in"],
                    'resend_after': challenge["resend_after"],
                })

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


def _find_patient_by_phone(phone):
    """Return the single patient account whose mobile matches `phone`
    (normalized), None when none, or 'ambiguous' when several match."""
    digits = re.sub(r"\D", "", phone)
    user_model = get_user_model()
    candidates = user_model.objects.filter(
        role="patient", phone__isnull=False
    ).values_list("id", "email", "phone")
    matched = [
        m for m in candidates
        if re.sub(r"\D", "", m[2] or "").endswith(digits[-10:])
    ]
    if not matched:
        return None
    if len(matched) > 1:
        return "ambiguous"
    return matched[0]


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
      {"phone": "+919876543210", "purpose": "login"}
      {"phone": "+919876543210", "purpose": "register", "email": "p@x.com"}

    - register: the email must not belong to an existing account; the phone
      and email are bound to the challenge and verified at completion.
    - login: the phone must belong to exactly one patient account; the
      challenge is bound to that account id.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        body = request.data or {}
        purpose = str(body.get("purpose") or "").strip()
        phone = normalize_phone(str(body.get("phone") or ""))

        if purpose not in (PURPOSE_LOGIN, "register"):
            return Response(
                {"success": False, "message": "purpose must be 'register' or 'login'"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if phone is None:
            return Response(
                {"success": False, "message": "A valid mobile number is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_model = get_user_model()
        email = str(body.get("email") or "").strip().lower()

        if purpose == "register":
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
        else:
            match = _find_patient_by_phone(phone)
            if match is None:
                return Response(
                    {"success": False,
                     "message": "No account found with this mobile number"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            if match == "ambiguous":
                logger.error("Ambiguous patient phone lookup")
                return Response(
                    {"success": False,
                     "message": "Multiple accounts share this number. Please sign in with email."},
                    status=status.HTTP_409_CONFLICT,
                )
            user_id, _email, _phone = match
            challenge, error = _issue_otp_or_error(
                phone, PURPOSE_LOGIN, user_id=str(user_id)
            )

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


@method_decorator(csrf_protect, name="dispatch")
class PatientOtpVerifyView(APIView):
    """
    POST /api/auth/otp/verify/ - verify a login OTP and open the session.

    Body: {"request_id": "<uuid>", "code": "123456"}

    The destination number and account are the ones bound to the challenge at
    request time — the client cannot redirect the code elsewhere.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        body = request.data or {}
        request_id = str(body.get("request_id") or "").strip()
        code = str(body.get("code") or "").strip()

        if not request_id or not code:
            return Response(
                {"success": False, "message": "request_id and code are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        otp_status, challenge = verify_challenge(request_id, PURPOSE_LOGIN, code)

        if otp_status != "ok":
            return Response(
                {"success": False,
                 "message": OTP_ERROR_MESSAGES.get(otp_status, "Verification failed")},
                status=OTP_STATUS_HTTP.get(otp_status, status.HTTP_400_BAD_REQUEST),
            )

        user_model = get_user_model()
        try:
            user = user_model.objects.get(id=challenge["user_id"])
        except user_model.DoesNotExist:
            return Response(
                {"success": False, "message": "Account not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        logger.info("Patient OTP login successful (request_id=%s)", request_id)

        return Response({
            "success": True,
            "message": "Login successful",
            "data": {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role,
            },
        })
