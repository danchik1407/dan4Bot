-- ============================================================================
-- AURA GAME: ЭКОНОМИКА И СОЦИАЛКА (задача 25)
--
-- Один файл на все новые механики:
--   1) subscriptions_history — бонус 200 коинов за подписку на канал (один раз);
--   2) passive_income        — пассивный доход с капсул (0.1/0.5/5/20/50 в час);
--   3) referrals             — реферальная программа (500/800/2000 коинов);
--   4) stars_payments        — оплата Telegram Stars (идемпотентно по charge_id);
--   5) idempotency_keys      — защита финансовых операций от повторной отправки;
--   6) game_settings         — админ меняет шансы апгрейда без деплоя;
--   7) relationships         — отношения пар (+ love_stage_rewards, love_cooldowns).
--
-- Файл идемпотентный: можно применять повторно сколько угодно раз.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- После применения обязательно выполните:
--     NOTIFY pgrst, 'reload schema';
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Подписка на канал: user_id — первичный ключ, поэтому бонус физически
--    невозможно начислить второй раз (отписался и подписался снова — нет).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.subscriptions_history (
    user_id       BIGINT PRIMARY KEY,
    bonus_claimed BOOLEAN     NOT NULL DEFAULT TRUE,
    reward        INTEGER     NOT NULL DEFAULT 200,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 2. Пассивный доход: когда последний раз начисляли (по факту инвентаря).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.passive_income (
    user_id  BIGINT PRIMARY KEY,
    last_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 3. Рефералы. referred_id — первичный ключ: один игрок = один пригласивший.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.referrals (
    referred_id  BIGINT PRIMARY KEY,
    referrer_id  BIGINT      NOT NULL,
    reward       INTEGER     NOT NULL DEFAULT 0,
    status       TEXT        NOT NULL DEFAULT 'pending',   -- pending | paid
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS referrals_referrer_idx ON public.referrals (referrer_id);

-- ---------------------------------------------------------------------------
-- 4. Telegram Stars: charge_id от Telegram — ключ идемпотентности.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.stars_payments (
    charge_id  TEXT PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    coins      INTEGER     NOT NULL DEFAULT 0,
    stars      INTEGER     NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 5. Идемпотентность финансовых операций.
--    Клиент присылает request_id, сервер сохраняет ответ; повтор возвращает
--    сохранённый ответ и НЕ списывает деньги второй раз.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.idempotency_keys (
    key        TEXT PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    endpoint   TEXT        NOT NULL,
    response   TEXT        NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idempotency_keys_user_idx ON public.idempotency_keys (user_id);

-- ---------------------------------------------------------------------------
-- 6. Настройки игры (шансы апгрейда и прочее) — читаются API с кэшем 30 с.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.game_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.game_settings (key, value) VALUES
    ('upgrade_chance_factor', '1.0'),   -- множитель шанса апгрейда (0.5 = вдвое строже)
    ('upgrade_house_edge',    '0.8'),   -- дом-эдж на балансе от 1000 коинов
    ('crash_house_edge',      '1.0')
ON CONFLICT (key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 7. Отношения.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.relationships (
    id         BIGSERIAL PRIMARY KEY,
    user_a     BIGINT      NOT NULL,
    user_b     BIGINT      NOT NULL,
    love       INTEGER     NOT NULL DEFAULT 0,      -- общий банк Love коинов
    bank       INTEGER     NOT NULL DEFAULT 0,      -- потраченный/накопленный банк пары
    status     TEXT        NOT NULL DEFAULT 'active',   -- active | broken
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS relationships_user_a_idx ON public.relationships (user_a);
CREATE INDEX IF NOT EXISTS relationships_user_b_idx ON public.relationships (user_b);

-- Какие этапы уже оплачены (награда каждому партнёру выдаётся один раз).
CREATE TABLE IF NOT EXISTS public.love_stage_rewards (
    relationship_id BIGINT NOT NULL,
    stage           INTEGER NOT NULL,
    paid_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (relationship_id, stage)
);

-- Кулдаун РП-действий: +10 Love коинов не чаще, чем раз в 5 часов.
-- (Сами действия в чате не ограничены — ограничена только награда.)
CREATE TABLE IF NOT EXISTS public.love_cooldowns (
    user_id    BIGINT PRIMARY KEY,
    action_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- i¢ (донат-валюта) и premium-статус для реферальных наград.
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS ice     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS premium BOOLEAN NOT NULL DEFAULT FALSE;

-- ---------------------------------------------------------------------------
-- 8. Права: сервисный ключ обходит RLS, но если в проекте RLS включён для
--    anon-ключа — эти политики не дадут «42501 row-level security policy».
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'subscriptions_history', 'passive_income', 'referrals', 'stars_payments',
        'idempotency_keys', 'game_settings', 'relationships',
        'love_stage_rewards', 'love_cooldowns'
    ] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_service_all', tbl);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL USING (true) WITH CHECK (true)',
            tbl || '_service_all', tbl
        );
    END LOOP;
END $$;

NOTIFY pgrst, 'reload schema';
