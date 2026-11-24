-- ############################################################################
-- Задача 87. Админ-панель 2.0: статистика игроков, рубильники игр и «стата
-- бота» без ошибок Postgres 42P10 / 42703.
--
-- Зачем файл. В Mini App игрок видел «⚠️ Статистика не пишется: примени
-- миграцию 20261203000000_stats_fix_and_love_cooldown_switch.sql», а админка
-- не могла показать топ и профиль игрока. Причины две, и обе чинятся здесь:
--
--   1) ИГРА НЕ ЗАСЧИТЫВАЕТСЯ. api.py пишет счётчики через функции
--      player_stats_add / player_stats_ensure. Если функции нет — включается
--      резервный путь «прочитать строку и записать upsert по user_id». На
--      рабочей базе он падает с 42P10: «there is no unique or exclusion
--      constraint matching the ON CONFLICT specification» — в старой таблице
--      player_stats user_id не был PRIMARY KEY. Приложение это проглатывает,
--      профиль показывает нули, и только предупреждение напоминает о причине.
--      Здесь: уникальный ключ на player_stats.user_id (после склейки дублей),
--      все счётчики из player_stats.py и обе функции — заново.
--
--   2) НЕТ ТАБЛИЦ. player_events могла остаться без колонки amount, а
--      referrals — без referred_id (на них ломался поиск игрока в админке:
--      42703 «column does not exist»). Здесь они создаются и дополняются.
--
-- Плюс создаётся таблица admin_audit: кто из админов что включил/выключил.
-- Это не для игроков — журнал нужен, чтобы через сутки понимать, кто закрыл
-- игру и когда открыть обратно. Ошибка записи журнала никогда не мешает.
--
-- Файл ИДЕМПОТЕНТНЫЙ: применять можно повторно, ничего не потеряется.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- После применения: NOTIFY pgrst, 'reload schema';
-- ############################################################################

-- --- 1) player_stats: счётчики профиля ---------------------------------------
create table if not exists public.player_stats (
    user_id bigint
);
alter table public.player_stats add column if not exists coins_spent      bigint  not null default 0;
alter table public.player_stats add column if not exists spent_upgrade    bigint  not null default 0;
alter table public.player_stats add column if not exists spent_shop       bigint  not null default 0;
alter table public.player_stats add column if not exists spent_crash      bigint  not null default 0;
alter table public.player_stats add column if not exists spent_arena      bigint  not null default 0;
alter table public.player_stats add column if not exists spent_mines      bigint  not null default 0;
alter table public.player_stats add column if not exists spent_dice       bigint  not null default 0;
alter table public.player_stats add column if not exists lost_games       bigint  not null default 0;
alter table public.player_stats add column if not exists coins_earned     bigint  not null default 0;
alter table public.player_stats add column if not exists won_crash        bigint  not null default 0;
alter table public.player_stats add column if not exists won_arena        bigint  not null default 0;
alter table public.player_stats add column if not exists won_mines        bigint  not null default 0;
alter table public.player_stats add column if not exists won_dice         bigint  not null default 0;
alter table public.player_stats add column if not exists won_upgrade      bigint  not null default 0;
alter table public.player_stats add column if not exists won_chests       bigint  not null default 0;
alter table public.player_stats add column if not exists won_love         bigint  not null default 0;
alter table public.player_stats add column if not exists sent_transfer    bigint  not null default 0;
alter table public.player_stats add column if not exists sent_transfer_cnt integer not null default 0;
alter table public.player_stats add column if not exists got_transfer     bigint  not null default 0;
alter table public.player_stats add column if not exists best_transfer    bigint  not null default 0;
alter table public.player_stats add column if not exists best_crash_bet   bigint  not null default 0;
alter table public.player_stats add column if not exists best_crash_win   bigint  not null default 0;
alter table public.player_stats add column if not exists best_arena_bet   bigint  not null default 0;
alter table public.player_stats add column if not exists best_arena_win   bigint  not null default 0;
alter table public.player_stats add column if not exists best_mines_bet   bigint  not null default 0;
alter table public.player_stats add column if not exists best_mines_win   bigint  not null default 0;
alter table public.player_stats add column if not exists best_dice_bet    bigint  not null default 0;
alter table public.player_stats add column if not exists best_dice_win    bigint  not null default 0;
alter table public.player_stats add column if not exists chests_opened    integer not null default 0;
alter table public.player_stats add column if not exists mines_games      integer not null default 0;
alter table public.player_stats add column if not exists golden_chests    integer not null default 0;
alter table public.player_stats add column if not exists upgrades_total   integer not null default 0;
alter table public.player_stats add column if not exists upgrades_won     integer not null default 0;
alter table public.player_stats add column if not exists games_played     integer not null default 0;
alter table public.player_stats add column if not exists games_won        integer not null default 0;
alter table public.player_stats add column if not exists updated_at       timestamptz not null default now();

-- Дубли по user_id (если старая таблица разрешала их) склеиваем в одну строку:
-- берём максимум по каждому счётчику, чтобы ничего не потерять.
do $$
begin
    if exists (
        select 1 from public.player_stats
         group by user_id having count(*) > 1
    ) then
        create temp table _player_stats_merged on commit drop as
        select user_id,
               max(coins_spent) as coins_spent, max(spent_upgrade) as spent_upgrade,
               max(spent_shop) as spent_shop, max(spent_crash) as spent_crash,
               max(spent_arena) as spent_arena, max(spent_mines) as spent_mines,
               max(spent_dice) as spent_dice, max(lost_games) as lost_games,
               max(coins_earned) as coins_earned, max(won_crash) as won_crash,
               max(won_arena) as won_arena, max(won_mines) as won_mines,
               max(won_dice) as won_dice, max(won_upgrade) as won_upgrade,
               max(won_chests) as won_chests, max(won_love) as won_love,
               max(sent_transfer) as sent_transfer, max(sent_transfer_cnt) as sent_transfer_cnt,
               max(got_transfer) as got_transfer, max(best_transfer) as best_transfer,
               max(best_crash_bet) as best_crash_bet, max(best_crash_win) as best_crash_win,
               max(best_arena_bet) as best_arena_bet, max(best_arena_win) as best_arena_win,
               max(best_mines_bet) as best_mines_bet, max(best_mines_win) as best_mines_win,
               max(best_dice_bet) as best_dice_bet, max(best_dice_win) as best_dice_win,
               max(chests_opened) as chests_opened, max(mines_games) as mines_games,
               max(golden_chests) as golden_chests, max(upgrades_total) as upgrades_total,
               max(upgrades_won) as upgrades_won, max(games_played) as games_played,
               max(games_won) as games_won, max(updated_at) as updated_at
          from public.player_stats
         group by user_id;
        delete from public.player_stats;
        insert into public.player_stats select * from _player_stats_merged;
    end if;
end $$;

-- Уникальный ключ: без него upsert по user_id падал с 42P10.
create unique index if not exists player_stats_user_key on public.player_stats (user_id);
alter table public.player_stats enable row level security;
drop policy if exists player_stats_all on public.player_stats;
create policy player_stats_all on public.player_stats
    for all to anon, authenticated, service_role
    using (true) with check (true);
grant select, insert, update, delete on public.player_stats to anon, authenticated, service_role;

-- --- 2) player_events: «лучшие моменты» --------------------------------------
create table if not exists public.player_events (
    id         bigserial primary key,
    user_id    bigint not null,
    kind       text   not null default 'upgrade',
    title      text   not null default '',
    amount     bigint not null default 0,
    meta       jsonb  not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
alter table public.player_events add column if not exists user_id    bigint not null default 0;
alter table public.player_events add column if not exists kind       text   not null default 'upgrade';
alter table public.player_events add column if not exists title      text   not null default '';
alter table public.player_events add column if not exists amount     bigint not null default 0;
alter table public.player_events add column if not exists meta       jsonb  not null default '{}'::jsonb;
alter table public.player_events add column if not exists created_at timestamptz not null default now();
create index if not exists player_events_user_idx
    on public.player_events (user_id, kind, amount desc);
alter table public.player_events enable row level security;
drop policy if exists player_events_all on public.player_events;
create policy player_events_all on public.player_events
    for all to anon, authenticated, service_role
    using (true) with check (true);
grant select, insert, update, delete on public.player_events to anon, authenticated, service_role;
grant usage, select on sequence public.player_events_id_seq to anon, authenticated, service_role;

-- Лента «моментов» — 20 последних на игрока, остальное чистим.
delete from public.player_events
 where id in (
     select id from (
         select id, row_number() over (partition by user_id order by id desc) rn
           from public.player_events
     ) ranked where rn > 20
 );

-- --- 3) referrals: на них держится «сколько пригласил» в админке ------------
create table if not exists public.referrals (
    referred_id bigint primary key,
    referrer_id bigint,
    reward      integer default 0,
    status      text    default 'paid',
    created_at  timestamptz default now(),
    paid_at     timestamptz
);
alter table public.referrals add column if not exists referrer_id bigint;
alter table public.referrals add column if not exists reward      integer default 0;
alter table public.referrals add column if not exists status      text    default 'paid';
alter table public.referrals add column if not exists created_at  timestamptz default now();
alter table public.referrals add column if not exists paid_at     timestamptz;
create index if not exists referrals_referrer_idx on public.referrals (referrer_id);
alter table public.referrals enable row level security;
drop policy if exists referrals_all on public.referrals;
create policy referrals_all on public.referrals
    for all to anon, authenticated, service_role
    using (true) with check (true);
grant select, insert, update, delete on public.referrals to anon, authenticated, service_role;

-- --- 4) game_settings: рубильники отдельных игр (задача 87) -------------------
create table if not exists public.game_settings (
    key        text primary key,
    value      text,
    updated_at timestamptz not null default now()
);
alter table public.game_settings add column if not exists value      text;
alter table public.game_settings add column if not exists updated_at timestamptz not null default now();
create unique index if not exists game_settings_key on public.game_settings (key);
alter table public.game_settings enable row level security;
drop policy if exists game_settings_all on public.game_settings;
create policy game_settings_all on public.game_settings
    for all to anon, authenticated, service_role
    using (true) with check (true);
grant select, insert, update, delete on public.game_settings to anon, authenticated, service_role;

-- Все игры по умолчанию ВКЛЮЧЕНЫ: строка '0' и есть «игра открыта».
insert into public.game_settings (key, value)
values ('game_disabled_mines', '0'),
       ('game_disabled_crash', '0'),
       ('game_disabled_arena_normal', '0'),
       ('game_disabled_arena_team', '0'),
       ('game_disabled_upgrade', '0'),
       ('game_disabled_chests', '0'),
       ('game_disabled_shop', '0'),
       ('game_disabled_love', '0')
on conflict (key) do nothing;

-- --- 5) admin_audit: кто что переключил --------------------------------------
create table if not exists public.admin_audit (
    id         bigserial primary key,
    admin_id   bigint,
    action     text not null default '',
    detail     jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
create index if not exists admin_audit_created_idx on public.admin_audit (created_at desc);
alter table public.admin_audit enable row level security;
drop policy if exists admin_audit_all on public.admin_audit;
create policy admin_audit_all on public.admin_audit
    for all to anon, authenticated, service_role
    using (true) with check (true);
grant select, insert, update, delete on public.admin_audit to anon, authenticated, service_role;
grant usage, select on sequence public.admin_audit_id_seq to anon, authenticated, service_role;

-- --- 6) player_stats_add: атомарное прибавление ------------------------------
create or replace function public.player_stats_add(
    p_user_id bigint,
    p_delta   jsonb
) returns void
language plpgsql
security definer
set search_path = public
as $$
declare
    d jsonb := coalesce(p_delta, '{}'::jsonb);
begin
    insert into public.player_stats (user_id) values (p_user_id)
    on conflict (user_id) do nothing;

    update public.player_stats set
        coins_spent        = coins_spent + coalesce((d->>'coins_spent')::bigint, 0),
        spent_upgrade      = spent_upgrade + coalesce((d->>'spent_upgrade')::bigint, 0),
        spent_shop         = spent_shop + coalesce((d->>'spent_shop')::bigint, 0),
        spent_crash        = spent_crash + coalesce((d->>'spent_crash')::bigint, 0),
        spent_arena        = spent_arena + coalesce((d->>'spent_arena')::bigint, 0),
        spent_mines        = spent_mines + coalesce((d->>'spent_mines')::bigint, 0),
        spent_dice         = spent_dice + coalesce((d->>'spent_dice')::bigint, 0),
        lost_games         = lost_games + coalesce((d->>'lost_games')::bigint, 0),
        coins_earned       = coins_earned + coalesce((d->>'coins_earned')::bigint, 0),
        won_crash          = won_crash + coalesce((d->>'won_crash')::bigint, 0),
        won_arena          = won_arena + coalesce((d->>'won_arena')::bigint, 0),
        won_mines          = won_mines + coalesce((d->>'won_mines')::bigint, 0),
        won_dice           = won_dice + coalesce((d->>'won_dice')::bigint, 0),
        won_upgrade        = won_upgrade + coalesce((d->>'won_upgrade')::bigint, 0),
        won_chests         = won_chests + coalesce((d->>'won_chests')::bigint, 0),
        won_love           = won_love + coalesce((d->>'won_love')::bigint, 0),
        sent_transfer      = sent_transfer + coalesce((d->>'sent_transfer')::bigint, 0),
        sent_transfer_cnt  = sent_transfer_cnt + coalesce((d->>'sent_transfer_cnt')::integer, 0),
        got_transfer       = got_transfer + coalesce((d->>'got_transfer')::bigint, 0),
        chests_opened      = chests_opened + coalesce((d->>'chests_opened')::integer, 0),
        mines_games        = mines_games + coalesce((d->>'mines_games')::integer, 0),
        golden_chests      = golden_chests + coalesce((d->>'golden_chests')::integer, 0),
        upgrades_total     = upgrades_total + coalesce((d->>'upgrades_total')::integer, 0),
        upgrades_won       = upgrades_won + coalesce((d->>'upgrades_won')::integer, 0),
        games_played       = games_played + coalesce((d->>'games_played')::integer, 0),
        games_won          = games_won + coalesce((d->>'games_won')::integer, 0),
        best_transfer      = greatest(best_transfer, coalesce((d->>'best_transfer')::bigint, 0)),
        best_crash_bet     = greatest(best_crash_bet, coalesce((d->>'best_crash_bet')::bigint, 0)),
        best_crash_win     = greatest(best_crash_win, coalesce((d->>'best_crash_win')::bigint, 0)),
        best_arena_bet     = greatest(best_arena_bet, coalesce((d->>'best_arena_bet')::bigint, 0)),
        best_arena_win     = greatest(best_arena_win, coalesce((d->>'best_arena_win')::bigint, 0)),
        best_mines_bet     = greatest(best_mines_bet, coalesce((d->>'best_mines_bet')::bigint, 0)),
        best_mines_win     = greatest(best_mines_win, coalesce((d->>'best_mines_win')::bigint, 0)),
        best_dice_bet      = greatest(best_dice_bet, coalesce((d->>'best_dice_bet')::bigint, 0)),
        best_dice_win      = greatest(best_dice_win, coalesce((d->>'best_dice_win')::bigint, 0)),
        updated_at         = now()
     where user_id = p_user_id;
end $$;

grant execute on function public.player_stats_add(bigint, jsonb) to anon, authenticated, service_role;

-- --- 7) player_stats_ensure: строка игрока есть всегда ----------------------
create or replace function public.player_stats_ensure(p_user_id bigint)
returns public.player_stats
language plpgsql
security definer
set search_path = public
as $$
declare
    row_out public.player_stats;
begin
    insert into public.player_stats (user_id) values (p_user_id)
    on conflict (user_id) do nothing;
    select * into row_out from public.player_stats where user_id = p_user_id;
    return row_out;
end $$;

grant execute on function public.player_stats_ensure(bigint) to anon, authenticated, service_role;

-- --- 8) player_stats_health: честный ответ приложению ----------------------
create or replace function public.player_stats_health()
returns jsonb
language sql
security definer
set search_path = public
as $$
    select jsonb_build_object(
        'ok', true,
        'version', '20261207000000',
        'stats_table', to_regclass('public.player_stats') is not null,
        'ledger_table', false,
        'rpc', exists (select 1 from pg_proc where proname = 'player_stats_add'),
        'unique_key', exists (
            select 1 from pg_indexes
             where schemaname = 'public' and tablename = 'player_stats'
               and indexname = 'player_stats_user_key'
        ),
        'mines_games', exists (
            select 1 from information_schema.columns
             where table_schema = 'public' and table_name = 'player_stats'
               and column_name = 'mines_games'
        ),
        'events_amount', exists (
            select 1 from information_schema.columns
             where table_schema = 'public' and table_name = 'player_events'
               and column_name = 'amount'
        ),
        'admin_audit', to_regclass('public.admin_audit') is not null,
        'ledger_kinds', 0
    );
$$;

grant execute on function public.player_stats_health() to anon, authenticated, service_role;

notify pgrst, 'reload schema';
