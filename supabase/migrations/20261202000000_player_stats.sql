-- ############################################################################
-- Задача 74. Профиль: «игровые моменты» игрока.
--
-- Заказчик: «профиль — это профиль, карман — это карман». В профиле нужны
-- мелочи про самого игрока, а не про имущество:
--   • сколько коинов потратил за всё время и на что;
--   • лучший апгрейд (что во что превратил);
--   • самая большая ставка и самый большой выигрыш в ракете / арене / минах /
--     кубах;
--   • сколько выиграл и проиграл в играх;
--   • сколько вывел и получил переводами;
--   • сколько открыл сундуков и что из них выпало.
--
-- Часть чисел уже есть в журналах (upgrade_log, arena_payouts, shop_purchases,
-- chest_claims), но по ним нельзя ответить на «сколько потратил на апгрейды»:
-- в upgrade_log нет цены проигранной ставки, а по арене нет размера ставки.
-- Поэтому добавляем:
--   player_stats   — счётчики игрока (одна строка на игрока), обновляются
--                    только прибавлением, читаются одним запросом;
--   player_events  — короткая лента событий (что во что улучшил, кому и
--                    сколько перевёл), из неё берутся «лучшие моменты».
--
-- Файл ИДЕМПОТЕНТНЫЙ: применять можно повторно.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- После применения: NOTIFY pgrst, 'reload schema';
-- ############################################################################

-- --- 1) Счётчики игрока ------------------------------------------------------
create table if not exists public.player_stats (
    user_id           bigint primary key,
    -- Расходы: всё, что ушло из кошелька, и разбивка «на что».
    coins_spent       bigint not null default 0,
    spent_upgrade     bigint not null default 0,   -- апгрейды (ценность ставки)
    spent_shop        bigint not null default 0,   -- магазин
    spent_crash       bigint not null default 0,   -- ракета (ставки)
    spent_arena       bigint not null default 0,   -- арена (ставки)
    spent_mines       bigint not null default 0,   -- мины (ставки)
    spent_dice        bigint not null default 0,   -- кубы (ставки)
    lost_games        bigint not null default 0,   -- проигранные ставки всего
    -- Доходы и победы.
    coins_earned      bigint not null default 0,   -- выигрыши в играх + призы
    won_crash         bigint not null default 0,
    won_arena         bigint not null default 0,
    won_mines         bigint not null default 0,
    won_dice          bigint not null default 0,
    won_upgrade       bigint not null default 0,   -- ценность выигранных апгрейдов
    won_chests        bigint not null default 0,   -- что выпало из сундуков
    won_love          bigint not null default 0,   -- награды этапов отношений
    -- Переводы между игроками.
    sent_transfer     bigint not null default 0,
    sent_transfer_cnt integer not null default 0,
    got_transfer      bigint not null default 0,
    best_transfer     bigint not null default 0,
    -- Рекорды по ставкам и выигрышам.
    best_crash_bet    bigint not null default 0,
    best_crash_win    bigint not null default 0,
    best_arena_bet    bigint not null default 0,
    best_arena_win    bigint not null default 0,
    best_mines_bet    bigint not null default 0,
    best_mines_win    bigint not null default 0,
    best_dice_bet     bigint not null default 0,
    best_dice_win     bigint not null default 0,
    -- Счётчики.
    chests_opened     integer not null default 0,
    golden_chests     integer not null default 0,
    upgrades_total    integer not null default 0,
    upgrades_won      integer not null default 0,
    games_played      integer not null default 0,
    games_won         integer not null default 0,
    updated_at        timestamptz not null default now()
);

alter table public.player_stats enable row level security;
drop policy if exists player_stats_all on public.player_stats;
create policy player_stats_all on public.player_stats
    for all to anon, authenticated, service_role
    using (true) with check (true);

-- --- 2) Лента событий: лучшие моменты ---------------------------------------
-- kind: 'upgrade' (что во что), 'transfer' (кому и сколько), 'prize' (за что).
create table if not exists public.player_events (
    id         bigserial primary key,
    user_id    bigint not null,
    kind       text   not null,
    title      text   not null default '',
    amount     bigint not null default 0,   -- ценность события в коинах
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

-- Держим таблицу маленькой: на игрока только 20 последних событий.
delete from public.player_events
 where id in (
    select id from (
        select id, row_number() over (partition by user_id order by id desc) as rn
          from public.player_events
    ) ranked where rn > 20
 );

-- --- 3) Атомарное прибавление счётчиков --------------------------------------
-- Клиент и бот пишут только через эту функцию: несколько параллельных побед
-- (например, выплата арены во время покупки) не теряют друг друга, как это
-- было бы с «прочитать → прибавить → записать». Отсутствующее поле в jsonb
-- считается нулём, поэтому вызывать можно с любым набором счётчиков.
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

notify pgrst, 'reload schema';
