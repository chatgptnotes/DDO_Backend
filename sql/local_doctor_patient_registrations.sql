-- Doctor-patient registration tracking (local PostgreSQL).
--
-- A patient is registered on a doctor's portal either explicitly ('manual' —
-- the roster that already existed before automatic registration shipped) or
-- automatically ('auto' — created the moment an appointment reaches a live
-- status: confirmed / completed / no_show).
--
-- Cancellation removes ONLY 'auto' registrations, and only when the patient
-- has no other live appointment with that doctor. 'manual' rows are never
-- touched by the trigger.

CREATE TABLE IF NOT EXISTS doc_doctor_patient_registrations (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    doctor_id     uuid NOT NULL REFERENCES doc_doctors(id) ON DELETE CASCADE,
    patient_id    uuid NOT NULL REFERENCES doc_patients(id) ON DELETE CASCADE,
    source        text NOT NULL DEFAULT 'auto'
                  CHECK (source IN ('manual', 'auto')),
    appointment_id uuid REFERENCES doc_appointments(id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (doctor_id, patient_id)
);

CREATE INDEX IF NOT EXISTS idx_doc_dpr_doctor
    ON doc_doctor_patient_registrations (doctor_id);

-- Keep registrations in sync with appointment status changes, no matter
-- which service performs them (Django patient flows, Next doctor flows).
CREATE OR REPLACE FUNCTION fn_sync_doctor_patient_registration()
RETURNS trigger AS $$
BEGIN
    IF NEW.patient_id IS NULL OR NEW.doctor_id IS NULL THEN
        RETURN NEW;
    END IF;

    -- Legacy appointment rows can reference patients that no longer exist
    -- (patient_id FK is not enforced); skip them rather than fail the write.
    IF NOT EXISTS (SELECT 1 FROM doc_patients p WHERE p.id = NEW.patient_id) THEN
        RETURN NEW;
    END IF;

    IF NEW.status IN ('confirmed', 'completed', 'no_show') THEN
        INSERT INTO doc_doctor_patient_registrations
            (doctor_id, patient_id, source, appointment_id)
        VALUES
            (NEW.doctor_id, NEW.patient_id, 'auto', NEW.id)
        ON CONFLICT (doctor_id, patient_id) DO NOTHING;
    ELSIF NEW.status = 'cancelled' AND OLD.status IS DISTINCT FROM 'cancelled' THEN
        DELETE FROM doc_doctor_patient_registrations r
         WHERE r.doctor_id = NEW.doctor_id
           AND r.patient_id = NEW.patient_id
           AND r.source = 'auto'
           AND NOT EXISTS (
                SELECT 1
                  FROM doc_appointments a
                 WHERE a.doctor_id = NEW.doctor_id
                   AND a.patient_id = NEW.patient_id
                   AND a.status IN ('confirmed', 'completed', 'no_show')
                   AND a.id <> NEW.id
               );
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_doc_appointments_registration ON doc_appointments;
CREATE TRIGGER trg_doc_appointments_registration
    AFTER INSERT OR UPDATE OF status ON doc_appointments
    FOR EACH ROW
    EXECUTE FUNCTION fn_sync_doctor_patient_registration();

-- One-time backfill: the roster that already exists today becomes 'manual'
-- and is therefore never auto-removed. Covers every appointment-linked
-- patient (any status) plus explicit active selections.
INSERT INTO doc_doctor_patient_registrations (doctor_id, patient_id, source)
SELECT DISTINCT a.doctor_id, a.patient_id, 'manual'
  FROM doc_appointments a
  JOIN doc_patients p ON p.id = a.patient_id
ON CONFLICT (doctor_id, patient_id) DO NOTHING;

INSERT INTO doc_doctor_patient_registrations (doctor_id, patient_id, source)
SELECT DISTINCT s.doctor_id, s.patient_id, 'manual'
  FROM doc_patient_doctor_selections s
  JOIN doc_patients p ON p.id = s.patient_id
 WHERE s.status = 'active'
ON CONFLICT (doctor_id, patient_id) DO NOTHING;
