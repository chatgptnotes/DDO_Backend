"""
Patient SMS OTP authentication — endpoint + logic tests.

Runs without a database: the OTP store, Twilio client, user lookups, and the
registration serializer are monkeypatched at the boundary (matching the
repo-wide "no live DB in tests" posture). Pure verification logic in
core.otp_service.evaluate_verification is tested directly.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.otp_service import evaluate_verification, normalize_phone

# NOTE: no django_db marker — every DB touchpoint (OTP store, user lookups,
# registration serializer) is monkeypatched at the boundary, matching the
# repo-wide no-live-DB test posture.


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def client():
    return APIClient(enforce_csrf_checks=False)


@pytest.fixture
def challenge_row():
    """A plausible active challenge row (register purpose)."""
    return {
        "request_id": "11111111-1111-1111-1111-111111111111",
        "phone": "+919876543210",
        "purpose": "register",
        "email": "new@patient.com",
        "user_id": None,
        "code_hash": "hash-of-123456",
        "attempts": 0,
        "consumed_at": None,
        "expires_at": timezone.now() + timedelta(minutes=5),
    }


def _patch_issue(monkeypatch, phone="+919876543210", code="123456", **overrides):
    sent = {}

    def fake_issue(phone_, purpose_, **kw):
        sent["phone"] = phone_
        sent["purpose"] = purpose_
        challenge = {
            "request_id": "11111111-1111-1111-1111-111111111111",
            "phone": phone_,
            "phone_masked": "******4321",
            "expires_in": 300,
            "resend_after": 60,
            "code": code,
        }
        challenge.update(overrides)
        return challenge

    monkeypatch.setattr("core.auth_views.issue_challenge", fake_issue)
    monkeypatch.setattr(
        "core.auth_views._send_otp_sms",
        lambda phone_, code_: sent.update(sms_to=phone_, sms_code=code_) or (True, None),
    )
    return sent


# ------------------------------------------------------- pure logic checks

def test_normalize_phone_variants():
    assert normalize_phone("98765 43210") == "+919876543210"
    assert normalize_phone("+91 98765 43210") == "+919876543210"
    assert normalize_phone("09876543210") == "+919876543210"
    assert normalize_phone("919876543210") == "+919876543210"
    assert normalize_phone("not-a-phone") is None
    assert normalize_phone("") is None


def test_evaluate_ok_invalid_expired_used_locked(challenge_row):
    good = dict(challenge_row, code_hash="hmac-of-123456")
    with mock.patch("core.otp_service._hash_code", return_value="hmac-of-123456"):
        status, consume = evaluate_verification(good, "123456")
    assert (status, consume) == ("ok", True)

    status, consume = evaluate_verification(challenge_row, "000000")
    assert (status, consume) == ("invalid", False)

    expired = dict(challenge_row, expires_at=timezone.now() - timedelta(seconds=1))
    assert evaluate_verification(expired, "123456")[0] == "expired"

    used = dict(challenge_row, consumed_at=timezone.now())
    assert evaluate_verification(used, "123456")[0] == "used"

    locked = dict(challenge_row, attempts=5)
    status, consume = evaluate_verification(locked, "123456")
    assert (status, consume) == ("locked", True)

    # 5th wrong attempt burns the challenge
    four = dict(challenge_row, attempts=4)
    assert evaluate_verification(four, "000000")[0] == "locked"


# ----------------------------------------------------------- OTP request

def test_request_register_otp_sends_sms(client, monkeypatch):
    sent = _patch_issue(monkeypatch)
    monkeypatch.setattr(
        "core.auth_views.get_user_model",
        lambda: SimpleNamespace(objects=SimpleNamespace(
            filter=lambda **kw: SimpleNamespace(exists=lambda: False))),
    )
    resp = client.post(
        "/api/auth/otp/request/",
        {"phone": "98765 43210", "purpose": "register", "email": "new@patient.com"},
        format="json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["otp_sent"] is True
    assert body["request_id"]
    assert body["phone_masked"].endswith("4321")
    assert sent["phone"] == "+919876543210"       # normalized destination
    assert sent["sms_code"].isdigit() and len(sent["sms_code"]) == 6
    # The code must never appear in the response
    assert sent["sms_code"] not in resp.content.decode()


def test_request_register_otp_email_taken(client, monkeypatch):
    _patch_issue(monkeypatch)
    taken = SimpleNamespace(objects=SimpleNamespace(
        filter=lambda **kw: SimpleNamespace(exists=lambda: True)))
    monkeypatch.setattr("core.auth_views.get_user_model", lambda: taken)
    resp = client.post(
        "/api/auth/otp/request/",
        {"phone": "+919876543210", "purpose": "register", "email": "taken@patient.com"},
        format="json",
    )
    assert resp.status_code == 409


def test_request_login_otp_unknown_phone(client, monkeypatch):
    _patch_issue(monkeypatch)
    monkeypatch.setattr("core.auth_views._find_patient_by_phone", lambda phone: None)
    resp = client.post(
        "/api/auth/otp/request/",
        {"phone": "+919800000000", "purpose": "login"},
        format="json",
    )
    assert resp.status_code == 404


def test_request_otp_resend_cooldown(client, monkeypatch):
    from core.otp_service import CooldownError

    monkeypatch.setattr(
        "core.auth_views._find_patient_by_phone",
        lambda phone: ("22222222-2222-2222-2222-222222222222", "p@patient.com", "+919876543210"),
    )

    def raise_cooldown(phone_, purpose_, **kw):
        raise CooldownError(45)

    monkeypatch.setattr("core.auth_views.issue_challenge", raise_cooldown)
    resp = client.post(
        "/api/auth/otp/request/",
        {"phone": "+919876543210", "purpose": "login"},
        format="json",
    )
    assert resp.status_code == 429
    assert resp.json()["retry_after"] == 45


def test_request_otp_send_limit(client, monkeypatch):
    from core.otp_service import SendLimitError

    monkeypatch.setattr(
        "core.auth_views._find_patient_by_phone",
        lambda phone: ("22222222-2222-2222-2222-222222222222", "p@patient.com", "+919876543210"),
    )

    def raise_limit(phone_, purpose_, **kw):
        raise SendLimitError(12)

    monkeypatch.setattr("core.auth_views.issue_challenge", raise_limit)
    resp = client.post(
        "/api/auth/otp/request/",
        {"phone": "+919876543210", "purpose": "login"},
        format="json",
    )
    assert resp.status_code == 429


def test_request_otp_twilio_unconfigured(client, monkeypatch):
    _patch_issue(monkeypatch)
    monkeypatch.setattr(
        "core.auth_views._find_patient_by_phone",
        lambda phone: ("22222222-2222-2222-2222-222222222222", "p@patient.com", "+919876543210"),
    )
    monkeypatch.setattr(
        "core.auth_views._send_otp_sms",
        lambda phone_, code_: (False, "not_configured"),
    )
    resp = client.post(
        "/api/auth/otp/request/",
        {"phone": "+919876543210", "purpose": "login"},
        format="json",
    )
    assert resp.status_code == 503


# ------------------------------------------------------- login verification

def _patch_verify(monkeypatch, status_, challenge=None):
    """Patch verify_challenge in BOTH consumer modules."""
    for module in ("core.auth_views", "core.patient_views"):
        monkeypatch.setattr(
            f"{module}.verify_challenge",
            lambda rid, purpose, code, s=status_, c=challenge: (s, c),
        )


def test_verify_login_success_opens_session(client, monkeypatch):
    challenge = {
        "request_id": "r1", "phone": "+919876543210", "purpose": "login",
        "email": "p@patient.com", "user_id": "22222222-2222-2222-2222-222222222222",
        "code_hash": "x", "attempts": 4, "consumed_at": None,
        "expires_at": timezone.now(),
    }
    _patch_verify(monkeypatch, "ok", challenge)
    fake_user = SimpleNamespace(
        id=challenge["user_id"], email="p@patient.com",
        full_name="Test Patient", role="patient",
    )
    monkeypatch.setattr(
        "core.auth_views.get_user_model",
        lambda: SimpleNamespace(objects=SimpleNamespace(
            get=lambda **kw: fake_user)),
    )
    logged_in = {}
    monkeypatch.setattr(
        "core.auth_views.login",
        lambda request, user, **kw: logged_in.update(user=user),
    )

    resp = client.post(
        "/api/auth/otp/verify/",
        {"request_id": "r1", "code": "123456"},
        format="json",
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["role"] == "patient"
    assert logged_in["user"] is fake_user


def test_verify_login_invalid_code(client, monkeypatch):
    _patch_verify(monkeypatch, "invalid")
    resp = client.post(
        "/api/auth/otp/verify/", {"request_id": "r1", "code": "000000"},
        format="json",
    )
    assert resp.status_code == 400
    assert "Incorrect" in resp.json()["message"]


def test_verify_login_expired(client, monkeypatch):
    _patch_verify(monkeypatch, "expired")
    resp = client.post(
        "/api/auth/otp/verify/", {"request_id": "r1", "code": "123456"},
        format="json",
    )
    assert resp.status_code == 400
    assert "expired" in resp.json()["message"]


def test_verify_login_reused(client, monkeypatch):
    _patch_verify(monkeypatch, "used")
    resp = client.post(
        "/api/auth/otp/verify/", {"request_id": "r1", "code": "123456"},
        format="json",
    )
    assert resp.status_code == 400
    assert "already used" in resp.json()["message"]


def test_verify_login_locked_after_too_many_attempts(client, monkeypatch):
    _patch_verify(monkeypatch, "locked")
    resp = client.post(
        "/api/auth/otp/verify/", {"request_id": "r1", "code": "123456"},
        format="json",
    )
    assert resp.status_code == 429
    assert "Too many" in resp.json()["message"]


def test_verify_login_unknown_request(client, monkeypatch):
    _patch_verify(monkeypatch, "not_found")
    resp = client.post(
        "/api/auth/otp/verify/", {"request_id": "nope", "code": "123456"},
        format="json",
    )
    assert resp.status_code == 404


# ------------------------------------------------ no-phone legacy fallback

def test_login_patient_without_phone_falls_back_to_password(client, monkeypatch):
    """Accounts created before phone capture have no number to OTP — they keep
    the legacy password-only session login (requirement: don't break existing
    login). A flag tells the portal to prompt for a number."""
    fake_user = SimpleNamespace(
        id="44444444-4444-4444-4444-444444444444", email="legacy@patient.com",
        full_name="Legacy Patient", role="patient", phone="",
    )
    monkeypatch.setattr(
        "core.auth_views.authenticate",
        lambda request, **kw: fake_user,
    )
    monkeypatch.setattr(
        "core.auth_views.normalize_phone", lambda p: None,
    )
    logged_in = {}
    monkeypatch.setattr(
        "core.auth_views.login",
        lambda request, user, **kw: logged_in.update(user=user),
    )

    resp = client.post(
        "/api/auth/login/",
        {"email": "legacy@patient.com", "password": "Whatever1!"},
        format="json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["phone_on_file"] is False
    assert logged_in["user"] is fake_user


def test_login_patient_with_phone_requires_otp(client, monkeypatch):
    fake_user = SimpleNamespace(
        id="55555555-5555-5555-5555-555555555555", email="p@patient.com",
        full_name="OTP Patient", role="patient", phone="+919876543210",
    )
    monkeypatch.setattr(
        "core.auth_views.authenticate", lambda request, **kw: fake_user,
    )
    monkeypatch.setattr("core.auth_views.normalize_phone", lambda p: "+919876543210")

    challenge = {
        "request_id": "r9", "phone": "+919876543210", "phone_masked": "******4321",
        "expires_in": 300, "resend_after": 60, "code": "999999",
    }
    monkeypatch.setattr("core.auth_views.issue_challenge", lambda phone, purpose, **kw: challenge)
    monkeypatch.setattr(
        "core.auth_views._send_otp_sms", lambda phone_, code_: (True, None),
    )
    login_called = {}
    monkeypatch.setattr(
        "core.auth_views.login",
        lambda request, user, **kw: login_called.update(user=user),
    )

    resp = client.post(
        "/api/auth/login/",
        {"email": "p@patient.com", "password": "Whatever1!"},
        format="json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["otp_required"] is True
    assert body["request_id"] == "r9"
    # No session may be opened before OTP verification
    assert login_called == {}


# ---------------------------------------------------- registration gating

def test_register_requires_otp(client, monkeypatch):
    serializer_called = False

    def fake_serializer(*a, **kw):
        nonlocal serializer_called
        serializer_called = True
        raise AssertionError("serializer must not run without OTP")

    monkeypatch.setattr("core.patient_views.PatientRegistrationSerializer",
                        fake_serializer)
    resp = client.post(
        "/api/patients/register/",
        {"email": "new@patient.com", "password": "Str0ngPass!",
         "full_name": "New Patient", "is_indian_resident": True},
        format="json",
    )
    assert resp.status_code == 400
    assert "Mobile verification required" in resp.json()["message"]
    assert serializer_called is False


def test_register_phone_mismatch_rejected(client, monkeypatch):
    challenge = {
        "request_id": "r1", "phone": "+919876543210", "purpose": "register",
        "email": "new@patient.com", "user_id": None, "code_hash": "x",
        "attempts": 4, "consumed_at": None, "expires_at": timezone.now(),
    }
    _patch_verify(monkeypatch, "ok", challenge)
    monkeypatch.setattr(
        "core.patient_views.PatientRegistrationSerializer",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("serializer must not run on phone mismatch")),
    )
    resp = client.post(
        "/api/patients/register/",
        {"email": "new@patient.com", "password": "Str0ngPass!",
         "full_name": "New Patient", "is_indian_resident": True,
         "phone": "+918000000000",
         "otp_request_id": "r1", "otp_code": "123456"},
        format="json",
    )
    assert resp.status_code == 400
    assert "different mobile number" in resp.json()["message"]


def test_register_success_after_otp(client, monkeypatch):
    challenge = {
        "request_id": "r1", "phone": "+919876543210", "purpose": "register",
        "email": "new@patient.com", "user_id": None, "code_hash": "x",
        "attempts": 0, "consumed_at": None, "expires_at": timezone.now(),
    }
    _patch_verify(monkeypatch, "ok", challenge)

    saved = {}
    fake_user = SimpleNamespace(
        id="33333333-3333-3333-3333-333333333333", email="new@patient.com",
        full_name="New Patient", role="patient",
        created_at=timezone.now(),
    )

    class FakeSerializer:
        def __init__(self, data):
            saved["data"] = data

        def is_valid(self, raise_exception=False):
            saved["validated"] = True
            return True

        def save(self):
            saved["saved"] = True
            return fake_user

    monkeypatch.setattr("core.patient_views.PatientRegistrationSerializer",
                        FakeSerializer)

    resp = client.post(
        "/api/patients/register/",
        {"email": "new@patient.com", "password": "Str0ngPass!",
         "full_name": "New Patient", "is_indian_resident": True,
         "phone": "+919876543210",
         "otp_request_id": "r1", "otp_code": "123456"},
        format="json",
    )
    assert resp.status_code == 201
    assert saved["saved"] is True
    assert saved["data"]["email"] == "new@patient.com"
    assert saved["data"]["phone"] == "+919876543210"


def test_register_expired_otp_rejected(client, monkeypatch):
    _patch_verify(monkeypatch, "expired")
    resp = client.post(
        "/api/patients/register/",
        {"email": "new@patient.com", "password": "Str0ngPass!",
         "full_name": "New Patient", "is_indian_resident": True,
         "phone": "+919876543210",
         "otp_request_id": "r1", "otp_code": "123456"},
        format="json",
    )
    assert resp.status_code == 400
    assert "expired" in resp.json()["message"]
