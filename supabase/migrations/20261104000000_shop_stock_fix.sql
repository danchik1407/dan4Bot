-- ============================================================================
-- AURA GAME: лечение магазина «Осталось: 1 000 из 1 000», а покупка → «Товар распродан»
--
-- Причина: таблица capsule_stock существует, но строк для предметов НЕТ
-- (сид не применяли / строки удалили), либо UPDATE внутри функции режет RLS.
-- Функция shop_take_stock не находит строку и возвращает «распродано»,
-- а каталог без строки показывал полный тираж — отсюда противоречие на экране.
--
-- Этот файл лечит всё сразу и его можно применять повторно:
--   1) создаёт таблицу и добивает недостающие строки тиража;
--   2) пересоздаёт функции с SECURITY DEFINER (работают при включённом RLS);
--   3) выдаёт права и политику RLS на capsule_stock;
--   4) обновляет кэш схемы PostgREST.
--
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- Файл самодостаточен: создаёт таблицу, функции и права, если их ещё нет.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Таблица тиража и строки предметов
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.capsule_stock (
    item_id    TEXT PRIMARY KEY,
    stock      INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    total      INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Сид. ON CONFLICT DO NOTHING: существующие остатки не перезаписываем,
-- поэтому повторный запуск не «возвращает» проданные капсулы.
INSERT INTO public.capsule_stock (item_id, stock, total) VALUES
    ('capsule',      1000, 1000),
    ('uniq_capsule',  500,  500),
    ('ruby',          100,  100),
    ('diamond',        50,   50),
    ('galactic',       10,   10)
ON CONFLICT (item_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. Функции (SECURITY DEFINER — работают даже при включённом RLS)
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.shop_take_stock(text);
DROP FUNCTION IF EXISTS public.shop_return_stock(text);

CREATE OR REPLACE FUNCTION public.shop_take_stock(p_item_id text, p_amount integer DEFAULT 1)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_amount integer := GREATEST(COALESCE(p_amount, 1), 1);
    v_left   integer;
BEGIN
    IF to_regclass('public.capsule_stock') IS NULL THEN
        RETURN -2;                      -- таблицы нет
    END IF;

    -- Строку НЕ создаём «на лету»: без знания тиража получилась бы строка 0/0,
    -- и предмет навсегда остался бы распроданным. Недостающие строки создаёт
    -- приложение (ensure_stock_rows) — с правильным total из каталога.
    UPDATE public.capsule_stock
       SET stock = stock - v_amount,
           updated_at = now()
     WHERE item_id = p_item_id
       AND stock >= v_amount
    RETURNING stock INTO v_left;

    IF v_left IS NULL THEN
        RETURN -1;                      -- распродано
    END IF;
    RETURN v_left;                      -- сколько осталось (0 — продан последний)
END $$;

CREATE OR REPLACE FUNCTION public.shop_return_stock(p_item_id text, p_amount integer DEFAULT 1)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_amount integer := GREATEST(COALESCE(p_amount, 1), 1);
    v_left   integer;
BEGIN
    IF to_regclass('public.capsule_stock') IS NULL THEN
        RETURN -2;
    END IF;

    UPDATE public.capsule_stock
       SET stock = LEAST(total, stock + v_amount),
           updated_at = now()
     WHERE item_id = p_item_id
       AND stock < total
    RETURNING stock INTO v_left;

    RETURN COALESCE(v_left, -1);
END $$;

CREATE OR REPLACE FUNCTION public.shop_stock_list()
RETURNS TABLE (item_id text, stock integer, total integer)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT s.item_id, s.stock, s.total FROM public.capsule_stock s ORDER BY s.item_id;
$$;

-- ---------------------------------------------------------------------------
-- 3. Права и RLS
-- ---------------------------------------------------------------------------
ALTER TABLE public.capsule_stock ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS capsule_stock_server_all ON public.capsule_stock;
CREATE POLICY capsule_stock_server_all ON public.capsule_stock
    FOR ALL TO anon, authenticated, service_role
    USING (true) WITH CHECK (true);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.capsule_stock TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.shop_take_stock(text, integer)   TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.shop_return_stock(text, integer) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.shop_stock_list()                TO anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 4. Самопроверка: должно вернуть 5 строк с остатками
-- ---------------------------------------------------------------------------
SELECT item_id, stock, total FROM public.capsule_stock ORDER BY item_id;

NOTIFY pgrst, 'reload schema';
