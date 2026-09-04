"""Default availability schedules for locally-created doctors.

Patients can only book doctors that have active `doc_availability` rows.
Doctors created through local flows start with none, which made them
unbookable ("Doctor unavailable" on every date). New doctors therefore get a
sensible default schedule (Mon-Sat, 09:00-13:00 and 14:00-17:00, 30-minute
slots) that they can adjust once the availability editor migrates local.
"""
from __future__ import annotations

from django.db import connection

DEFAULT_SLOT_MINUTES = 30
DEFAULT_WINDOWS = [("09:00", "13:00"), ("14:00", "17:00")]
# doc_availability.day_of_week: 0=Sunday..6=Saturday -> Mon..Sat = 1..6
DEFAULT_DAYS = [1, 2, 3, 4, 5, 6]


def create_default_availability(doctor_id: str) -> int:
    """Insert the default weekly schedule if the doctor has none.

    Returns the number of rows inserted (0 when a schedule already exists).
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM doc_availability WHERE doctor_id = %s LIMIT 1",
            [doctor_id],
        )
        if cur.fetchone() is not None:
            return 0

        inserted = 0
        for day in DEFAULT_DAYS:
            for start, end in DEFAULT_WINDOWS:
                cur.execute(
                    """
                    INSERT INTO doc_availability (
                        doctor_id, day_of_week, start_time, end_time,
                        slot_duration, is_active, visit_type
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, true, ARRAY['online', 'physical']::text[]
                    )
                    """,
                    [doctor_id, day, start, end, DEFAULT_SLOT_MINUTES],
                )
                inserted += cur.rowcount
    return inserted
