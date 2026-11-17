-- ============================================================================
-- AURA GAME: задача 30 — одинокие (UNLOVE) + ремонт схемы
--
-- Что делает файл:
--   1) solo_love               — одиночный счёт Unlove (один игрок = одна строка);
--   2) solo_love_stage_rewards — какие этапы одиночке уже оплачены (без повторов);
--   3) passive_income          — заново создаёт таблицу, если её не было
--      (именно её отсутствие давало окно «Примените миграцию» в профиле);
--   4) шанс апгрейда возвращает к фиксированному 90 / X.
--
-- Файл идемпотентный: можно применять повторно сколько угодно раз.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 0. Пассивный доход. Если миграция 20261109000000 не применялась — создаём.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.passive_income (
    user_id  BIGINT PRIMARY KEY,
    last_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 1. Одиночный счёт (Unlove).
--    Валюта личная: зарабатывается РП-командами без пары, передать её
--    другому нельзя. При вступлении в отношения строка удаляется — счёт
--    сгорает, начинается общая история пары.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.solo_love (
    user_id    BIGINT PRIMARY KEY,
    love       INTEGER     NOT NULL DEFAULT 0 CHECK (love >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 2. Какие этапы одиночке уже оплачены.
--    PRIMARY KEY (user_id, stage) — награду за этап физически нельзя выдать
--    дважды. Строки НЕ удаляются при вступлении в отношения: иначе награды
--    можно было бы фармить по кругу «пара → развод → снова один».
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.solo_love_stage_rewards (
    user_id BIGINT      NOT NULL,
    stage   INTEGER     NOT NULL,
    paid_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, stage)
);

-- Одиночный счёт ищется по user_id — первичный ключ уже даёт индекс;
-- отдельный нужен только для статистики админки.
CREATE INDEX IF NOT EXISTS solo_love_love_idx ON public.solo_love (love DESC);

-- ---------------------------------------------------------------------------
-- 3. Шанс апгрейда — ровно 90 / X.
--    Кнопка «Шансы апгрейда» из админки убрана, множители больше не читаются.
--    Здесь же чиним базу, где остались старые значения (0.8 дом-эдж и т.п.),
--    чтобы «что видит игрок» и «по чему играем» были одним числом.
-- ---------------------------------------------------------------------------
INSERT INTO public.game_settings (key, value) VALUES
    ('upgrade_house_edge',    '1.0'),
    ('upgrade_chance_factor', '1.0')
ON CONFLICT (key) DO NOTHING;

UPDATE public.game_settings
   SET value = '1.0', updated_at = now()
 WHERE key IN ('upgrade_house_edge', 'upgrade_chance_factor')
   AND value <> '1.0';

-- ---------------------------------------------------------------------------
-- 4. Права: сервисный ключ обходит RLS, но политики нужны, если в проекте
--    включён RLS для anon — иначе «42501 row-level security policy».
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['solo_love', 'solo_love_stage_rewards', 'passive_income'] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_service_all', tbl);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL USING (true) WITH CHECK (true)',
            tbl || '_service_all', tbl
        );
    END LOOP;
END $$;

NOTIFY pgrst, 'reload schema';
