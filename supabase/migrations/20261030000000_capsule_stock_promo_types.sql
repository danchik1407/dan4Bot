-- Лимитированные капсулы, промокоды с любым типом награды и отложенная выдача сундуков.

-- 1) Глобальный тираж капсул (как у NFT): одна строка на предмет, общая на всех игроков.
CREATE TABLE IF NOT EXISTS public.capsule_stock (
    item_id    TEXT PRIMARY KEY,
    stock      INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    total      INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Тираж на момент запуска. Остаток существующих строк НЕ перезаписываем:
-- повторный запуск миграции не должен возвращать проданные капсулы.
INSERT INTO public.capsule_stock (item_id, stock, total) VALUES
    ('capsule',      1000, 1000),
    ('uniq_capsule',  500,  500),
    ('ruby',          100,  100),
    ('diamond',        50,   50),
    ('galactic',       10,   10)
ON CONFLICT (item_id) DO NOTHING;

-- 2) Промокоды: награда «коины» или «капсула».
ALTER TABLE public.promocodes
    ADD COLUMN IF NOT EXISTS reward_type TEXT NOT NULL DEFAULT 'coins';
ALTER TABLE public.promocodes
    ADD COLUMN IF NOT EXISTS item_id TEXT;
ALTER TABLE public.promocodes
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Старые промокоды были только на коины.
UPDATE public.promocodes
SET reward_type = 'coins'
WHERE reward_type IS NULL OR btrim(reward_type) = '';

UPDATE public.promocodes
SET created_at = now()
WHERE created_at IS NULL;

-- Один и тот же промокод нельзя активировать дважды одним игроком.
CREATE INDEX IF NOT EXISTS promocode_uses_code_user_idx
    ON public.promocode_uses (code, user_id);

ALTER TABLE public.promocode_uses
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- 3) Сундуки: награда выдаётся только по кнопке «Забрать» в Mini App.
--    /api/open_chest создаёт строку, /api/claim_chest начисляет и закрывает её.
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

-- Держим таблицу маленькой: старые закрытые выдачи не нужны.
DELETE FROM public.chest_claims
WHERE claimed = TRUE
  AND created_at < now() - INTERVAL '30 days';

NOTIFY pgrst, 'reload schema';
