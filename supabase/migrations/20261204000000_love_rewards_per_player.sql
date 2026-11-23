-- ############################################################################
-- ЗАДАЧА 83: «наград нету / кто раньше получал — больше не получает».
--
-- Было: отметка «уровень оплачен» стояла на ПАРУ — (relationship_id, stage) в
-- love_stage_rewards. Из этого не видно, дошло ли деньги до КАЖДОГО партнёра:
--   • если одному начисление сорвалось, второй всё равно записан как
--     получивший, и первый не получал приз уже никогда;
--   • после «пара → развод → снова пара» игрок начинал с нулём Love и призы
--     за пройденные уровни не получал («кто раньше получал — не получает»).
--
-- Стало: своя отметка на КАЖДОГО игрока в КАЖДОЙ паре — love_stage_members
-- (relationship_id, user_id, stage). Код (love.claim_stage_rewards) пишет
-- отметку до начисления и снимает её, если коины не легли в базу, поэтому
-- награда догоняет игрока при следующем действии, а дважды не выдаётся.
--
-- Миграция безопасна для повторного запуска. Старую таблицу не трогаем —
-- она остаётся для совместимости и как история.
-- ############################################################################

create table if not exists public.love_stage_members (
    relationship_id bigint      not null,
    user_id         bigint      not null,
    stage           integer     not null,
    paid_at         timestamptz not null default now(),
    primary key (relationship_id, user_id, stage)
);

-- Отметки, которые уже есть в старой таблице (пара + этап), раскладываем на
-- обоих участников пары: до этой миграции пара считалась оплаченной целиком,
-- значит повторно платить за эти уровни нельзя — иначе бот выдал бы ВСЁ заново.
insert into public.love_stage_members (relationship_id, user_id, stage)
select r.id, u.uid, s.stage
  from public.love_stage_rewards s
  join public.relationships r on r.id = s.relationship_id
 cross join lateral (values (r.user_a), (r.user_b)) as u(uid)
 where u.uid is not null
on conflict (relationship_id, user_id, stage) do nothing;

-- Индексы: чтение всегда «этот игрок в этой паре».
create index if not exists love_stage_members_user_idx
    on public.love_stage_members (user_id);

-- Права: сервисный ключ ключ обходит RLS, но если в проекте включён RLS —
-- без политики код получит «42501 row-level security policy».
do $$
begin
    if to_regclass('public.love_stage_members') is not null then
        execute 'alter table public.love_stage_members enable row level security';
        execute 'drop policy if exists love_stage_members_service_all on public.love_stage_members';
        execute 'create policy love_stage_members_service_all on public.love_stage_members '
                'for all using (true) with check (true)';
    end if;
exception when others then
    raise notice 'love_stage_members: политику создать не удалось: %', sqlerrm;
end $$;

grant all on public.love_stage_members to service_role;

-- Проверка (должно вернуть 1):
--   select count(*) from information_schema.tables
--    where table_schema = 'public' and table_name = 'love_stage_members';
