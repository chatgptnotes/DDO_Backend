CREATE TABLE IF NOT EXISTS public.adamrit_sync_jobs (
  id uuid PRIMARY KEY,
  requested_by_id uuid NOT NULL,
  status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  result_summary jsonb,
  error_message text
);

CREATE INDEX IF NOT EXISTS idx_adamrit_sync_jobs_queue
  ON public.adamrit_sync_jobs (status, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_adamrit_sync_job
  ON public.adamrit_sync_jobs ((true))
  WHERE status IN ('queued', 'running');

ALTER TABLE public.adamrit_sync_jobs ENABLE ROW LEVEL SECURITY;
