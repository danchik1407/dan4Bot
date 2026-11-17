-- ============================================================================
-- AURA GAME: задача 26 — один роман на игрока, топ отношений, честный шанс
--
-- Файл идемпотентный: можно применять повторно.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- Требует уже применённой 20261109000000_economy_social.sql.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Один активный роман на игрока.
--    Проверка в боте («я уже в паре?») не спасает от гонки: двое могут нажать
--    «Да» одновременно. Триггер в базе делает это невозможным физически —
--    вторая активная пара с участием того же игрока не создастся.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.love_single_active_relationship()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = 'active' THEN
        IF EXISTS (
            SELECT 1
            FROM public.relationships AS r
            WHERE r.status = 'active'
              AND r.id IS DISTINCT FROM NEW.id
              AND (r.user_a IN (NEW.user_a, NEW.user_b)
                   OR r.user_b IN (NEW.user_a, NEW.user_b))
        ) THEN
            RAISE EXCEPTION 'Участник уже состоит в активных отношениях'
                USING HINT = 'Сначала завершите текущие отношения командой -любовь';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS love_single_active_relationship_trg ON public.relationships;
CREATE TRIGGER love_single_active_relationship_trg
    BEFORE INSERT OR UPDATE OF user_a, user_b, status ON public.relationships
    FOR EACH ROW
    EXECUTE FUNCTION public.love_single_active_relationship();

-- Быстрый поиск активной пары по игроку (профиль, плашка, `топ отношений`).
CREATE INDEX IF NOT EXISTS relationships_status_idx
    ON public.relationships (status);
CREATE INDEX IF NOT EXISTS relationships_active_users_idx
    ON public.relationships (user_a, user_b) WHERE status = 'active';

-- ---------------------------------------------------------------------------
-- 2. Шанс апгрейда: дом-эдж больше не прячется в расчёте.
--    Раньше «x1.75 → 45.71%» получалось как (1/1.75) × 0.8. Теперь шанс берётся
--    из фиксированной таблицы в api.py (x1.12 → 85%, x1.35 → 70%,
--    x1.55 → 60%, x1.75 → 50%, x20 → 4%, x50 → 1.5%), и это ровно то число,
--    которое видит игрок. Значение 0.8 в настройках обнуляем до 1.0, иначе
--    круг снова показывал бы «нелогичные» проценты.
-- ---------------------------------------------------------------------------
INSERT INTO public.game_settings (key, value)
VALUES ('upgrade_house_edge', '1.0')
ON CONFLICT (key) DO NOTHING;

UPDATE public.game_settings
   SET value = '1.0', updated_at = now()
 WHERE key = 'upgrade_house_edge'
   AND value = '0.8';

NOTIFY pgrst, 'reload schema';
