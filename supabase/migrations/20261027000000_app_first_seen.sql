-- Задача 43: «Дней в игре» считается от ПЕРВОГО ЗАХОДА В MINI APP,
-- а не от регистрации в боте (у старых игроков разница — годы).
-- Таблицу можно применять повторно: create if not exists.
create table if not exists public.app_first_seen (
    user_id bigint primary key,
    first_seen timestamptz not null default now()
);

alter table public.app_first_seen enable row level security;

-- Сервер aura-api ходит обычным ключом (service-подобным), поэтому политики
-- открываем для всех: строки не содержат ничего, кроме даты первого захода.
drop policy if exists app_first_seen_all on public.app_first_seen;
create policy app_first_seen_all on public.app_first_seen
    for all using (true) with check (true);
