-- ============================================================================
-- AURA GAME: задача 106 (правка) — награды за этапы считаются ПО ЦИКЛУ.
--
-- Заказчик: «выдача награды при достижении следующего этапа в любовных и
-- одиночных отношениях — выдача даже если забирал раньше (типа если был раньше
-- в отношениях)».
--
-- Проблема, которую это лечит: «оплачено ли» решалось по служебным отметкам
-- (love_stage_members / solo_love_stage_rewards) и журналу. Отметка могла
-- остаться от старого сбоя, при котором деньги не дошли, — и уровень навсегда
-- считался оплаченным. Со стороны: «перешли на этап, а награды нет».
--
-- Что делает файл:
--   1) добавляет поле `rewards` (jsonb) в строку ЦИКЛА:
--        • public.relationships — у пары,
--        • public.solo_love     — у одиночки;
--      в нём список [{"user_id": 1, "stage": 2}, ...] — кто что уже получил
--      ИМЕННО в этой паре / этом одиночном цикле;
--   2) переносит туда историю по журналу love_rewards_log (журнал пишется
--      только после реального начисления коинов), чтобы после миграции никто
--      не получил выплаты повторно за текущий цикл;
--   3) чистит мусор от несуществующих пар (строки отметок/журнала без пары) —
--      деньги за них не начисляются, просто база перестаёт путаться;
--   4) код (love.claim_stage_rewards / claim_solo_stage_rewards) теперь платит
--      по этому полю: новая пара = пустое поле = награды заново; новый
--      одиночный цикл (вступление в пару / расставание) = поле сброшено.
--
-- Файл ИДЕМПОТЕНТНЫЙ: применять можно повторно.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- После применения: NOTIFY pgrst, 'reload schema';
-- ============================================================================

-- --- 1) поле цикла ---
alter table public.relationships
    add column if not exists rewards jsonb not null default '[]'::jsonb;
alter table public.solo_love
    add column if not exists rewards jsonb not null default '[]'::jsonb;

comment on column public.relationships.rewards is
    'Полученные награды за этапы ТЕКУЩЕЙ пары: [{"user_id":1,"stage":2}]. Новая пара — пусто.';
comment on column public.solo_love.rewards is
    'Полученные награды за этапы ТЕКУЩЕГО одиночного цикла: [{"stage":2}]. Сброс — при входе в пару и при расставании.';

-- --- 2) перенос истории по журналу (только реально начисленные деньги) ---
do $$
begin
    if to_regclass('public.love_rewards_log') is not null then
        execute $sql$
            update public.relationships r
               set rewards = coalesce((
                    select jsonb_agg(jsonb_build_object('user_id', l.user_id, 'stage', l.stage))
                      from public.love_rewards_log l
                     where l.relationship_id = r.id), '[]'::jsonb)
             where coalesce(r.rewards, '[]'::jsonb) = '[]'::jsonb
        $sql$;
        execute $sql$
            update public.solo_love s
               set rewards = coalesce((
                    select jsonb_agg(jsonb_build_object('stage', l.stage))
                      from public.love_rewards_log l
                     where l.relationship_id = 0
                       and l.user_id = s.user_id), '[]'::jsonb)
             where coalesce(s.rewards, '[]'::jsonb) = '[]'::jsonb
        $sql$;
    end if;
end $$;

-- --- 3) чистка мусора от пар, которых больше нет ---
-- На выплаты это не влияет (ключ — id пары), но база перестаёт копить
-- «оплаченные» уровни удалённых отношений, из-за которых раньше путались
-- проверки и отчёты.
do $$
begin
    if to_regclass('public.love_stage_members') is not null then
        execute $sql$
            delete from public.love_stage_members m
             where not exists (select 1 from public.relationships r where r.id = m.relationship_id)
        $sql$;
    end if;
    if to_regclass('public.love_stage_rewards') is not null then
        execute $sql$
            delete from public.love_stage_rewards s
             where not exists (select 1 from public.relationships r where r.id = s.relationship_id)
        $sql$;
    end if;
    if to_regclass('public.love_rewards_log') is not null then
        execute $sql$
            delete from public.love_rewards_log l
             where l.relationship_id <> 0
               and not exists (select 1 from public.relationships r where r.id = l.relationship_id)
        $sql$;
    end if;
end $$;

notify pgrst, 'reload schema';
