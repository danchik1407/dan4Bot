-- ============================================================================
-- AURA GAME: ОБЫЧНАЯ КАПСУЛА ИЗ СУНДУКОВ (задача 22)
--
-- Шансы (считаются на сервере, api.py):
--   • золотой сундук — 10 %  на 💊 обычную капсулу;
--   • обычный сундук — 0.1 % на 💊 обычную капсулу.
-- Капсула выпадает В ДОБАВОК к коинам/ключу и начисляется только по кнопке
-- «Забрать» — как и остальная награда сундука.
--
-- Что делает файл:
--   1) добавляет в chest_claims колонки reward_item / reward_item_amount;
--   2) пересоздаёт функции chest_claim_create / chest_claim_pending /
--      chest_claim_take так, чтобы капсула хранилась и возвращалась вместе
--      с коинами и ключами.
--
-- ВАЖНО: без этого файла игра НЕ ломается. Сервер держит капсулу в памяти
-- процесса до кнопки «Забрать», но при перезапуске Render (или если игрок
-- закроет аппку) такая награда пропадёт. Поэтому миграцию лучше применить.
--
-- Файл идемпотентный: запускай повторно сколько нужно.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Колонки капсулы
-- ---------------------------------------------------------------------------
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

ALTER TABLE public.chest_claims ADD COLUMN IF NOT EXISTS reward_item        TEXT    NOT NULL DEFAULT '';
ALTER TABLE public.chest_claims ADD COLUMN IF NOT EXISTS reward_item_amount INTEGER NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
-- 2. Функции сундуков
--    chest_claim_create получает параметры капсулы (с значениями по умолчанию,
--    чтобы старый вызов из четырёх аргументов продолжал работать).
--    Старую версию удаляем: иначе PostgREST запутается в перегрузках.
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.chest_claim_create(text, text, integer, integer);
DROP FUNCTION IF EXISTS public.chest_claim_create(text, text, integer, integer, text, integer);
DROP FUNCTION IF EXISTS public.chest_claim_pending(text);
DROP FUNCTION IF EXISTS public.chest_claim_take(bigint, text);

CREATE OR REPLACE FUNCTION public.chest_claim_create(
    p_user_id         text,
    p_chest_type      text DEFAULT 'normal',
    p_coins           integer DEFAULT 0,
    p_keys            integer DEFAULT 0,
    p_item            text DEFAULT '',
    p_item_amount     integer DEFAULT 0
)
RETURNS TABLE (claim_id bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_item        text    := COALESCE(btrim(p_item), '');
    v_item_amount integer := GREATEST(COALESCE(p_item_amount, 0), 0);
BEGIN
    RETURN QUERY
    INSERT INTO public.chest_claims
        (user_id, chest_type, reward_coins, reward_keys, reward_item, reward_item_amount, claimed)
    VALUES (p_user_id,
            COALESCE(NULLIF(btrim(p_chest_type), ''), 'normal'),
            GREATEST(COALESCE(p_coins, 0), 0),
            GREATEST(COALESCE(p_keys, 0), 0),
            v_item,
            CASE WHEN v_item = '' THEN 0 ELSE v_item_amount END,
            false)
    RETURNING chest_claims.id;
END $$;

-- Незабранные награды игрока (их зачисляем перед новым сундуком).
CREATE OR REPLACE FUNCTION public.chest_claim_pending(p_user_id text)
RETURNS TABLE (id bigint, reward_coins integer, reward_keys integer,
               reward_item text, reward_item_amount integer)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT c.id, c.reward_coins, c.reward_keys, c.reward_item, c.reward_item_amount
      FROM public.chest_claims c
     WHERE c.user_id = p_user_id
       AND c.claimed = false
     ORDER BY c.id;
$$;

-- Атомарно «занять» награду: already_claimed = true, если её уже забрали.
-- Пустой результат — такого сундука у игрока нет.
CREATE OR REPLACE FUNCTION public.chest_claim_take(p_claim_id bigint, p_user_id text)
RETURNS TABLE (reward_coins integer, reward_keys integer,
               reward_item text, reward_item_amount integer, already_claimed boolean)
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
        RETURN QUERY SELECT v_row.reward_coins, v_row.reward_keys,
                            v_row.reward_item, v_row.reward_item_amount, false;
        RETURN;
    END IF;

    SELECT c.* INTO v_row
      FROM public.chest_claims c
     WHERE c.id = p_claim_id AND c.user_id = p_user_id;

    IF v_row.id IS NULL THEN
        RETURN;   -- сундук не найден
    END IF;
    RETURN QUERY SELECT 0, 0, ''::text, 0, true;
END $$;

-- ---------------------------------------------------------------------------
-- 3. Права и RLS
-- ---------------------------------------------------------------------------
ALTER TABLE public.chest_claims ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS chest_claims_server_all ON public.chest_claims;
CREATE POLICY chest_claims_server_all ON public.chest_claims
    FOR ALL TO anon, authenticated, service_role
    USING (true) WITH CHECK (true);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.chest_claims TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON SEQUENCE public.chest_claims_id_seq TO anon, authenticated, service_role;

GRANT EXECUTE ON FUNCTION public.chest_claim_create(text, text, integer, integer, text, integer)
    TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.chest_claim_pending(text)           TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.chest_claim_take(bigint, text)      TO anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 4. Самопроверка
-- ---------------------------------------------------------------------------
-- Колонки на месте:
SELECT column_name, data_type, column_default
  FROM information_schema.columns
 WHERE table_schema = 'public'
   AND table_name = 'chest_claims'
   AND column_name IN ('reward_item', 'reward_item_amount');

-- Функция отвечает на 6 аргументов (создаём и сразу удаляем пробную выдачу):
DO $$
DECLARE
    v_id bigint;
BEGIN
    SELECT claim_id INTO v_id
      FROM public.chest_claim_create('__probe__', 'gold', 0, 0, 'capsule', 1);
    RAISE NOTICE 'проверка chest_claim_create: claim_id=%', v_id;
    DELETE FROM public.chest_claims WHERE user_id = '__probe__';
END $$;

NOTIFY pgrst, 'reload schema';
