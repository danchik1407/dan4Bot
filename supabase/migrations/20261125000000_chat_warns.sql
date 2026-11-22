-- ============================================================================
-- AURA GAME: задача 44 — «!аурапред» (предупреждения) и «!аурапредлист».
--
-- Зачем файл: предупреждения считаются ОТДЕЛЬНО В КАЖДОМ ЧАТЕ, у каждого чата
-- свой лист. В базе храним по строке на пару (чат, игрок):
--
--   warns     — сколько предупреждений набрано в этом чате (0..3);
--   muted_until — до какого момента игрок в игноре в ЭТОМ чате.
--
-- Схема поведения (её же держит bot.py):
--   • 1-е предупреждение — «предупреждение 1 из 3»;
--   • 3-е подряд — игнор на 5 часов ТОЛЬКО в этом чате, счётчик сбрасывается в 0;
--   • пока игнор активен, бот не реагирует ни на одну твою команду в этом чате,
--     а отвечает «вы наказаны в течение (время)».
--
-- Файл ИДЕМПОТЕНТНЫЙ: применять можно повторно.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- ============================================================================

create table if not exists public.chat_warns (
    chat_id     bigint      not null,
    user_id     bigint      not null,
    warns       integer     not null default 0,
    muted_until timestamptz,
    updated_at  timestamptz not null default now(),
    primary key (chat_id, user_id)
);

alter table public.chat_warns enable row level security;

-- Строки не содержат личных данных: только счётчики модерации.
-- Политику открываем целиком, чтобы и бот, и обычный ключ api.py могли писать.
drop policy if exists chat_warns_all on public.chat_warns;
create policy chat_warns_all on public.chat_warns
    for all using (true) with check (true);

create index if not exists chat_warns_chat_idx on public.chat_warns (chat_id);
