-- ЧАСТЬ 1. Таблицы и колонки

create table if not exists public.users (
    user_id bigint,
    username text,
    chat_id bigint default 0,
    coins bigint default 0,
    keys integer default 0,
    inventory jsonb default '[]'::jsonb,
    last_chest bigint default 0,
    created_at timestamptz default now(),
    photo_url text,
    ice integer,
    premium boolean,
    daily_buys text,
    last_shop_reset bigint,
    primary key (user_id)
);

alter table public.users add column if not exists username text, add column if not exists chat_id bigint default 0, add column if not exists coins bigint default 0, add column if not exists keys integer default 0, add column if not exists inventory jsonb default '[]'::jsonb, add column if not exists last_chest bigint default 0, add column if not exists created_at timestamptz default now(), add column if not exists photo_url text, add column if not exists ice integer, add column if not exists premium boolean, add column if not exists daily_buys text, add column if not exists last_shop_reset bigint;

create table if not exists public.chat_warns (
    chat_id bigint,
    user_id bigint,
    warns integer default 0,
    muted_until timestamptz,
    updated_at timestamptz default now(),
    primary key (chat_id, user_id)
);

alter table public.chat_warns add column if not exists warns integer default 0, add column if not exists muted_until timestamptz, add column if not exists updated_at timestamptz default now();

create table if not exists public.relationships (
    id bigserial,
    user_a bigint,
    user_b bigint,
    love integer default 0,
    bank integer default 0,
    status text default 'active',
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    primary key (id)
);

alter table public.relationships add column if not exists user_a bigint, add column if not exists user_b bigint, add column if not exists love integer default 0, add column if not exists bank integer default 0, add column if not exists status text default 'active', add column if not exists created_at timestamptz default now(), add column if not exists updated_at timestamptz default now();

create table if not exists public.solo_love (
    user_id bigint,
    love integer default 0,
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    primary key (user_id)
);

alter table public.solo_love add column if not exists love integer default 0, add column if not exists created_at timestamptz default now(), add column if not exists updated_at timestamptz default now();

create table if not exists public.love_stage_rewards (
    relationship_id bigint,
    stage integer,
    paid_at timestamptz default now(),
    primary key (relationship_id, stage)
);

alter table public.love_stage_rewards add column if not exists paid_at timestamptz default now();

create table if not exists public.solo_love_stage_rewards (
    user_id bigint,
    stage integer,
    paid_at timestamptz default now(),
    primary key (user_id, stage)
);

alter table public.solo_love_stage_rewards add column if not exists paid_at timestamptz default now();

create table if not exists public.love_cooldowns (
    user_id bigint,
    action_at timestamptz default now(),
    primary key (user_id)
);

alter table public.love_cooldowns add column if not exists action_at timestamptz default now();

create table if not exists public.passive_income (
    user_id bigint,
    last_at timestamptz default now(),
    primary key (user_id)
);

alter table public.passive_income add column if not exists last_at timestamptz default now();

create table if not exists public.passive_carry (
    user_id bigint,
    carry numeric(10,6) default 0,
    primary key (user_id)
);

alter table public.passive_carry add column if not exists carry numeric(10,6) default 0;

create table if not exists public.app_first_seen (
    user_id bigint,
    first_seen timestamptz default now(),
    primary key (user_id)
);

alter table public.app_first_seen add column if not exists first_seen timestamptz default now();

create table if not exists public.subscriptions_history (
    user_id bigint,
    bonus_claimed boolean default true,
    reward integer default 200,
    created_at timestamptz default now(),
    primary key (user_id)
);

alter table public.subscriptions_history add column if not exists bonus_claimed boolean default true, add column if not exists reward integer default 200, add column if not exists created_at timestamptz default now();

create table if not exists public.referrals (
    referred_id bigint,
    referrer_id bigint,
    reward integer default 0,
    status text default 'paid',
    created_at timestamptz default now(),
    paid_at timestamptz,
    primary key (referred_id)
);

alter table public.referrals add column if not exists referrer_id bigint, add column if not exists reward integer default 0, add column if not exists status text default 'paid', add column if not exists created_at timestamptz default now(), add column if not exists paid_at timestamptz;

create table if not exists public.stars_payments (
    id bigserial,
    user_id bigint,
    charge_id text,
    coins integer default 0,
    stars integer default 0,
    created_at timestamptz default now(),
    primary key (id)
);

alter table public.stars_payments add column if not exists user_id bigint, add column if not exists charge_id text, add column if not exists coins integer default 0, add column if not exists stars integer default 0, add column if not exists created_at timestamptz default now();

create table if not exists public.idempotency_keys (
    key text,
    user_id bigint,
    endpoint text default '',
    response text default '{}',
    created_at timestamptz default now(),
    primary key (key)
);

alter table public.idempotency_keys add column if not exists user_id bigint, add column if not exists endpoint text default '', add column if not exists response text default '{}', add column if not exists created_at timestamptz default now();

create table if not exists public.game_settings (
    key text,
    value text,
    updated_at timestamptz default now(),
    primary key (key)
);

alter table public.game_settings add column if not exists value text, add column if not exists updated_at timestamptz default now();

create table if not exists public.bans (
    user_id bigint,
    reason text,
    created_at timestamptz default now(),
    primary key (user_id)
);

alter table public.bans add column if not exists reason text, add column if not exists created_at timestamptz default now();

create table if not exists public.crash_history (
    id bigserial,
    round_id text,
    crash_point numeric(10,2) default 0,
    players jsonb default '[]'::jsonb,
    created_at timestamptz default now(),
    primary key (id)
);

alter table public.crash_history add column if not exists round_id text, add column if not exists crash_point numeric(10,2) default 0, add column if not exists players jsonb default '[]'::jsonb, add column if not exists created_at timestamptz default now();

create table if not exists public.promocodes (
    code text,
    uses integer default 1,
    amount integer default 0,
    reward_type text default 'coins',
    item_id text,
    created_at timestamptz default now(),
    primary key (code)
);

alter table public.promocodes add column if not exists uses integer default 1, add column if not exists amount integer default 0, add column if not exists reward_type text default 'coins', add column if not exists item_id text, add column if not exists created_at timestamptz default now();

create table if not exists public.promocode_uses (
    code text,
    user_id bigint,
    created_at timestamptz default now(),
    primary key (code, user_id)
);

alter table public.promocode_uses add column if not exists created_at timestamptz default now();

create table if not exists public.shop_purchases (
    id bigserial,
    user_id bigint,
    item_id text,
    price integer default 0,
    source text default 'mini',
    created_at timestamptz default now(),
    primary key (id)
);

alter table public.shop_purchases add column if not exists user_id bigint, add column if not exists item_id text, add column if not exists price integer default 0, add column if not exists source text default 'mini', add column if not exists created_at timestamptz default now();

create table if not exists public.capsule_stock (
    item_id text,
    stock integer default 0,
    total integer default 0,
    updated_at timestamptz default now(),
    primary key (item_id)
);

alter table public.capsule_stock add column if not exists stock integer default 0, add column if not exists total integer default 0, add column if not exists updated_at timestamptz default now();

create table if not exists public.chest_claims (
    id bigserial,
    user_id text,
    chest_type text default 'normal',
    reward_coins integer default 0,
    reward_keys integer default 0,
    reward_item text default '',
    reward_item_amount integer default 0,
    claimed boolean default false,
    created_at timestamptz default now(),
    claimed_at timestamptz,
    primary key (id)
);

alter table public.chest_claims add column if not exists user_id text, add column if not exists chest_type text default 'normal', add column if not exists reward_coins integer default 0, add column if not exists reward_keys integer default 0, add column if not exists reward_item text default '', add column if not exists reward_item_amount integer default 0, add column if not exists claimed boolean default false, add column if not exists created_at timestamptz default now(), add column if not exists claimed_at timestamptz;

create table if not exists public.upgrade_log (
    id bigserial,
    user_id bigint,
    item_from text,
    amount_from integer default 0,
    item_to text,
    chance numeric(6, 2) default 0,
    roll numeric(6, 2) default 0,
    won boolean default false,
    win_item text,
    win_amount bigint default 0,
    coins_before bigint default 0,
    coins_after bigint default 0,
    inventory_after jsonb default '[]'::jsonb,
    seen boolean default false,
    created_at timestamptz default now(),
    primary key (id)
);

alter table public.upgrade_log add column if not exists user_id bigint, add column if not exists item_from text, add column if not exists amount_from integer default 0, add column if not exists item_to text, add column if not exists chance numeric(6, 2) default 0, add column if not exists roll numeric(6, 2) default 0, add column if not exists won boolean default false, add column if not exists win_item text, add column if not exists win_amount bigint default 0, add column if not exists coins_before bigint default 0, add column if not exists coins_after bigint default 0, add column if not exists inventory_after jsonb default '[]'::jsonb, add column if not exists seen boolean default false, add column if not exists created_at timestamptz default now();

create table if not exists public.arena_rounds (
    arena text,
    round_id text,
    state jsonb,
    updated_at timestamptz default now(),
    primary key (arena)
);

alter table public.arena_rounds add column if not exists round_id text, add column if not exists state jsonb, add column if not exists updated_at timestamptz default now();

create table if not exists public.arena_payouts (
    arena text,
    round_id text,
    user_id bigint,
    amount bigint default 0,
    status text default 'pending',
    error text,
    created_at timestamptz default now(),
    paid_at timestamptz,
    primary key (arena, round_id, user_id)
);

alter table public.arena_payouts add column if not exists amount bigint default 0, add column if not exists status text default 'pending', add column if not exists error text, add column if not exists created_at timestamptz default now(), add column if not exists paid_at timestamptz;
-- ЧАСТЬ 2. Приведение старых баз
-- 2.1 users.user_id → BIGINT (иначе «12» и 12 — разные игроки)
do $$
declare t text;
begin
    select data_type into t from information_schema.columns
     where table_schema = 'public' and table_name = 'users' and column_name = 'user_id';
    if t is not null and t not in ('bigint', 'integer', 'smallint') then
        begin
            execute 'alter table public.users alter column user_id type bigint using user_id::bigint';
            raise notice 'users.user_id приведён к BIGINT (было %)', t;
        exception when others then
            raise notice 'users.user_id оставлен как %: %', t, sqlerrm;
        end;
    end if;
end $$;

-- 2.2 Дубли user_id: балансы складываем, лишние строки убираем
do $$
begin
    if exists (select 1 from public.users group by user_id having count(*) > 1) then
        with grouped as (
            select user_id, sum(coalesce(coins, 0)) as coins_sum, max(ctid::text) as keep_ctid
              from public.users group by user_id having count(*) > 1
        ), upd as (
            update public.users u set coins = g.coins_sum
              from grouped g where u.user_id = g.user_id and u.ctid::text = g.keep_ctid
            returning u.user_id
        )
        delete from public.users u using grouped g
         where u.user_id = g.user_id and u.ctid::text <> g.keep_ctid;
        raise notice 'users: дубли user_id схлопнуты, балансы сложены';
    end if;
end $$;

do $$
begin
    if not exists (select 1 from pg_constraint c join pg_class t on t.oid = c.conrelid
                    join pg_namespace n on n.oid = t.relnamespace
                   where n.nspname = 'public' and t.relname = 'users' and c.contype = 'p') then
        alter table public.users add primary key (user_id);
        raise notice 'users: добавлен первичный ключ user_id';
    end if;
end $$;

-- 2.3 chat_warns: составной ключ (chat_id, user_id) — без него upsert из бота падает
do $$
declare pair_key boolean; any_pk boolean;
begin
    select exists (
        select 1 from pg_constraint c join pg_class t on t.oid = c.conrelid
          join pg_namespace n on n.oid = t.relnamespace
         where n.nspname = 'public' and t.relname = 'chat_warns' and c.contype in ('p','u')
           and (select array_agg(a.attname::text order by a.attname)
                  from unnest(c.conkey) as u(attnum)
                  join pg_attribute a on a.attrelid = t.oid and a.attnum = u.attnum)
               = array['chat_id', 'user_id']::text[])
    into pair_key;
    if pair_key then return; end if;

    select exists (select 1 from pg_constraint c join pg_class t on t.oid = c.conrelid
                     join pg_namespace n on n.oid = t.relnamespace
                    where n.nspname = 'public' and t.relname = 'chat_warns' and c.contype = 'p')
    into any_pk;

    delete from public.chat_warns a using public.chat_warns b
     where a.ctid < b.ctid and a.chat_id = b.chat_id and a.user_id = b.user_id;

    if any_pk then
        alter table public.chat_warns add constraint chat_warns_chat_user_key unique (chat_id, user_id);
    else
        alter table public.chat_warns add primary key (chat_id, user_id);
    end if;
    raise notice 'chat_warns: ключ (chat_id, user_id) добавлен';
end $$;

-- 2.4 Значения NULL → рабочие дефолты (суммы игроков не трогаем)
update public.users set coins = 0 where coins is null;
update public.users set keys = 0 where keys is null;
update public.users set inventory = '[]'::jsonb where inventory is null;
update public.users set chat_id = 0 where chat_id is null;
update public.users set last_chest = 0 where last_chest is null;
update public.users set created_at = now() where created_at is null;
update public.chat_warns set warns = 0 where warns is null;
update public.relationships set love = 0 where love is null;
update public.relationships set bank = 0 where bank is null;
update public.relationships set status = 'active' where status is null;
update public.solo_love set love = 0 where love is null;
update public.upgrade_log set seen = false where seen is null;
update public.chest_claims set reward_item = '' where reward_item is null;
update public.chest_claims set reward_item_amount = 0 where reward_item_amount is null;
update public.capsule_stock set stock = 0 where stock is null;
update public.capsule_stock set total = 0 where total is null;

-- 2.5 Тираж капсул: только недостающие строки, существующие остатки не трогаем
insert into public.capsule_stock (item_id, stock, total) values
    ('capsule', 1000, 1000), ('uniq_capsule', 500, 500), ('ruby', 100, 100),
    ('diamond', 50, 50), ('galactic', 10, 10)
on conflict (item_id) do nothing;

insert into public.game_settings (key, value) values
    ('upgrade_house_edge', '1.0'), ('upgrade_chance_factor', '1.0')
on conflict (key) do nothing;

-- 2.6 Один активный роман на игрока
create or replace function public.love_single_active_relationship()
returns trigger language plpgsql as $$
begin
    if new.status = 'active' and exists (
        select 1 from public.relationships r
         where r.status = 'active' and r.id is distinct from new.id
           and (r.user_a in (new.user_a, new.user_b) or r.user_b in (new.user_a, new.user_b))
    ) then
        raise exception 'Участник уже состоит в активных отношениях'
            using hint = 'Сначала завершите текущие отношения командой -отны';
    end if;
    return new;
end $$;

drop trigger if exists love_single_active_relationship_trg on public.relationships;
create trigger love_single_active_relationship_trg
    before insert or update of user_a, user_b, status on public.relationships
    for each row execute function public.love_single_active_relationship();

-- 2.7 Индексы
create index if not exists users_coins_idx on public.users (coins desc);
create index if not exists chat_warns_chat_idx on public.chat_warns (chat_id);
create index if not exists relationships_user_a_idx on public.relationships (user_a);
create index if not exists relationships_user_b_idx on public.relationships (user_b);
create index if not exists upgrade_log_user_idx on public.upgrade_log (user_id, id desc);
create index if not exists chest_claims_user_idx on public.chest_claims (user_id, claimed);
create index if not exists shop_purchases_user_idx on public.shop_purchases (user_id);
create index if not exists arena_payouts_pending_idx on public.arena_payouts (status, created_at);
-- ЧАСТЬ 3. Функции (ровно те сигнатуры, которые ждёт api.py)
drop function if exists public.arena_payout_apply(text, text, bigint, bigint);
create function public.arena_payout_apply(p_arena text, p_round_id text,
                                          p_user_id bigint, p_amount bigint)
returns boolean language plpgsql security definer as $$
declare v_rows integer;
begin
    if p_amount is null or p_amount <= 0 then return false; end if;
    update public.arena_payouts
       set status = 'paid', paid_at = now(), error = null, amount = p_amount
     where arena = p_arena and round_id = p_round_id and user_id = p_user_id
       and status <> 'paid';
    get diagnostics v_rows = row_count;
    if v_rows = 0 then return false; end if;
    update public.users set coins = coalesce(coins, 0) + p_amount where user_id = p_user_id;
    return true;
end $$;

drop function if exists public.chest_claim_create(text, text, integer, integer);
drop function if exists public.chest_claim_create(text, text, integer, integer, text, integer);
drop function if exists public.chest_claim_pending(text);
drop function if exists public.chest_claim_take(bigint, text);
drop function if exists public.chest_claim_release(bigint, text);

create function public.chest_claim_create(
    p_user_id text, p_chest_type text default 'normal', p_coins integer default 0,
    p_keys integer default 0, p_item text default '', p_item_amount integer default 0)
returns table (claim_id bigint) language plpgsql security definer as $$
declare v_item text := coalesce(btrim(p_item), '');
begin
    return query
    insert into public.chest_claims
        (user_id, chest_type, reward_coins, reward_keys, reward_item, reward_item_amount, claimed)
    values (btrim(p_user_id::text), coalesce(nullif(btrim(p_chest_type), ''), 'normal'),
            greatest(coalesce(p_coins, 0), 0), greatest(coalesce(p_keys, 0), 0), v_item,
            case when v_item = '' then 0 else greatest(coalesce(p_item_amount, 0), 0) end, false)
    returning chest_claims.id;
end $$;

create function public.chest_claim_pending(p_user_id text)
returns table (id bigint, reward_coins integer, reward_keys integer,
               reward_item text, reward_item_amount integer)
language sql security definer as $$
    select c.id, c.reward_coins, c.reward_keys,
           coalesce(c.reward_item, ''), coalesce(c.reward_item_amount, 0)
      from public.chest_claims c
     where c.user_id = btrim(p_user_id::text) and c.claimed = false
     order by c.id;
$$;

create function public.chest_claim_take(p_claim_id bigint, p_user_id text)
returns table (reward_coins integer, reward_keys integer, reward_item text,
               reward_item_amount integer, already_claimed boolean)
language plpgsql security definer as $$
declare v_row public.chest_claims%rowtype;
begin
    update public.chest_claims c
       set claimed = true, claimed_at = now()
     where c.id = p_claim_id and c.user_id = btrim(p_user_id::text) and c.claimed = false
    returning c.* into v_row;
    if v_row.id is not null then
        return query select coalesce(v_row.reward_coins, 0), coalesce(v_row.reward_keys, 0),
                            coalesce(v_row.reward_item, ''), coalesce(v_row.reward_item_amount, 0), false;
        return;
    end if;
    select c.* into v_row from public.chest_claims c
     where c.id = p_claim_id and c.user_id = btrim(p_user_id::text);
    if v_row.id is null then return; end if;
    return query select 0, 0, ''::text, 0, true;
end $$;

create function public.chest_claim_release(p_claim_id bigint, p_user_id text)
returns boolean language plpgsql security definer as $$
declare v_rows integer;
begin
    update public.chest_claims set claimed = false, claimed_at = null
     where id = p_claim_id and user_id = btrim(p_user_id::text) and claimed = true;
    get diagnostics v_rows = row_count;
    return v_rows > 0;
end $$;

drop function if exists public.shop_take_stock(text, integer);
drop function if exists public.shop_return_stock(text, integer);

drop function if exists public.shop_take_stock(text);
create function public.shop_take_stock(p_item_id text)
returns integer language plpgsql security definer as $$
declare v_left integer;
begin
    if to_regclass('public.capsule_stock') is null then return -2; end if;
    update public.capsule_stock set stock = stock - 1, updated_at = now()
     where item_id = p_item_id and stock > 0 returning stock into v_left;
    if v_left is null then return -1; end if;
    return v_left;
end $$;

drop function if exists public.shop_return_stock(text);
create function public.shop_return_stock(p_item_id text)
returns integer language plpgsql security definer as $$
declare v_left integer;
begin
    if to_regclass('public.capsule_stock') is null then return -2; end if;
    update public.capsule_stock set stock = stock + 1, updated_at = now()
     where item_id = p_item_id and stock < total returning stock into v_left;
    return coalesce(v_left, -1);
end $$;

drop function if exists public.shop_stock_list();
create function public.shop_stock_list()
returns table (item_id text, stock integer, total integer)
language sql security definer as $$
    select c.item_id, c.stock, c.total from public.capsule_stock c order by c.item_id;
$$;

grant execute on function public.arena_payout_apply(text, text, bigint, bigint) to anon, authenticated, service_role;
grant execute on function public.chest_claim_create(text, text, integer, integer, text, integer) to anon, authenticated, service_role;
grant execute on function public.chest_claim_pending(text) to anon, authenticated, service_role;
grant execute on function public.chest_claim_take(bigint, text) to anon, authenticated, service_role;
grant execute on function public.chest_claim_release(bigint, text) to anon, authenticated, service_role;
grant execute on function public.shop_take_stock(text) to anon, authenticated, service_role;
grant execute on function public.shop_return_stock(text) to anon, authenticated, service_role;
grant execute on function public.shop_stock_list() to anon, authenticated, service_role;
-- ЧАСТЬ 4. Права и RLS: без политик запись молча отбрасывается (42501)
do $$
declare tbl text;
declare list text[] := array[
    'users','chat_warns','relationships','solo_love','love_stage_rewards',
    'solo_love_stage_rewards','love_cooldowns','passive_income','passive_carry',
    'app_first_seen','subscriptions_history','referrals','stars_payments',
    'idempotency_keys','game_settings','bans','crash_history','promocodes',
    'promocode_uses','shop_purchases','capsule_stock','chest_claims','upgrade_log',
    'arena_rounds','arena_payouts'];
begin
    foreach tbl in array list loop
        if to_regclass('public.' || tbl) is null then continue; end if;
        execute format('alter table public.%I enable row level security', tbl);
        execute format('drop policy if exists %I on public.%I', tbl || '_service_all', tbl);
        execute format('drop policy if exists %I on public.%I', tbl || '_all', tbl);
        execute format('create policy %I on public.%I for all to anon, authenticated, service_role '
                       'using (true) with check (true)', tbl || '_service_all', tbl);
        execute format('grant all on table public.%I to anon, authenticated, service_role', tbl);
    end loop;
end $$;

grant usage, select on all sequences in schema public to anon, authenticated, service_role;

-- ЧАСТЬ 5. Кэш схемы PostgREST: без этого ещё минуты летит PGRST204
notify pgrst, 'reload schema';

-- ЧАСТЬ 6. Проверка: должно быть пусто, а внизу «ИТОГ | ok»
with expected(tbl, col) as (
    values ('users','coins'), ('users','inventory'), ('users','keys'),
           ('chat_warns','warns'), ('relationships','love'), ('relationships','bank'),
           ('solo_love','love'), ('upgrade_log','seen'), ('upgrade_log','inventory_after'),
           ('chest_claims','reward_item'), ('capsule_stock','stock'),
           ('arena_payouts','status'), ('idempotency_keys','response')
),
tables(tbl) as (
    values ('chat_warns'), ('relationships'), ('solo_love'), ('upgrade_log'),
           ('chest_claims'), ('capsule_stock'), ('arena_rounds'), ('arena_payouts'),
           ('idempotency_keys'), ('game_settings')
),
problems as (
    select 'нет таблицы: ' || t.tbl as what from tables t where to_regclass('public.' || t.tbl) is null
    union all
    select 'нет колонки: ' || e.tbl || '.' || e.col from expected e
     where not exists (select 1 from information_schema.columns c
                        where c.table_schema = 'public'
                          and c.table_name = e.tbl and c.column_name = e.col)
    union all
    select 'нет политики: ' || t.tbl from tables t
     where to_regclass('public.' || t.tbl) is not null
       and not exists (select 1 from pg_policies p
                        where p.schemaname = 'public' and p.tablename = t.tbl)
)
select coalesce((select string_agg(what, '; ') from problems), 'ИТОГ | ok — схема готова, очки будут писаться') as result;
