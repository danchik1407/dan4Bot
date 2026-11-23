-- ############################################################################
-- Задача 79. «Игры не засчитываются» + кнопка КД РП в админ-панели.
--
-- 1) ПОЧЕМУ игры не попадали в профиль и историю.
--    Счётчики профиля (player_stats) — отдельная таблица, которую создаёт
--    миграция 20261202000000_player_stats.sql. Если её не применить, api.py
--    пишет только debug-лог и молча живёт дальше: игра идёт, деньги
--    начисляются, а в профиле ноль. Здесь мы:
--      • создаём таблицы (player_stats, player_events), если их почему-то нет;
--      • ДОБАВЛЯЕМ недостающие колонки, если таблица была создана старой
--        версией (например, до счётчика «каток в мины»);
--      • заново объявляем функции player_stats_add и player_stats_ensure —
--        теперь ошибка базы НЕ проглатывается, а приходит игроку понятным
--        текстом «статистика недоступна»;
--      • добавляем player_stats_health, чтобы приложение могло честно сказать
--        «не Applied», а не показывать нули.
--
-- 2) КД РП (5 часов / 0 секунд) — переключатель админа.
--    Значение живёт в game_settings (ключ love_cooldown_enabled), потому что
--    его читают ОБА сервиса: бот (начисляет награду) и api (отдаёт состояние
--    в Mini App). Строку создаём сразу: по умолчанию КД ВКЛЮЧЁН.
--
-- Файл ИДЕМПОТЕНТНЫЙ: применять можно повторно, ничего не потеряется.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- После применения: NOTIFY pgrst, 'reload schema';
-- ############################################################################

-- --- 0) game_settings: тумблер КД РП -----------------------------------------
create table if not exists public.game_settings (
    key        text primary key,
    value      text,
    updated_at timestamptz not null default now()
);

insert into public.game_settings (key, value)
values ('love_cooldown_enabled', '1')
on conflict (key) do nothing;

-- Если в базе осталась старая метка «КД выключен» от команды `кд`, переносим её
-- в новый ключ, чтобы поведение не изменилось молча.
update public.game_settings
   set value = '0', updated_at = now()
 where key = 'love_cooldown_enabled'
   and value = '1'
   and exists (select 1 from public.game_settings
                where key = 'love_cooldown_seconds'
                  and lower(trim(value)) in ('off', '0', 'выкл'));

-- --- 1) player_stats: счётчики профиля ---------------------------------------
create table if not exists public.player_stats (
    user_id           bigint primary key,
    coins_spent       bigint not null default 0,
    spent_upgrade     bigint not null default 0,
    spent_shop        bigint not null default 0,
    spent_crash       bigint not null default 0,
    spent_arena       bigint not null default 0,
    spent_mines       bigint not null default 0,
    spent_dice        bigint not null default 0,
    lost_games        bigint not null default 0,
    coins_earned      bigint not null default 0,
    won_crash         bigint not null default 0,
    won_arena         bigint not null default 0,
    won_mines         bigint not null default 0,
    won_dice          bigint not null default 0,
    won_upgrade       bigint not null default 0,
    won_chests        bigint not null default 0,
    won_love          bigint not null default 0,
    sent_transfer     bigint not null default 0,
    sent_transfer_cnt integer not null default 0,
    got_transfer      bigint not null default 0,
    best_transfer     bigint not null default 0,
    best_crash_bet    bigint not null default 0,
    best_crash_win    bigint not null default 0,
    best_arena_bet    bigint not null default 0,
    best_arena_win    bigint not null default 0,
    best_mines_bet    bigint not null default 0,
    best_mines_win    bigint not null default 0,
    best_dice_bet     bigint not null default 0,
    best_dice_win     bigint not null default 0,
    chests_opened     integer not null default 0,
    mines_games       integer not null default 0,
    golden_chests     integer not null default 0,
    upgrades_total    integer not null default 0,
    upgrades_won      integer not null default 0,
    games_played      integer not null default 0,
    games_won         integer not null default 0,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

-- Таблица могла быть создана старой версией миграции — досыпаем колонки.
alter table public.player_stats add column if not exists coins_spent       bigint not null default 0;
alter table public.player_stats add column if not exists spent_upgrade     bigint not null default 0;
alter table public.player_stats add column if not exists spent_shop        bigint not null default 0;
alter table public.player_stats add column if not exists spent_crash       bigint not null default 0;
alter table public.player_stats add column if not exists spent_arena       bigint not null default 0;
alter table public.player_stats add column if not exists spent_mines       bigint not null default 0;
alter table public.player_stats add column if not exists spent_dice        bigint not null default 0;
alter table public.player_stats add column if not exists lost_games        bigint not null default 0;
alter table public.player_stats add column if not exists coins_earned      bigint not null default 0;
alter table public.player_stats add column if not exists won_crash         bigint not null default 0;
alter table public.player_stats add column if not exists won_arena         bigint not null default 0;
alter table public.player_stats add column if not exists won_mines         bigint not null default 0;
alter table public.player_stats add column if not exists won_dice          bigint not null default 0;
alter table public.player_stats add column if not exists won_upgrade       bigint not null default 0;
alter table public.player_stats add column if not exists won_chests        bigint not null default 0;
alter table public.player_stats add column if not exists won_love          bigint not null default 0;
alter table public.player_stats add column if not exists sent_transfer     bigint not null default 0;
alter table public.player_stats add column if not exists sent_transfer_cnt integer not null default 0;
alter table public.player_stats add column if not exists got_transfer      bigint not null default 0;
alter table public.player_stats add column if not exists best_transfer     bigint not null default 0;
alter table public.player_stats add column if not exists best_crash_bet    bigint not null default 0;
alter table public.player_stats add column if not exists best_crash_win    bigint not null default 0;
alter table public.player_stats add column if not exists best_arena_bet    bigint not null default 0;
alter table public.player_stats add column if not exists best_arena_win    bigint not null default 0;
alter table public.player_stats add column if not exists best_mines_bet    bigint not null default 0;
alter table public.player_stats add column if not exists best_mines_win    bigint not null default 0;
alter table public.player_stats add column if not exists best_dice_bet     bigint not null default 0;
alter table public.player_stats add column if not exists best_dice_win     bigint not null default 0;
alter table public.player_stats add column if not exists chests_opened     integer not null default 0;
alter table public.player_stats add column if not exists mines_games       integer not null default 0;
alter table public.player_stats add column if not exists golden_chests     integer not null default 0;
alter table public.player_stats add column if not exists upgrades_total    integer not null default 0;
alter table public.player_stats add column if not exists upgrades_won      integer not null default 0;
alter table public.player_stats add column if not exists games_played      integer not null default 0;
alter table public.player_stats add column if not exists games_won         integer not null default 0;
alter table public.player_stats add column if not exists created_at        timestamptz not null default now();
alter table public.player_stats add column if not exists updated_at        timestamptz not null default now();

alter table public.player_stats enable row level security;
drop policy if exists player_stats_all on public.player_stats;
create policy player_stats_all on public.player_stats
    for all to anon, authenticated, service_role
    using (true) with check (true);

-- --- 2) player_events: «лучшие моменты» --------------------------------------
create table if not exists public.player_events (
    id         bigserial primary key,
    user_id    bigint not null,
    kind       text   not null,
    title      text   not null default '',
    amount     bigint not null default 0,
    meta       jsonb  not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists player_events_user_idx
    on public.player_events (user_id, kind, amount desc);

alter table public.player_events enable row level security;
drop policy if exists player_events_all on public.player_events;
create policy player_events_all on public.player_events
    for all to anon, authenticated, service_role
    using (true) with check (true);

delete from public.player_events
 where id in (
    select id from (
        select id, row_number() over (partition by user_id order by id desc) as rn
          from public.player_events
    ) ranked where rn > 20
 );

-- --- 3) player_ledger: УДАЛЕНО (задача 80) -----------------------------------
-- Заказчик: «историю ставок и пополнений убери полностью». Таблица больше не
-- создаётся и не пишется: подтверждение пополнения приходит сообщением в личку
-- с ботом. Если таблица осталась от прошлых версий, она просто не используется;
-- удалять её не нужно (данные никому не мешают).
drop function if exists public.player_ledger_trim();

-- --- 4) player_stats_add: атомарное прибавление ------------------------------
-- Полный список колонок. Если добавляете новый счётчик — допишите и сюда, и в
-- player_stats (иначе значение молча не запишется).
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

-- --- 5) player_stats_ensure: «проверь себя» ----------------------------------
-- Возвращает строку счётчиков игрока, создавая её при первом обращении. Ошибка
-- здесь НЕ проглатывается: api.py показывает игроку «статистика недоступна»,
-- а не рисует нули. Это и есть лечение «игры не засчитываются».
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

-- --- 6) health-check для приложения ------------------------------------------
-- Приложение зовёт это, чтобы честно сказать «статистика не применяется»,
-- вместо пустых достижений.
create or replace function public.player_stats_health()
returns jsonb
language sql
security definer
set search_path = public
as $$
    select jsonb_build_object(
        'ok', true,
        'version', '20261204000000',
        'stats_table', to_regclass('public.player_stats') is not null,
        'ledger_table', false,   -- задача 80: история удалена, поле оставлено
                                 -- для совместимости со старой сборкой API
        'rpc', exists (select 1 from pg_proc where proname = 'player_stats_add'),
        'mines_games', exists (
            select 1 from information_schema.columns
             where table_schema = 'public' and table_name = 'player_stats'
               and column_name = 'mines_games'
        ),
        'ledger_kinds', 0       -- задача 80: истории больше нет
    );
$$;

grant execute on function public.player_stats_health() to anon, authenticated, service_role;

notify pgrst, 'reload schema';
