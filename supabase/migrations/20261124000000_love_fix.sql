-- ============================================================================
-- AURA GAME: задача 33 — «Не удалось создать пару» и RLS
--
-- Зачем файл: при нажатии «Да» бот писал «Не удалось создать пару — примените
-- миграцию». Причин может быть три, и все три здесь лечатся:
--   1) таблицы relationships нет вообще (миграция 20261109000000 не применялась);
--   2) таблица есть, но в ней нет колонок bank / status / created_at / updated_at
--      (её создавали руками или старой версией схемы) — Supabase отвечает
--      PGRST204 «Could not find the 'bank' column»;
--   3) таблица есть, RLS включён, а политики нет — anon-ключ из Render не может
--      вставить строку: «new row violates row-level security policy».
--
-- Файл ИДЕМПОТЕНТНЫЙ: можно применять повторно сколько угодно раз.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. users: колонки, без которых не работают награды, пассивка и premium.
--    Добавляем только отсутствующие — существующие данные не трогаем.
-- ---------------------------------------------------------------------------
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS coins     BIGINT      NOT NULL DEFAULT 0;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS keys      INTEGER     NOT NULL DEFAULT 0;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS inventory JSONB       NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS ice       INTEGER     NOT NULL DEFAULT 0;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS premium   BOOLEAN     NOT NULL DEFAULT FALSE;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS username  TEXT;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS photo_url TEXT;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- ---------------------------------------------------------------------------
-- 2. relationships — таблица пар. Создаём, если её нет, и добираем колонки.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.relationships (
    id         BIGSERIAL PRIMARY KEY,
    user_a     BIGINT      NOT NULL,
    user_b     BIGINT      NOT NULL,
    love       INTEGER     NOT NULL DEFAULT 0,
    bank       INTEGER     NOT NULL DEFAULT 0,
    status     TEXT        NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.relationships ADD COLUMN IF NOT EXISTS love       INTEGER     NOT NULL DEFAULT 0;
ALTER TABLE public.relationships ADD COLUMN IF NOT EXISTS bank       INTEGER     NOT NULL DEFAULT 0;
ALTER TABLE public.relationships ADD COLUMN IF NOT EXISTS status     TEXT        NOT NULL DEFAULT 'active';
ALTER TABLE public.relationships ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE public.relationships ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS relationships_user_a_idx ON public.relationships (user_a);
CREATE INDEX IF NOT EXISTS relationships_user_b_idx ON public.relationships (user_b);
CREATE INDEX IF NOT EXISTS relationships_status_idx ON public.relationships (status);

-- ---------------------------------------------------------------------------
-- 3. Остальные таблицы любви, одиночного счёта и бонусов.
--    Делаем их «самодостаточными»: если предыдущие миграции применялись
--    частично, всё равно всё будет работать.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.love_stage_rewards (
    relationship_id BIGINT      NOT NULL,
    stage           INTEGER     NOT NULL,
    paid_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (relationship_id, stage)
);

CREATE TABLE IF NOT EXISTS public.love_cooldowns (
    user_id   BIGINT      PRIMARY KEY,
    action_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.solo_love (
    user_id    BIGINT      PRIMARY KEY,
    love       INTEGER     NOT NULL DEFAULT 0 CHECK (love >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.solo_love_stage_rewards (
    user_id BIGINT      NOT NULL,
    stage   INTEGER     NOT NULL,
    paid_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, stage)
);

CREATE TABLE IF NOT EXISTS public.passive_income (
    user_id BIGINT      PRIMARY KEY,
    last_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.subscriptions_history (
    user_id       BIGINT      PRIMARY KEY,
    bonus_claimed BOOLEAN     NOT NULL DEFAULT TRUE,
    reward        INTEGER     NOT NULL DEFAULT 200,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.referrals (
    referred_id BIGINT      PRIMARY KEY,
    referrer_id BIGINT      NOT NULL,
    reward      INTEGER     NOT NULL DEFAULT 0,
    status      TEXT        NOT NULL DEFAULT 'paid',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS public.stars_payments (
    id         BIGSERIAL   PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    charge_id  TEXT,
    coins      INTEGER     NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 4. Один активный роман на игрока. Триггер не даёт создать вторую пару даже
--    при двойном нажатии «Да» двумя игроками одновременно. Повторяем его здесь
--    на случай, если таблица была создана без него.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.love_single_active_relationship()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = 'active' THEN
        IF EXISTS (
            SELECT 1
            FROM public.relationships AS r
            WHERE r.status = 'active'
              AND r.id IS DISTINCT FROM NEW.id
              AND (r.user_a IN (NEW.user_a, NEW.user_b)
                   OR r.user_b IN (NEW.user_a, NEW.user_b))
        ) THEN
            RAISE EXCEPTION 'Участник уже состоит в активных отношениях'
                USING HINT = 'Сначала завершите текущие отношения командой -отны';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS love_single_active_relationship_trg ON public.relationships;
CREATE TRIGGER love_single_active_relationship_trg
    BEFORE INSERT OR UPDATE OF user_a, user_b, status ON public.relationships
    FOR EACH ROW
    EXECUTE FUNCTION public.love_single_active_relationship();

-- updated_at пары обновляется сам — бот и Mini App это поле только читают.
CREATE OR REPLACE FUNCTION public.relationships_touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS relationships_touch_updated_at_trg ON public.relationships;
CREATE TRIGGER relationships_touch_updated_at_trg
    BEFORE UPDATE ON public.relationships
    FOR EACH ROW
    EXECUTE FUNCTION public.relationships_touch_updated_at();

-- ---------------------------------------------------------------------------
-- 5. RLS: сервер ходит anon/сервисным ключом, поэтому на всех таблицах, к
--    которым обращаются api.py и bot.py, нужна разрешающая политика.
--    Без неё INSERT пары падает с «new row violates row-level security policy».
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'users', 'relationships', 'love_stage_rewards', 'love_cooldowns',
        'solo_love', 'solo_love_stage_rewards', 'passive_income',
        'subscriptions_history', 'referrals', 'stars_payments',
        'idempotency_keys', 'game_settings', 'promocodes', 'promocode_uses',
        'users_inventory', 'shop_purchases', 'capsule_stock', 'chest_claims',
        'crash_history', 'upgrade_log', 'bans'
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

-- ---------------------------------------------------------------------------
-- 6. Кэш схемы PostgREST. Без этого Supabase ещё несколько минут отвечает
--    PGRST204 «column not found», даже когда колонка уже создана.
-- ---------------------------------------------------------------------------
NOTIFY pgrst, 'reload schema';

-- ---------------------------------------------------------------------------
-- 7. Самопроверка. Должно вернуть по одной строке на каждую таблицу с ok.
-- ---------------------------------------------------------------------------
SELECT 'relationships' AS table_name,
       (SELECT count(*) FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'relationships')::text
       || ' колонок' AS state
UNION ALL
SELECT 'solo_love',
       (SELECT count(*) FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'solo_love')::text
       || ' колонок'
UNION ALL
SELECT 'policies',
       (SELECT count(*)::text FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'relationships')
       || ' политик';
