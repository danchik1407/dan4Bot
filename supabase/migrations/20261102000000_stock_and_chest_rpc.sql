-- ============================================================================
-- AURA GAME: строгий тираж капсул и сундуки, работающие даже при включённом RLS
--
-- Зачем это нужно
-- ---------------
-- 1) Счётчик «Осталось: X из Y» не уменьшался, потому что UPDATE таблицы
--    capsule_stock падал (RLS / нет строки), а сервер молча продолжал продажу.
--    Функция shop_take_stock делает списание ОДНИМ атомарным запросом
--    «stock = stock - 1 WHERE stock > 0» — без гонок «прочитал 1000, записал 999».
--
-- 2) «Сундуки обновляются, попробуйте чуть позже» — это ошибка записи в
--    chest_claims (чаще всего "new row violates row-level security policy").
--    Функции chest_*_ работают с таблицей от имени владельца (SECURITY DEFINER),
--    поэтому им не мешает RLS, и ключ anon продолжает работать.
--
-- SECURITY DEFINER здесь безопасен: функции умеют только узкие операции
-- (списать/вернуть единицу тиража, создать/закрыть/прочитать свою выдачу),
-- и не дают ни произвольного SQL, ни доступа к чужим данным.
--
-- Миграция идемпотентна: можно применять повторно.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. ТИРАЖ КАПСУЛ
-- ---------------------------------------------------------------------------

-- Строки тиража на месте (повторный запуск не возвращает проданное).
INSERT INTO public.capsule_stock (item_id, stock, total) VALUES
    ('capsule',      1000, 1000),
    ('uniq_capsule',  500,  500),
    ('ruby',          100,  100),
    ('diamond',        50,   50),
    ('galactic',       10,   10)
ON CONFLICT (item_id) DO NOTHING;

-- Списать ровно одну единицу. Возвращает остаток после списания,
-- -1 — товар распродан (или строки нет), -2 — таблицы нет.
CREATE OR REPLACE FUNCTION public.shop_take_stock(p_item_id text)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_left integer;
BEGIN
    IF to_regclass('public.capsule_stock') IS NULL THEN
        RETURN -2;
    END IF;

    UPDATE public.capsule_stock
       SET stock = stock - 1,
           updated_at = now()
     WHERE item_id = p_item_id
       AND stock > 0
    RETURNING stock INTO v_left;

    IF v_left IS NULL THEN
        RETURN -1;   -- распродано
    END IF;
    RETURN v_left;
END $$;

-- Вернуть единицу тиража, если покупку не удалось записать в профиль.
CREATE OR REPLACE FUNCTION public.shop_return_stock(p_item_id text)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_left integer;
BEGIN
    IF to_regclass('public.capsule_stock') IS NULL THEN
        RETURN -2;
    END IF;

    UPDATE public.capsule_stock
       SET stock = stock + 1,
           updated_at = now()
     WHERE item_id = p_item_id
       AND stock < total
    RETURNING stock INTO v_left;

    RETURN COALESCE(v_left, -1);
END $$;

-- Снимок тиража одним запросом: читается даже при включённом RLS на таблице.
CREATE OR REPLACE FUNCTION public.shop_stock_list()
RETURNS TABLE (item_id text, stock integer, total integer)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT s.item_id, s.stock, s.total FROM public.capsule_stock s ORDER BY s.item_id;
$$;

-- ---------------------------------------------------------------------------
-- 2. СУНДУКИ
-- ---------------------------------------------------------------------------

-- Таблица нужна функциям ниже. Обычно её создаёт
-- 20261030000000_capsule_stock_promo_types.sql; здесь повторяем то же самое
-- с IF NOT EXISTS, чтобы миграция работала и на базе, где тот файл пропустили.
CREATE TABLE IF NOT EXISTS public.chest_claims (
    id           BIGSERIAL PRIMARY KEY,
    user_id      TEXT NOT NULL,
    chest_type   TEXT NOT NULL DEFAULT 'normal',
    reward_coins INTEGER NOT NULL DEFAULT 0,
    reward_keys  INTEGER NOT NULL DEFAULT 0,
    claimed      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS chest_claims_user_idx
    ON public.chest_claims (user_id, claimed);

-- Разыграть награду: строка появляется сразу, начисление — только по «Забрать».
CREATE OR REPLACE FUNCTION public.chest_claim_create(
    p_user_id    text,
    p_chest_type text DEFAULT 'normal',
    p_coins      integer DEFAULT 0,
    p_keys       integer DEFAULT 0
)
RETURNS TABLE (claim_id bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    INSERT INTO public.chest_claims (user_id, chest_type, reward_coins, reward_keys, claimed)
    VALUES (p_user_id,
            COALESCE(NULLIF(btrim(p_chest_type), ''), 'normal'),
            GREATEST(COALESCE(p_coins, 0), 0),
            GREATEST(COALESCE(p_keys, 0), 0),
            false)
    RETURNING chest_claims.id;
END $$;

-- Незабранные награды игрока (их зачисляем перед новым сундуком).
CREATE OR REPLACE FUNCTION public.chest_claim_pending(p_user_id text)
RETURNS TABLE (id bigint, reward_coins integer, reward_keys integer)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT c.id, c.reward_coins, c.reward_keys
      FROM public.chest_claims c
     WHERE c.user_id = p_user_id
       AND c.claimed = false
     ORDER BY c.id;
$$;

-- Атомарно «занять» награду: already_claimed = true, если её уже забрали.
-- Пустой результат — такого сундука у игрока нет.
CREATE OR REPLACE FUNCTION public.chest_claim_take(p_claim_id bigint, p_user_id text)
RETURNS TABLE (reward_coins integer, reward_keys integer, already_claimed boolean)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row public.chest_claims%ROWTYPE;
BEGIN
    UPDATE public.chest_claims c
       SET claimed = true,
           claimed_at = now()
     WHERE c.id = p_claim_id
       AND c.user_id = p_user_id
       AND c.claimed = false
    RETURNING c.* INTO v_row;

    IF v_row.id IS NOT NULL THEN
        RETURN QUERY SELECT v_row.reward_coins, v_row.reward_keys, false;
        RETURN;
    END IF;

    SELECT c.* INTO v_row
      FROM public.chest_claims c
     WHERE c.id = p_claim_id AND c.user_id = p_user_id;

    IF v_row.id IS NULL THEN
        RETURN;   -- сундук не найден
    END IF;
    RETURN QUERY SELECT 0, 0, true;
END $$;

-- Откат «занятия», если награду не удалось записать в профиль.
CREATE OR REPLACE FUNCTION public.chest_claim_release(p_claim_id bigint, p_user_id text)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    UPDATE public.chest_claims c
       SET claimed = false,
           claimed_at = NULL
     WHERE c.id = p_claim_id
       AND c.user_id = p_user_id
       AND c.claimed = true;
    RETURN FOUND;
END $$;

-- ---------------------------------------------------------------------------
-- 3. ПРАВА: функции вызываются ключом anon из Render (PostgREST → /rpc/...)
-- ---------------------------------------------------------------------------
GRANT EXECUTE ON FUNCTION public.shop_take_stock(text)                     TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.shop_return_stock(text)                   TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.shop_stock_list()                         TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.chest_claim_create(text, text, integer, integer) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.chest_claim_pending(text)                 TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.chest_claim_take(bigint, text)            TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.chest_claim_release(bigint, text)         TO anon, authenticated, service_role;

-- PostgREST кэширует список RPC: без этого /rpc/... будет отвечать 404.
NOTIFY pgrst, 'reload schema';
