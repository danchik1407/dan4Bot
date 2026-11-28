-- ============================================================================
-- AURA GAME: задача 106 — ставка в журнале выплат арены.
--
-- Зачем: лидерборд «кто больше заработал» пишет ЧИСТЫЙ доход (выигрыш минус
-- своя ставка). Арена — единственная игра, где ставка терялась: план раунда её
-- знал, а журнал arena_payouts и очередь повторов — нет. Поэтому после
-- рестарта/доплаты посчитать «выплата − ставка» было нельзя, и в таблицу
-- уходила ВАЛОВАЯ выплата. Отсюда жалоба заказчика «в лидерборде все игры
-- считаются верно, но пвп даёт слишком много коинов за одну игру».
--
-- Что делает файл:
--   1) добавляет колонку bet в public.arena_payouts (если её ещё нет);
--   2) восстанавливает ставку у уже записанных строк по снимку раунда
--      (arena_rounds.state → players[].bet) — история не пересчитывается
--      задним числом «на глазок», берём ровно то, что было в раунде;
--   3) не даёт записать отрицательную ставку.
--
-- Файл ИДЕМПОТЕНТНЫЙ: применять можно повторно.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- После применения: NOTIFY pgrst, 'reload schema';
-- ============================================================================

-- --- 1) колонка ставки ---
alter table public.arena_payouts
    add column if not exists bet bigint not null default 0;

comment on column public.arena_payouts.bet is
    'Ставка игрока в этом раунде. Нужна лидерборду: доход = amount - bet.';

-- --- 2) восстановление ставки по снимку раунда ---
-- В arena_rounds.state лежит снимок раунда: {round_id, players: [{id, bet}]}.
-- Строку журнала и игрока сопоставляем по арене, id раунда и id игрока.
update public.arena_payouts as p
   set bet = coalesce((
        select (player.value ->> 'bet')::bigint
          from public.arena_rounds as r
          cross join lateral jsonb_array_elements(coalesce(r.state -> 'players', '[]'::jsonb)) as player
         where r.arena = p.arena
           and coalesce(r.state ->> 'round_id', r.round_id) = p.round_id
           and (player.value ->> 'id') = p.user_id::text
         limit 1), p.bet)
 where p.bet = 0;

-- --- 3) страховка от мусора ---
do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conname = 'arena_payouts_bet_non_negative'
           and conrelid = 'public.arena_payouts'::regclass
    ) then
        alter table public.arena_payouts
            add constraint arena_payouts_bet_non_negative check (bet >= 0);
    end if;
end $$;

-- Права и RLS не меняются: политика arena_payouts_all уже создана миграцией
-- 20261127000000_arena_rounds.sql.
notify pgrst, 'reload schema';
