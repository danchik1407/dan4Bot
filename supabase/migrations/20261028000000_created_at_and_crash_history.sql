-- Profile "days in game" + persistent global crash history.

-- 1) users.created_at: needed for the "Дней в игре" counter.
ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;

-- Existing rows have no signup date: use the moment of the migration so the
-- counter starts from zero instead of showing a bogus 1970 date.
UPDATE public.users
SET created_at = now()
WHERE created_at IS NULL;

ALTER TABLE public.users
    ALTER COLUMN created_at SET DEFAULT now(),
    ALTER COLUMN created_at SET NOT NULL;

-- 2) Global rocket history: survives restarts and does not depend on who is online.
CREATE TABLE IF NOT EXISTS public.crash_history (
    id BIGSERIAL PRIMARY KEY,
    round_id TEXT,
    crash_point NUMERIC(12, 2) NOT NULL DEFAULT 0,
    players JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS crash_history_created_at_idx
    ON public.crash_history (created_at DESC);

-- Keep the table small: only the newest rounds are ever shown.
DELETE FROM public.crash_history
WHERE id NOT IN (SELECT id FROM public.crash_history ORDER BY id DESC LIMIT 200);

NOTIFY pgrst, 'reload schema';
