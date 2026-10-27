-- Лог апгрейдов: доказательство каждой выплаты + честная статистика + показ
-- результата, если игрок закрыл приложение до конца анимации.
CREATE TABLE IF NOT EXISTS public.upgrade_log (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    item_from TEXT NOT NULL,
    amount_from INTEGER NOT NULL DEFAULT 0,
    item_to TEXT NOT NULL,
    chance NUMERIC(6, 2) NOT NULL DEFAULT 0,   -- шанс, который видел игрок
    roll NUMERIC(6, 2) NOT NULL DEFAULT 0,     -- что выпало из 100
    won BOOLEAN NOT NULL DEFAULT FALSE,
    win_item TEXT,
    win_amount BIGINT NOT NULL DEFAULT 0,
    coins_before BIGINT NOT NULL DEFAULT 0,
    coins_after BIGINT NOT NULL DEFAULT 0,
    inventory_after JSONB NOT NULL DEFAULT '[]'::jsonb,
    seen BOOLEAN NOT NULL DEFAULT FALSE,       -- показан ли результат игроку
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS upgrade_log_user_idx
    ON public.upgrade_log (user_id, id DESC);

-- Индекс для «последний непоказанный результат».
CREATE INDEX IF NOT EXISTS upgrade_log_unseen_idx
    ON public.upgrade_log (user_id, seen)
    WHERE seen = FALSE;

NOTIFY pgrst, 'reload schema';
