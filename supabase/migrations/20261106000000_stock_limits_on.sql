-- ============================================================================
-- AURA GAME: ВСЕ КАПСУЛЫ СНОВА ЛИМИТИРОВАННЫЕ (тираж работает «под ключ»)
--
-- Задача 19. В коде уже стоит STOCK_LIMITS_ENABLED = True (shop_catalog.py),
-- но счётчик «Осталось: X из Y» двигается только если в базе есть:
--   1) таблица capsule_stock со строкой на КАЖДЫЙ предмет каталога;
--   2) функции shop_take_stock / shop_return_stock / shop_stock_list;
--   3) права + политика RLS, иначе UPDATE внутри функции режет RLS и остаток
--      «замирает» на полном тираже (ровно тот баг, из-за которого покупка
--      отвечала «Товар распродан» при «1 000 из 1 000» на карточке).
--
-- Этот файл делает всё сразу и его можно запускать повторно:
--   • создаёт таблицу и добивает недостающие строки тиража;
--   • приводит total к тиражам каталога и НЕ возвращает проданное
--     (stock не увеличивается, только обрезается до total);
--   • пересоздаёт функции с SECURITY DEFINER (работают при включённом RLS);
--   • убирает старую одноаргументную версию shop_take_stock, чтобы PostgREST
--     не путался в перегрузках;
--   • выдаёт гранты и политику RLS;
--   • в конце печатает таблицу остатков и обновляет кэш схемы PostgREST.
--
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- После запуска в конце должно быть 5 строк: capsule / diamond / galactic /
-- ruby / uniq_capsule с ненулевым stock.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Таблица тиража
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.capsule_stock (
    item_id    TEXT PRIMARY KEY,
    stock      INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    total      INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- На всякий случай: если таблица была создана раньше без колонок total/updated_at.
ALTER TABLE public.capsule_stock ADD COLUMN IF NOT EXISTS total      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.capsule_stock ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- ---------------------------------------------------------------------------
-- 2. Тираж каталога (значения обязаны совпадать с SHOP_ITEMS в shop_catalog.py)
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _cat_stock (item_id TEXT PRIMARY KEY, total INTEGER NOT NULL) ON COMMIT DROP;

INSERT INTO _cat_stock (item_id, total) VALUES
    ('capsule',      1000),   -- Обычная капсула
    ('uniq_capsule',  500),   -- Уникальная капсула
    ('ruby',          100),   -- Рубиновая капсула (админ)
    ('diamond',        50),   -- Алмазная капсула (админ)
    ('galactic',       10);   -- Галактическая капсула (админ)

-- Недостающие строки: stock = полный тираж.
INSERT INTO public.capsule_stock (item_id, stock, total)
SELECT c.item_id, c.total, c.total FROM _cat_stock c
ON CONFLICT (item_id) DO NOTHING;

-- Существующие строки: total приводим к каталогу, stock не трогаем вверх,
-- только обрезаем, если он оказался больше тиража (иначе shop_return_stock
-- с LEAST(total, ...) не вернул бы единицу тиража).
UPDATE public.capsule_stock s
   SET total = c.total,
       stock = LEAST(s.stock, c.total),
       updated_at = now()
  FROM _cat_stock c
 WHERE s.item_id = c.item_id
   AND (s.total <> c.total OR s.stock > c.total);

-- ---------------------------------------------------------------------------
-- 3. Функции (SECURITY DEFINER — работают даже при включённом RLS)
--    Старую одноаргументную версию удаляем: PostgREST путается в перегрузках.
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.shop_take_stock(text);
DROP FUNCTION IF EXISTS public.shop_return_stock(text);

CREATE OR REPLACE FUNCTION public.shop_take_stock(p_item_id text, p_amount integer DEFAULT 1)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_amount integer := GREATEST(COALESCE(p_amount, 1), 1);
    v_left   integer;
BEGIN
    IF to_regclass('public.capsule_stock') IS NULL THEN
        RETURN -2;                       -- таблицы нет
    END IF;

    -- Строку «на лету» не создаём: без тиража получилась бы строка 0/0 и предмет
    -- навсегда остался бы распроданным. Недостающие строки создаёт приложение
    -- (ensure_stock_rows) или этот файл — с правильным total из каталога.
    UPDATE public.capsule_stock
       SET stock = stock - v_amount,
           updated_at = now()
     WHERE item_id = p_item_id
       AND stock >= v_amount
    RETURNING stock INTO v_left;

    IF v_left IS NULL THEN
        RETURN -1;                       -- распродано (или строки нет)
    END IF;
    RETURN v_left;                       -- 0 — продан последний экземпляр
END $$;

CREATE OR REPLACE FUNCTION public.shop_return_stock(p_item_id text, p_amount integer DEFAULT 1)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_amount integer := GREATEST(COALESCE(p_amount, 1), 1);
    v_left   integer;
BEGIN
    IF to_regclass('public.capsule_stock') IS NULL THEN
        RETURN -2;
    END IF;

    UPDATE public.capsule_stock
       SET stock = LEAST(GREATEST(total, 0), stock + v_amount),
           updated_at = now()
     WHERE item_id = p_item_id
    RETURNING stock INTO v_left;

    RETURN COALESCE(v_left, -1);
END $$;

CREATE OR REPLACE FUNCTION public.shop_stock_list()
RETURNS TABLE (item_id text, stock integer, total integer)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT s.item_id, s.stock, s.total FROM public.capsule_stock s ORDER BY s.item_id;
$$;

-- ---------------------------------------------------------------------------
-- 4. Права и RLS
-- ---------------------------------------------------------------------------
ALTER TABLE public.capsule_stock ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS capsule_stock_server_all ON public.capsule_stock;
CREATE POLICY capsule_stock_server_all ON public.capsule_stock
    FOR ALL TO anon, authenticated, service_role
    USING (true) WITH CHECK (true);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.capsule_stock TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;
-- Только те сигнатуры, которые точно есть: одноаргументные версии удалены выше,
-- и GRANT на них уронил бы весь скрипт.
GRANT EXECUTE ON FUNCTION public.shop_take_stock(text, integer)   TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.shop_return_stock(text, integer) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.shop_stock_list()                TO anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 5. Самопроверка
--    Ожидаем 5 строк. Если строка пропущена — значит каталог и база разошлись.
-- ---------------------------------------------------------------------------
SELECT item_id, stock, total, (total - stock) AS sold
  FROM public.capsule_stock
 ORDER BY item_id;

-- Проверка функций: ничего не списывает (item_id заведомо несуществующий),
-- должно вернуть -1 = «распродано», то есть функция жива и отвечает.
SELECT public.shop_take_stock('__probe__', 1) AS take_probe;

NOTIFY pgrst, 'reload schema';
