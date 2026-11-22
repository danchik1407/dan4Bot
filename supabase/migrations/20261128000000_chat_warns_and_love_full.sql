-- ============================================================================
-- AURA GAME — задача 57. ОДИН ФАЙЛ НА ДВЕ ПРОБЛЕМЫ ИЗ СКРИНШОТОВ.
--
--   ЧАСТЬ 1 — ПРЕДУПРЕЖДЕНИЯ (!аурапред):
--     «В базу не сохранилось: ошибка базы: {'message': "Could not find the
--      'warns' column of 'chat_warns' in the schema cache", 'code': 'PGRST204'}»
--     → у таблицы chat_warns нет колонки warns (или таблицы нет вовсе),
--       либо нет составного ключа, по которому бот делает upsert.
--
--   ЧАСТЬ 2 — LOVE / UNLOVE («Обнять», «Любовь», «По душам»):
--     «⚠️ Love не записался в базу — бонус не начислен … relationships /
--      solo_love, миграция 20261124000000_love_fix.sql»
--     → нет таблиц пар и одиночного счёта, нет колонок или RLS не даёт писать.
--
-- КАК ПРИМЕНЯТЬ (2 минуты):
--   1) Supabase → SQL Editor → New query;
--   2) вставить ВЕСЬ этот файл (всё, что ниже) → Run;
--   3) в конце должны появиться строки «ok» из самопроверки (блок 3).
--
-- Файл ИДЕМПОТЕНТНЫЙ: применять можно повторно сколько угодно раз, данные он
-- не удаляет (кроме дублей в chat_warns, если у таблицы не было ключа).
-- ============================================================================


-- ############################################################################
-- ЧАСТЬ 1. ПРЕДУПРЕЖДЕНИЯ: chat_warns
-- ############################################################################

-- 1.1 Таблица. Если её нет — создаём сразу с колонкой warns.
create table if not exists public.chat_warns (
    chat_id     bigint      not null,
    user_id     bigint      not null,
    warns       integer     not null default 0,
    muted_until timestamptz,
    updated_at  timestamptz not null default now(),
    primary key (chat_id, user_id)
);

-- 1.2 Таблица БЫЛА, но без колонки warns — это и есть PGRST204 со скриншота.
alter table public.chat_warns add column if not exists warns       integer;
alter table public.chat_warns add column if not exists muted_until timestamptz;
alter table public.chat_warns add column if not exists updated_at  timestamptz;

-- Пустые значения приводим к рабочим: старые строки без счётчика = 0.
update public.chat_warns set warns = 0       where warns is null;
update public.chat_warns set updated_at = now() where updated_at is null;
alter table public.chat_warns alter column warns set default 0;
alter table public.chat_warns alter column updated_at set default now();

-- 1.3 Составной ключ (chat_id, user_id). Без него бот при сохранении получает
--     «there is no unique or exclusion constraint matching the ON CONFLICT
--     specification» и предупреждение снова не сохраняется.
do $$
declare
    pair_key boolean;
    any_pk   boolean;
begin
    select exists (
        select 1
        from pg_constraint c
        join pg_class t     on t.oid = c.conrelid
        join pg_namespace n on n.oid = t.relnamespace
        where n.nspname = 'public' and t.relname = 'chat_warns'
          and c.contype in ('p', 'u')
          and (
                select array_agg(a.attname::text order by a.attname)
                from unnest(c.conkey) as u(attnum)
                join pg_attribute a on a.attrelid = t.oid and a.attnum = u.attnum
              ) = array['chat_id', 'user_id']::text[]
    ) into pair_key;

    if pair_key then
        raise notice 'chat_warns: ключ (chat_id, user_id) уже есть';
        return;
    end if;

    select exists (
        select 1
        from pg_constraint c
        join pg_class t     on t.oid = c.conrelid
        join pg_namespace n on n.oid = t.relnamespace
        where n.nspname = 'public' and t.relname = 'chat_warns' and c.contype = 'p'
    ) into any_pk;

    if any_pk then
        -- Другой первичный ключ (например, только chat_id) — не ломаем его,
        -- а добавляем уникальность на пару: боту достаточно и UNIQUE.
        delete from public.chat_warns a
        using public.chat_warns b
        where a.ctid < b.ctid and a.chat_id = b.chat_id and a.user_id = b.user_id;
        alter table public.chat_warns
            add constraint chat_warns_chat_user_key unique (chat_id, user_id);
        raise notice 'chat_warns: добавлен UNIQUE (chat_id, user_id)';
    else
        delete from public.chat_warns a
        using public.chat_warns b
        where a.ctid < b.ctid and a.chat_id = b.chat_id and a.user_id = b.user_id;
        alter table public.chat_warns add primary key (chat_id, user_id);
        raise notice 'chat_warns: добавлен первичный ключ (chat_id, user_id)';
    end if;
end $$;

-- 1.4 Доступ: сервер (бот и API) ходит anon-ключом, поэтому RLS открываем
--     политикой на чтение и запись. Личных данных в таблице нет.
alter table public.chat_warns enable row level security;
drop policy if exists chat_warns_all             on public.chat_warns;
drop policy if exists chat_warns_service_all     on public.chat_warns;
create policy chat_warns_all on public.chat_warns
    for all using (true) with check (true);

grant all on table public.chat_warns to anon, authenticated, service_role;

create index if not exists chat_warns_chat_idx on public.chat_warns (chat_id);


-- ############################################################################
-- ЧАСТЬ 2. LOVE / UNLOVE: отношения, пары и одиночный счёт
-- ############################################################################

-- 2.1 users: колонки, без которых не работают награды и профиль.
--     Если таблицы users нет вовсе (совсем пустая база) — создаём минимум.
create table if not exists public.users (
    user_id    bigint primary key,
    username   text,
    coins      bigint      not null default 0,
    keys       integer     not null default 0,
    inventory  jsonb       not null default '[]'::jsonb,
    created_at timestamptz not null default now()
);

alter table public.users add column if not exists coins      bigint      not null default 0;
alter table public.users add column if not exists keys       integer     not null default 0;
alter table public.users add column if not exists inventory  jsonb       not null default '[]'::jsonb;
alter table public.users add column if not exists ice        integer     not null default 0;
alter table public.users add column if not exists premium    boolean     not null default false;
alter table public.users add column if not exists username   text;
alter table public.users add column if not exists photo_url  text;
alter table public.users add column if not exists created_at timestamptz not null default now();

-- 2.2 relationships — пара. Создаём, если нет, и добираем колонки, если есть.
create table if not exists public.relationships (
    id         bigserial primary key,
    user_a     bigint      not null,
    user_b     bigint      not null,
    love       integer     not null default 0,
    bank       integer     not null default 0,
    status     text        not null default 'active',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.relationships add column if not exists love       integer;
alter table public.relationships add column if not exists bank       integer;
alter table public.relationships add column if not exists status     text;
alter table public.relationships add column if not exists created_at timestamptz;
alter table public.relationships add column if not exists updated_at timestamptz;

update public.relationships set love       = 0                       where love       is null;
update public.relationships set bank       = 0                       where bank       is null;
update public.relationships set status     = 'active'                where status     is null;
update public.relationships set created_at = now()                   where created_at is null;
update public.relationships set updated_at = now()                   where updated_at is null;

alter table public.relationships alter column love    set default 0;
alter table public.relationships alter column bank    set default 0;
alter table public.relationships alter column status  set default 'active';

create index if not exists relationships_user_a_idx on public.relationships (user_a);
create index if not exists relationships_user_b_idx on public.relationships (user_b);
create index if not exists relationships_status_idx on public.relationships (status);

-- 2.3 solo_love — «Unlove» одиночки. Именно здесь живёт счёт того, кто «обнял»
--     без пары. Таблицы не было → «Love не записался в базу».
create table if not exists public.solo_love (
    user_id    bigint      primary key,
    love       integer     not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.solo_love add column if not exists love       integer;
alter table public.solo_love add column if not exists created_at timestamptz;
alter table public.solo_love add column if not exists updated_at timestamptz;

update public.solo_love set love       = 0     where love       is null;
update public.solo_love set created_at = now() where created_at is null;
update public.solo_love set updated_at = now() where updated_at is null;

alter table public.solo_love alter column love set default 0;

-- 2.4 Награды за этапы, кулдаун РП и остальные таблицы любви.
create table if not exists public.love_stage_rewards (
    relationship_id bigint      not null,
    stage           integer     not null,
    paid_at         timestamptz not null default now(),
    primary key (relationship_id, stage)
);

create table if not exists public.solo_love_stage_rewards (
    user_id bigint      not null,
    stage   integer     not null,
    paid_at timestamptz not null default now(),
    primary key (user_id, stage)
);

create table if not exists public.love_cooldowns (
    user_id   bigint      primary key,
    action_at timestamptz not null default now()
);

create table if not exists public.passive_income (
    user_id bigint      primary key,
    last_at timestamptz not null default now()
);

-- «Мелочь» пассивного дохода (задача 56): дробные коины, которые нельзя выдать
-- целыми. Без таблицы они копятся только в памяти процесса и теряются при
-- рестарте — на скриншотах это выглядело как «пассивка не считается».
create table if not exists public.passive_carry (
    user_id bigint       primary key,
    carry   numeric(10,6) not null default 0
);

create table if not exists public.subscriptions_history (
    user_id       bigint      primary key,
    bonus_claimed boolean     not null default true,
    reward        integer     not null default 200,
    created_at    timestamptz not null default now()
);

create table if not exists public.referrals (
    referred_id bigint      primary key,
    referrer_id bigint      not null,
    reward      integer     not null default 0,
    status      text        not null default 'paid',
    created_at  timestamptz not null default now(),
    paid_at     timestamptz
);

create table if not exists public.stars_payments (
    id         bigserial   primary key,
    user_id    bigint      not null,
    charge_id  text,
    coins      integer     not null default 0,
    created_at timestamptz not null default now()
);

-- Колонки stars_payments, которые могли появиться только в коде.
alter table public.stars_payments add column if not exists stars integer not null default 0;

-- 2.5 Один активный роман на игрока: двойное «Да» не создаст две пары.
create or replace function public.love_single_active_relationship()
returns trigger
language plpgsql
as $$
begin
    if new.status = 'active' then
        if exists (
            select 1
            from public.relationships as r
            where r.status = 'active'
              and r.id is distinct from new.id
              and (r.user_a in (new.user_a, new.user_b)
                   or r.user_b in (new.user_a, new.user_b))
        ) then
            raise exception 'Участник уже состоит в активных отношениях'
                using hint = 'Сначала завершите текущие отношения командой -отны';
        end if;
    end if;
    return new;
end;
$$;

drop trigger if exists love_single_active_relationship_trg on public.relationships;
create trigger love_single_active_relationship_trg
    before insert or update of user_a, user_b, status on public.relationships
    for each row
    execute function public.love_single_active_relationship();

-- updated_at пары обновляется сам — бот и Mini App это поле только читают.
create or replace function public.relationships_touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists relationships_touch_updated_at_trg on public.relationships;
create trigger relationships_touch_updated_at_trg
    before update on public.relationships
    for each row
    execute function public.relationships_touch_updated_at();

-- 2.6 RLS. Сервер ходит anon/сервисным ключом: без политики INSERT пары и
--     upsert Unlove падают (или молча ничего не меняют) — из-за этого игрок
--     видел «Love не записался», а после рестарта счёт откатывался.
do $$
declare
    tbl  text;
    list text[] := array[
        'users', 'relationships', 'love_stage_rewards', 'love_cooldowns',
        'solo_love', 'solo_love_stage_rewards', 'passive_income', 'passive_carry',
        'subscriptions_history', 'referrals', 'stars_payments',
        'idempotency_keys', 'game_settings', 'promocodes', 'promocode_uses',
        'users_inventory', 'shop_purchases', 'capsule_stock', 'chest_claims',
        'crash_history', 'upgrade_log', 'bans', 'chat_warns'
    ];
begin
    foreach tbl in array list loop
        if exists (select 1 from information_schema.tables
                    where table_schema = 'public' and table_name = tbl) then
            execute format('alter table public.%I enable row level security', tbl);
            execute format('drop policy if exists %I on public.%I', tbl || '_service_all', tbl);
            execute format(
                'create policy %I on public.%I for all using (true) with check (true)',
                tbl || '_service_all', tbl
            );
            execute format('grant all on table public.%I to anon, authenticated, service_role', tbl);
        end if;
    end loop;
end $$;

-- Права на последовательности (bigserial): без них INSERT пары падал бы с
-- «permission denied for sequence relationships_id_seq».
grant usage, select on all sequences in schema public to anon, authenticated, service_role;

-- 2.7 Кэш схемы PostgREST. БЕЗ ЭТОЙ СТРОКИ Supabase ещё несколько минут
--     отвечает PGRST204 «Could not find the 'warns' column», даже когда колонка
--     уже создана. Именно поэтому «применил миграцию, а ошибка та же».
notify pgrst, 'reload schema';


-- ############################################################################
-- ЧАСТЬ 3. САМОПРОВЕРКА. После Run должно быть три строки «ok».
-- Если где-то «НЕТ» — строка не выполнилась, прокрутите лог выше.
-- ############################################################################
select '1. chat_warns.warns' as check_name,
       case when exists (
                select 1 from information_schema.columns
                 where table_schema = 'public' and table_name = 'chat_warns'
                   and column_name = 'warns')
            then 'ok' else 'НЕТ — колонка не создалась' end as result
union all
select '2. chat_warns ключ (chat_id, user_id)',
       case when exists (
                select 1
                from pg_constraint c
                join pg_class t     on t.oid = c.conrelid
                join pg_namespace n on n.oid = t.relnamespace
                where n.nspname = 'public' and t.relname = 'chat_warns'
                  and c.contype in ('p', 'u')
                  and (select array_agg(a.attname::text order by a.attname)
                         from unnest(c.conkey) as u(attnum)
                         join pg_attribute a on a.attrelid = t.oid and a.attnum = u.attnum)
                      = array['chat_id', 'user_id']::text[])
            then 'ok' else 'НЕТ — бот не сможет сохранять предупреждения' end
union all
select '3. relationships + solo_love + колонка love',
       case when (select count(*) from information_schema.tables
                   where table_schema = 'public'
                     and table_name in ('relationships', 'solo_love')) = 2
                 and exists (select 1 from information_schema.columns
                              where table_schema = 'public' and table_name = 'solo_love'
                                and column_name = 'love')
            then 'ok' else 'НЕТ — таблицы любви не создались' end;
