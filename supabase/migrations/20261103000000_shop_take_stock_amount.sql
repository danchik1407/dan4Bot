-- ============================================================================
-- AURA GAME: shop_take_stock с количеством (p_amount)
--
-- Зачем: сервер вызывает
--     supabase.rpc("shop_take_stock", {"p_item_id": <id>, "p_amount": 1})
-- а функция из 20261102000000 принимала только p_item_id. PostgREST в таком
-- случае отвечает PGRST202 «no function matches», и списание уходило в старый
-- путь с UPDATE — из-за этого счётчик «Осталось» мог не меняться.
--
-- Здесь функция получает второй параметр с DEFAULT 1, поэтому работают ОБА
-- вызова: с p_amount и без него.
--
-- Возвращает остаток после списания:
--     >= 0 — сколько осталось (0 — продан последний экземпляр)
--       -1 — распродано (списывать нечего)
--       -2 — таблицы capsule_stock нет
--
-- Миграция идемпотентна: применяйте повторно сколько нужно.
-- ============================================================================

-- Старые сигнатуры убираем, иначе PostgREST видит две функции и путается,
-- какую из них вызывать (перегрузка по числу аргументов).
DROP FUNCTION IF EXISTS public.shop_take_stock(text);
DROP FUNCTION IF EXISTS public.shop_return_stock(text);

-- ---------------------------------------------------------------------------
-- Списать p_amount единиц тиража ОДНИМ атомарным запросом.
-- ---------------------------------------------------------------------------
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
        RETURN -2;
    END IF;

    UPDATE public.capsule_stock
       SET stock = stock - v_amount,
           updated_at = now()
     WHERE item_id = p_item_id
       AND stock >= v_amount
    RETURNING stock INTO v_left;

    IF v_left IS NULL THEN
        RETURN -1;   -- распродано (или такого предмета нет)
    END IF;
    RETURN v_left;
END $$;

-- ---------------------------------------------------------------------------
-- Вернуть p_amount единиц, если покупку не удалось записать в профиль.
-- Выше тиража (total) не поднимаем.
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- Чтение остатка одним запросом (работает и при включённом RLS на таблице).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.shop_stock_list()
RETURNS TABLE (item_id text, stock integer, total integer)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT s.item_id, s.stock, s.total FROM public.capsule_stock s ORDER BY s.item_id;
$$;

GRANT EXECUTE ON FUNCTION public.shop_take_stock(text, integer)   TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.shop_return_stock(text, integer) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.shop_stock_list()                TO anon, authenticated, service_role;

-- PostgREST кэширует список функций: без этого вызов ещё какое-то время даёт 404.
NOTIFY pgrst, 'reload schema';
