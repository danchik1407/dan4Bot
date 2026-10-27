-- Shop purchase counters and UTC epoch-day marker.
ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS daily_buys TEXT NOT NULL DEFAULT '{}';

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS last_shop_reset BIGINT NOT NULL DEFAULT 0;

UPDATE public.users
SET daily_buys = '{}'
WHERE daily_buys IS NULL OR btrim(daily_buys) = '';

UPDATE public.users
SET last_shop_reset = 0
WHERE last_shop_reset IS NULL;

ALTER TABLE public.users
    ALTER COLUMN daily_buys SET DEFAULT '{}',
    ALTER COLUMN daily_buys SET NOT NULL,
    ALTER COLUMN last_shop_reset SET DEFAULT 0,
    ALTER COLUMN last_shop_reset SET NOT NULL;

NOTIFY pgrst, 'reload schema';
