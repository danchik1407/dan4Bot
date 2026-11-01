-- ============================================================================
-- AURA GAME: политики RLS для серверных таблиц
--
-- Симптом, который это лечит (реальный случай из бота):
--   ❌ Не удалось создать промокод: {'message': 'new row violates row-level
--      security policy for table "promocodes"', 'code': '42501'}
--
-- Причина: в таблице включён Row Level Security, а политик нет. Бот (dan4Bot)
-- и Mini App API (aura-api) работают ключом Supabase из переменных окружения
-- Render — то есть ролью anon, для которой политик не было.
--
-- Вариант 1 (рекомендуется, без правки базы): задать в Render
--   SUPABASE_SERVICE_KEY = <service_role key из Supabase → Settings → API>
--   для сервисов aura-api и dan4Bot. Service-role обходит RLS, код уже
--   поддерживает этот ключ (см. bot.py / api.py).
--
-- Вариант 2 (эта миграция): разрешить серверу писать явными политиками.
--   Ключ anon лежит только в переменных окружения Render и в Mini App не
--   попадает — поэтому доступ «для anon» здесь ограничен доверенным сервером.
--
-- Миграция идемпотентна: можно применять повторно.
-- ============================================================================

DO $$
DECLARE
    t       text;
    tables  text[] := ARRAY[
        'promocodes',       -- создание/удаление/списание активаций
        'promocode_uses',   -- отметка «этот игрок уже активировал»
        'chest_claims',     -- награда сундука до кнопки «Забрать»
        'capsule_stock',    -- глобальный тираж капсул
        'upgrade_log',      -- честная история апгрейдов
        'crash_history',    -- история раундов «Ракеты»
        'bans'              -- баны игроков
    ];
BEGIN
    FOREACH t IN ARRAY tables
    LOOP
        -- Таблицы может ещё не быть (старая база) — просто пропускаем.
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
    END LOOP;
END $$;

-- PostgREST кэширует схему: без этого запросы ещё какое-то время будут падать.
NOTIFY pgrst, 'reload schema';
