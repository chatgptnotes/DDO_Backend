"""
Twilio SMS client — backend-only.

Credentials are read exclusively from environment variables (TWILIO_ACCOUNT_SID,
TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER) and never leave the server. The auth
token is base64-encoded for the REST call and never logged.

Stdlib-only (urllib), matching the adamrit_client pattern — no new dependency.
"""

from __future__ import annotations

import base64
import logging
import urllib.error
import urllib.parse
import urllib.request

from decouple import config

logger = logging.getLogger("core")

_TIMEOUT = 10

# Outcome constants returned as the error member of the result tuple.
NOT_CONFIGURED = "not_configured"
TWILIO_ERROR = "twilio_error"
NETWORK_ERROR = "network_error"


def _credentials() -> tuple[str, str, str] | None:
    sid = config("TWILIO_ACCOUNT_SID", default="")
    token = config("TWILIO_AUTH_TOKEN", default="")
    phone = config("TWILIO_PHONE_NUMBER", default="")
    if not sid or not token or not phone:
        return None
    return sid, token, phone


def send_sms(to: str, body: str) -> tuple[bool, str | None]:
    """Send an SMS via the Twilio REST API.

    Returns (ok, error). `error` is one of the outcome constants above —
    never the auth token, and never the message body (which contains OTPs).
    """
    creds = _credentials()
    if creds is None:
        logger.warning("Twilio not configured (TWILIO_* env vars missing) — SMS not sent")
        return False, NOT_CONFIGURED

    sid, token, from_number = creds
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({"To": to, "From": from_number, "Body": body}).encode("ascii")

    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header(
        "Authorization",
        "Basic " + base64.b64encode(f"{sid}:{token}".encode("ascii")).decode("ascii"),
    )
    request.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            response.read()
        # Log only the outcome — never the token, body, or full phone number.
        logger.info("Twilio SMS accepted (to=%s****)", str(to)[-4:])
        return True, None
    except urllib.error.HTTPError as exc:
        exc.read()
        logger.error("Twilio SMS rejected: HTTP %s", exc.code)
        return False, TWILIO_ERROR
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.error("Twilio SMS network failure: %s", exc.__class__.__name__)
        return False, NETWORK_ERROR
