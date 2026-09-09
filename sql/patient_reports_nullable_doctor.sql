-- doc_patient_reports.doctor_id must be nullable: rows uploaded_by = 'patient'
-- have no doctor yet (the remote Supabase schema allowed NULL; the local copy
-- carried a NOT NULL constraint that broke patient uploads).
ALTER TABLE public.doc_patient_reports ALTER COLUMN doctor_id DROP NOT NULL;
