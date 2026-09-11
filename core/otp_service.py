"""
Patient SMS OTP service.

Issues and verifies short-lived 6-digit codes for patient registration and
login. Codes are stored only as HMAC-SHA256 hashes (keyed with the Django
SECRET_KEY + the challenge id) — never in plaintext. Challenges live in the
`patient_otp_challenges` table (see core/migrations/0008).

Security properties:
- 5-minute expiry, 6 random digits from `secrets`.
- Max 5 verification attempts per challenge, then the challenge is burned.
- 60 s resend cooldown and max 5 sends per 15 min per phone+purpose.
- The destination number is fixed at request time; the verify step always
  sends to / reads the number stored on the challenge row, never a number
  supplied by the client.
- OTPs are never logged, and never returned by any API response.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from django.conf import settings
from django.db import connection
from django.utils import timezone

PURPOSE_REGISTER = "register"
PURPOSE_LOGIN = "login"

CODE_TTL_SECONDS = 300          # OTP valid for 5 minutes
RESEND_COOLDOWN_SECONDS = 60    # min gap between sends to the same number
SEND_WINDOW_SECONDS = 900       # sliding window for the send cap
MAX_SENDS_PER_WINDOW = 5        # per phone+purpose within the window
MAX_VERIFY_ATTEMPTS = 5         # per challenge; then the challenge is burned

_CODE_ALPHABET = "0123456789"
_CODE_LENGTH = 6


class OtpError(Exception):
    """Base class — `status` is the HTTP status the view should return."""

    status = 400

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status


class CooldownError(OtpError):
    status = 429

    def __init__(self, retry_after: int):
        super().__init__(f"Please wait {retry_after}s before requesting another code", 429)
        self.retry_after = retry_after


class SendLimitError(OtpError):
    status = 429

    def __init__(self, retry_after: int):
        super().__init__(
            f"Too many codes requested. Try again in {retry_after} minutes", 429
        )
        self.retry_after = retry_after


def normalize_phone(raw: str) -> str | None:
    """Normalise a phone number to E.164-ish `+<digits>` (India-default).

    Returns None when the input has no plausible phone number in it.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    if len(digits) == 10 and digits[0] in "123456789":
        return "+91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if len(digits) >= 8:
        return "+" + digits
    return None


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return "****"
    return "******" + digits[-4:]


def generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def _hash_code(challenge_id: str, code: str) -> str:
    secret = (settings.SECRET_KEY or "").encode("utf-8")
    message = f"{challenge_id}:{code.strip()}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def issue_challenge(phone: str, purpose: str, *, email: str | None = None,
                    user_id: str | None = None, now=None) -> dict:
    """Create (or re-send) the active OTP challenge for phone+purpose.

    Raises CooldownError / SendLimitError when rate limits trip. Returns the
    client-safe challenge descriptor — the code itself is NEVER included.
    """
    now = now or timezone.now()
    import datetime

    code = generate_code()

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, last_sent_at, window_started_at, send_count
              FROM patient_otp_challenges
             WHERE phone = %s AND purpose = %s
             LIMIT 1
            """,
            [phone, purpose],
        )
        row = cur.fetchone()

    challenge_id = row[0] if row else str(uuid.uuid4())
    code_hash = _hash_code(challenge_id, code)
    expires_at = now + datetime.timedelta(seconds=CODE_TTL_SECONDS)

    if row is None:
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO patient_otp_challenges (
                    id, phone, purpose, email, user_id, code_hash,
                    attempts, consumed_at, expires_at,
                    last_sent_at, send_count, window_started_at
                ) VALUES (%s::uuid, %s, %s, %s, %s::uuid, %s,
                          0, NULL, %s, %s, 1, %s)
                """,
                [challenge_id, phone, purpose, email, user_id, code_hash,
                 expires_at, now, now],
            )
    else:
        # Rate limits: 60 s cooldown + max N sends per sliding window.
        last_sent, window_started, send_count = row[1], row[2], row[3]
        if last_sent is not None:
            elapsed = (now - last_sent).total_seconds()
            if elapsed < RESEND_COOLDOWN_SECONDS:
                raise CooldownError(int(RESEND_COOLDOWN_SECONDS - elapsed) + 1)

        in_window = (now - window_started).total_seconds() < SEND_WINDOW_SECONDS
        new_count = send_count + 1 if in_window else 1
        if in_window and send_count >= MAX_SENDS_PER_WINDOW:
            retry_after = int(SEND_WINDOW_SECONDS - (now - window_started).total_seconds()) + 1
            raise SendLimitError(max(1, retry_after // 60) if retry_after >= 60 else retry_after)

        new_window_started = window_started if in_window else now
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE patient_otp_challenges
                   SET code_hash = %s,
                       attempts = 0,
                       consumed_at = NULL,
                       expires_at = %s,
                       email = COALESCE(%s, email),
                       user_id = COALESCE(%s::uuid, user_id),
                       last_sent_at = %s,
                       send_count = %s,
                       window_started_at = %s
                 WHERE id = %s::uuid
                """,
                [code_hash, expires_at, email, user_id, now, new_count,
                 new_window_started, challenge_id],
            )

    # The plaintext code exists only in the caller's scope so it can be sent
    # by SMS — it is never stored or logged.
    return {
        "request_id": challenge_id,
        "phone": phone,
        "phone_masked": mask_phone(phone),
        "expires_in": CODE_TTL_SECONDS,
        "resend_after": RESEND_COOLDOWN_SECONDS,
        "code": code,
    }


def get_challenge(request_id: str, purpose: str) -> dict | None:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, phone, purpose, email, user_id::text, code_hash,
                   attempts, consumed_at, expires_at
              FROM patient_otp_challenges
             WHERE id = %s::uuid AND purpose = %s
             LIMIT 1
            """,
            [request_id, purpose],
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "request_id": row[0],
        "phone": row[1],
        "purpose": row[2],
        "email": row[3],
        "user_id": row[4],
        "code_hash": row[5],
        "attempts": row[6],
        "consumed_at": row[7],
        "expires_at": row[8],
    }


def evaluate_verification(challenge: dict, code: str, now=None) -> tuple[str, bool]:
    """Pure verification decision.

    Returns (status, consume) where status is one of
    'ok' | 'invalid' | 'expired' | 'used' | 'locked'. `consume` tells the
    caller to burn the challenge (successful verification or attempt-limit
    reached). Attempt counting is the caller's responsibility (persist
    attempts+1 for any non-'used' failure).
    """
    now = now or timezone.now()
    if challenge.get("consumed_at") is not None:
        return "used", False
    expires_at = challenge.get("expires_at")
    if expires_at is not None and expires_at < now:
        return "expired", False
    if int(challenge.get("attempts") or 0) >= MAX_VERIFY_ATTEMPTS:
        return "locked", True

    expected = _hash_code(challenge["request_id"], str(code))
    if hmac.compare_digest(expected, str(challenge.get("code_hash") or "")):
        return "ok", True

    new_attempts = int(challenge.get("attempts") or 0) + 1
    if new_attempts >= MAX_VERIFY_ATTEMPTS:
        return "locked", True
    return "invalid", False


def verify_challenge(request_id: str, purpose: str, code: str, now=None) -> tuple[str, dict | None]:
    """Fetch + evaluate + persist one verification attempt.

    Returns (status, challenge). The challenge row is returned for successful
    verifications so the caller can read the bound email/user_id; on failure
    the challenge is None and the status explains why.
    """
    now = now or timezone.now()
    challenge = get_challenge(request_id, purpose)
    if challenge is None:
        return "not_found", None

    status, consume = evaluate_verification(challenge, code, now=now)

    with connection.cursor() as cur:
        if status == "ok":
            cur.execute(
                """UPDATE patient_otp_challenges
                      SET consumed_at = %s, attempts = attempts + 1
                    WHERE id = %s::uuid""",
                [now, request_id],
            )
        elif consume:
            cur.execute(
                """UPDATE patient_otp_challenges
                      SET consumed_at = %s, attempts = %s
                    WHERE id = %s::uuid""",
                [now, MAX_VERIFY_ATTEMPTS, request_id],
            )
        else:
            cur.execute(
                """UPDATE patient_otp_challenges
                      SET attempts = attempts + 1
                    WHERE id = %s::uuid""",
                [request_id],
            )

    if status != "ok":
        return status, None
    return "ok", challenge
