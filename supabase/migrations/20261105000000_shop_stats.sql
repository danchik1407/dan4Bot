-- ============================================================================
-- AURA GAME: история покупок магазина + статистика «сколько каких капсул купили»
--
-- Зачем: команда «стата бота» должна показывать, сколько капсул каждого вида
-- купили игроки. Из users.inventory этого не узнать — капсулы тратятся в
-- апгрейде, поэтому пишем каждую покупку отдельной строкой.
--
-- Заодно лимиты магазина отключены (задача 17): тираж больше не списывается,
-- и capsule_stock не участвует в продаже. Таблица остаётся — её можно вернуть
-- флагом STOCK_LIMITS_ENABLED в shop_catalog.py.
--
-- Миграция идемпотентна: применяйте повторно сколько нужно.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.shop_purchases (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    price      INTEGER NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT 'mini',   -- 'mini' | 'bot'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS shop_purchases_item_idx
    ON public.shop_purchases (item_id);
CREATE INDEX IF NOT EXISTS shop_purchases_created_idx
    ON public.shop_purchases (created_at);
CREATE INDEX IF NOT EXISTS shop_purchases_user_idx
    ON public.shop_purchases (user_id);

-- RLS: сервер (aura-api и dan4Bot) ходит ключом anon из переменных окружения
-- Render, поэтому политика нужна такая же, как у остальных серверных таблиц.
ALTER TABLE public.shop_purchases ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS shop_purchases_server_all ON public.shop_purchases;
CREATE POLICY shop_purchases_server_all ON public.shop_purchases
    FOR ALL TO anon, authenticated, service_role
    USING (true) WITH CHECK (true);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.shop_purchases TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON SEQUENCE public.shop_purchases_id_seq TO anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- Агрегат для «стата бота»: item_id → сколько купили и на сколько коинов.
-- SECURITY DEFINER — чтобы работало и при включённом RLS.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.shop_purchase_stats()
RETURNS TABLE (item_id text, bought bigint, coins bigint)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT p.item_id,
           COUNT(*)::bigint                  AS bought,
           COALESCE(SUM(p.price), 0)::bigint AS coins
      FROM public.shop_purchases p
     GROUP BY p.item_id
     ORDER BY COUNT(*) DESC;
$$;

GRANT EXECUTE ON FUNCTION public.shop_purchase_stats() TO anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- Самопроверка: сколько чего куплено прямо сейчас
-- ---------------------------------------------------------------------------
SELECT * FROM public.shop_purchase_stats();

NOTIFY pgrst, 'reload schema';
