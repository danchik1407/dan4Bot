-- ============================================================================
-- AURA GAME: ОТКРЫТИЕ ИГРЫ ДЛЯ ВСЕХ (задача 21)
--
-- 1) Чинит промокоды:
--      ❌ Не удалось создать промокод: {'message': 'new row violates row-level
--         security policy for table "promocodes"', 'code': '42501', ...}
--    Это включённый RLS без политики для promocodes/promocode_uses. Ниже
--    создаются политики и гранты для роли, которой работают aura-api и dan4Bot
--    (ключ из переменных окружения Render).
--
-- 2) Обновляет тиражи капсул к открытию: остаток возвращается к полному
--    тиражу (stock = total) — «после техработ лимиты обновились».
--    ВНИМАНИЕ: это обнуляет счётчик проданного по всем капсулам.
--
-- Файл идемпотентный: запускай повторно сколько нужно.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. RLS + гранты для промокодов (и соседних серверных таблиц, если есть)
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    t      text;
    tables text[] := ARRAY[
        'promocodes',       -- создание/удаление промокодов
        'promocode_uses',   -- «этот игрок уже активировал»
        'capsule_stock',    -- тираж капсул
        'shop_purchases',   -- история покупок магазина
        'chest_claims',     -- награда сундука до кнопки «Забрать»
        'upgrade_log',      -- честная история апгрейдов
        'crash_history',    -- история раундов «Ракеты»
        'bans'              -- баны игроков
    ];
BEGIN
    FOREACH t IN ARRAY tables
    LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            RAISE NOTICE 'пропуск: таблицы public.% нет', t;
            CONTINUE;
        END IF;

        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);

        -- Пересоздаём только СВОЮ политику: чужие правила не трогаем.
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', t || '_server_all', t);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO anon, authenticated, service_role '
            'USING (true) WITH CHECK (true)',
            t || '_server_all', t
        );
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I '
                       'TO anon, authenticated, service_role', t);
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Тиражи к открытию: остаток = полный тираж
--    Значения обязаны совпадать с SHOP_ITEMS в shop_catalog.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.capsule_stock (
    item_id    TEXT PRIMARY KEY,
    stock      INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    total      INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.capsule_stock ADD COLUMN IF NOT EXISTS total      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.capsule_stock ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

INSERT INTO public.capsule_stock (item_id, stock, total, updated_at)
VALUES
    ('capsule',      1000, 1000, now()),   -- Обычная капсула
    ('uniq_capsule',  500,  500, now()),   -- Уникальная капсула
    ('ruby',          100,  100, now()),   -- Рубиновая капсула
    ('diamond',        50,   50, now()),   -- Алмазная капсула
    ('galactic',       10,   10, now())    -- Галактическая капсула
ON CONFLICT (item_id) DO UPDATE
   SET stock      = EXCLUDED.stock,
       total      = EXCLUDED.total,
       updated_at = now();

-- ---------------------------------------------------------------------------
-- 3. Самопроверка
-- ---------------------------------------------------------------------------
-- Должно быть 5 строк, sold = 0.
SELECT item_id, stock, total, (total - stock) AS sold
  FROM public.capsule_stock
 ORDER BY item_id;

-- Политики промокодов должны быть видны в списке:
SELECT tablename, policyname, cmd, roles
  FROM pg_policies
 WHERE schemaname = 'public'
   AND tablename IN ('promocodes', 'promocode_uses')
 ORDER BY tablename;

-- PostgREST кэширует схему и права: без этого ещё какое-то время будет 42501.
NOTIFY pgrst, 'reload schema';
