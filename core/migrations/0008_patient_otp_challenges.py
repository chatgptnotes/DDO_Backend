"""
OTP challenges for patient SMS verification (Twilio-delivered).

Codes are stored only as HMAC-SHA256 hashes; the table holds one active
challenge per (phone, purpose) with attempt counters and send-rate windows.
Managed via raw SQL to match the schema-first layout of this database.
"""
from django.db import migrations

SQL_CREATE = """
CREATE TABLE IF NOT EXISTS patient_otp_challenges (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone             text NOT NULL,
    purpose           text NOT NULL CHECK (purpose IN ('register', 'login')),
    email             text,
    user_id           uuid REFERENCES users (id) ON DELETE CASCADE,
    code_hash         text NOT NULL,
    attempts          smallint NOT NULL DEFAULT 0,
    consumed_at       timestamptz,
    expires_at        timestamptz NOT NULL,
    last_sent_at      timestamptz NOT NULL DEFAULT now(),
    send_count        integer NOT NULL DEFAULT 1,
    window_started_at timestamptz NOT NULL DEFAULT now(),
    created_at        timestamptz NOT NULL DEFAULT now()
);
"""

SQL_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS patient_otp_challenges_phone_purpose_idx
    ON patient_otp_challenges (phone, purpose);
CREATE INDEX IF NOT EXISTS patient_otp_challenges_expires_idx
    ON patient_otp_challenges (expires_at);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0007_ensure_public_users_table"),
    ]

    operations = [
        migrations.RunSQL(
            SQL_CREATE + SQL_INDEXES,
            "DROP TABLE IF EXISTS patient_otp_challenges;",
        ),
    ]
