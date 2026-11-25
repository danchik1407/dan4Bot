-- Задача 90: снежинки — чистая декорация (заказчик: «берёшь и собираешь снежинки,
-- игрокам будет интересно потом показывать, кто сколько насобирал»).
--
-- Правила от заказчика:
--   * нельзя передавать другим игрокам,
--   * нельзя ничего за них покупать,
--   * нельзя обменивать.
-- Поэтому здесь ТОЛЬКО счётчик на игрока: ни истории переводов, ни списаний.
-- Счётчик растёт исключительно через API ловли (/api/snowflakes/catch).
--
-- Миграция идемпотентная: можно запускать повторно, ничего не сломает.
-- Если не применить — игра работает, снежинки просто не сохраняются.

create table if not exists public.snowflakes (
    user_id     bigint primary key,
    count       integer not null default 0,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- Топ «кто сколько насобирал» читается отсортированным по count.
create index if not exists snowflakes_count_idx
    on public.snowflakes (count desc);

-- Служебная функция health: приложение спрашивает, готова ли таблица, чтобы
-- честно показать «Снежинки: 0», а не молчать. Ошибки не бросает.
create or replace function public.snowflakes_health()
returns jsonb
language sql
security definer
set search_path = public
as $$
    select jsonb_build_object(
        'ok', to_regclass('public.snowflakes') is not null,
        'version', '20261208000000'
    );
$$;

grant execute on function public.snowflakes_health() to anon, authenticated, service_role;

-- Права: приложение ходит сервисным ключом, но и anon/authenticated не мешают.
grant select, insert, update on public.snowflakes to anon, authenticated, service_role;

notify pgrst, 'reload schema';
