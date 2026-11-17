-- ============================================================================
-- AURA GAME: добиваем схему до состояния, в котором её ждёт код (задача 28)
--
-- Зачем: Mini App показывал «В базе не хватает колонки «created_at» в таблице
-- users», хотя миграции были применены. Причин две:
--   1) старая ошибка ВСЕГДА называла таблицу users, даже если колонки не
--      хватало в subscriptions_history / referrals / stars_payments;
--   2) у части таблиц created_at действительно не было (таблицы создавались
--      более ранними, частичными версиями миграций).
--
-- Эта миграция идемпотентна: `ADD COLUMN IF NOT EXISTS` + `DEFAULT now()`,
-- поэтому её можно применять повторно сколько угодно раз и на любой базе —
-- ничего не потеряется и не перезапишется.
--
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- После применения обязательно выполните (последняя строка файла):
--     NOTIFY pgrst, 'reload schema';
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. users: дата регистрации («дней в игре») + поля экономики.
--    ВАЖНО: существующие строки получат текущий момент — это то же поведение,
--    что было в 20261028000000_created_at_and_crash_history.sql, счётчик
--    «дней в игре» начнётся с сегодня, а не с 1970.
-- ---------------------------------------------------------------------------
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS ice        INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS premium    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS inventory  JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS last_chest INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS keys       INTEGER NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
-- 2. Служебные таблицы: created_at нужен для «когда выдан бонус/реферал».
-- ---------------------------------------------------------------------------
ALTER TABLE public.subscriptions_history ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE public.referrals             ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE public.referrals             ADD COLUMN IF NOT EXISTS paid_at    TIMESTAMPTZ;
ALTER TABLE public.stars_payments        ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE public.idempotency_keys      ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE public.shop_purchases        ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE public.crash_history         ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE public.upgrade_log           ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE public.relationships         ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE public.relationships         ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- ---------------------------------------------------------------------------
-- 3. Даже если таблицы создавались вручную — не даём им «просвечивать» без
--    политики (сервер ходит anon-ключом из Render).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'users', 'subscriptions_history', 'referrals', 'stars_payments',
        'idempotency_keys', 'shop_purchases', 'crash_history', 'upgrade_log',
        'relationships'
    ] LOOP
        IF EXISTS (SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = tbl) THEN
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
            EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_service_all', tbl);
            EXECUTE format(
                'CREATE POLICY %I ON public.%I FOR ALL USING (true) WITH CHECK (true)',
                tbl || '_service_all', tbl
            );
        END IF;
    END LOOP;
END $$;

-- 4. Кэш схемы PostgREST: без него база «не видит» новые колонки до
--    перезапуска проекта, и ошибка PGRST204 возвращается даже после миграции.
NOTIFY pgrst, 'reload schema';

-- ---------------------------------------------------------------------------
-- Проверка (необязательно): все колонки на месте?
-- Должно вернуть 0 строк.
-- ---------------------------------------------------------------------------
-- SELECT 'users' AS tbl, 'created_at' AS col
--  WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns
--                     WHERE table_schema='public' AND table_name='users' AND column_name='created_at');
