-- ############################################################################
-- ЗАДАЧА 84: «ничего не начисляется» — журнал ФАКТИЧЕСКИХ начислений.
--
-- Было: решали, «оплачен ли уровень», по служебной отметке. Отметка могла
-- остаться от старого бага, от неудачной миграции или от прошлого сбоя, при
-- котором деньги не дошли, — и уровень навсегда считался оплаченным. Со
-- стороны это выглядело так: «перешёл на 3-й уровень, а награды нет».
--
-- Стало: таблица love_rewards_log. Строка пишется ТОЛЬКО после того, как
-- коины реально легли на баланс (love._credit_coins перечитывает баланс и
-- возвращает True только при прибавке). Код считает уровень выплаченным по
-- журналу, а не по отметке, поэтому «долг» догоняется при следующем действии
-- или заходе в профиль.
--
-- relationship_id = 0 — награда одиночной ветки (Unlove).
-- ############################################################################

create table if not exists public.love_rewards_log (
    id              bigserial   primary key,
    relationship_id bigint      not null default 0,
    user_id         bigint      not null,
    stage           integer     not null,
    reward          integer     not null default 0,
    paid_at         timestamptz not null default now()
);

create index if not exists love_rewards_log_pair_idx
    on public.love_rewards_log (relationship_id, user_id);
create index if not exists love_rewards_log_user_idx
    on public.love_rewards_log (user_id);

-- ВАЖНО: старые отметки (love_stage_members / love_stage_rewards) в журнал НЕ
-- переносим. Они могли остаться от сбоя, при котором деньги не дошли, — и
-- тогда игрок снова остался бы без награды. Пустой журнал = «подтверждённых
-- выплат нет», поэтому после миграции каждый игрок один раз получит догон за
-- уровни, которые прошёл, а дальше журнал закрывает повтор.

-- Права: сервисный ключ обходит RLS, но если RLS включён — без политики код
-- получит «42501 row-level security policy», и журнал перестанет писаться.
do $$
begin
    if to_regclass('public.love_rewards_log') is not null then
        execute 'alter table public.love_rewards_log enable row level security';
        execute 'drop policy if exists love_rewards_log_service_all on public.love_rewards_log';
        execute 'create policy love_rewards_log_service_all on public.love_rewards_log '
                'for all using (true) with check (true)';
    end if;
exception when others then
    raise notice 'love_rewards_log: политику создать не удалось: %', sqlerrm;
end $$;

grant all on public.love_rewards_log to service_role;
grant all on sequence public.love_rewards_log_id_seq to service_role;

-- Проверка (должно вернуть 1):
--   select count(*) from information_schema.tables
--    where table_schema = 'public' and table_name = 'love_rewards_log';
