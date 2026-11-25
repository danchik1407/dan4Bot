-- ============================================================================
--  НОВОГОДНИЙ ЛИДЕРБОРД — ПОЛНАЯ МИГРАЦИЯ (одним файлом)
--  Версия: 20261211000000
--
--  Ничего до неё применять не нужно: файл сам создаёт таблицу, функцию,
--  права и политику. Применять поверх старой миграции тоже безопасно —
--  данные игроков не теряются (идемпотентно).
--
--  Что считаем: ЧИСТЫЙ доход игрока за акцию.
--    * победа в игре → выигрыш минус своя ставка (ставил 500, выиграл 2000 → 1500);
--    * проигрыш → 0, ничего не отнимаем;
--    * пассивный доход с капсул → целиком.
--
--  Где применять: Supabase → SQL Editor → вставить файл целиком → Run.
--  После Run должно быть «Success. No rows returned».
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Таблица: одна строка на игрока
-- ---------------------------------------------------------------------------
create table if not exists public.newyear_board (
    user_id        bigint primary key,
    earned         bigint  not null default 0,
    earned_games   bigint  not null default 0,
    earned_passive bigint  not null default 0,
    wins           integer not null default 0,
    updated_at     timestamptz not null default now()
);

-- Если таблица уже есть, но колонки не все — добавляем недостающие.
alter table public.newyear_board add column if not exists earned         bigint      not null default 0;
alter table public.newyear_board add column if not exists earned_games   bigint      not null default 0;
alter table public.newyear_board add column if not exists earned_passive bigint      not null default 0;
alter table public.newyear_board add column if not exists wins           integer     not null default 0;
alter table public.newyear_board add column if not exists updated_at     timestamptz not null default now();

create index if not exists newyear_board_earned_idx
    on public.newyear_board (earned desc);

-- ---------------------------------------------------------------------------
-- 2. Начисление: count = count + прибавка внутри одной операции
-- ---------------------------------------------------------------------------
create or replace function public.newyear_board_add(
    p_user_id bigint,
    p_games bigint default 0,
    p_passive bigint default 0,
    p_win boolean default false
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_games   bigint := greatest(0, coalesce(p_games, 0));
    v_passive bigint := greatest(0, coalesce(p_passive, 0));
    v_row public.newyear_board;
begin
    if p_user_id is null or (v_games = 0 and v_passive = 0) then
        select * into v_row from public.newyear_board where user_id = p_user_id;
        return jsonb_build_object(
            'ok', true,
            'earned', coalesce(v_row.earned, 0),
            'earned_games', coalesce(v_row.earned_games, 0),
            'earned_passive', coalesce(v_row.earned_passive, 0),
            'wins', coalesce(v_row.wins, 0)
        );
    end if;

    insert into public.newyear_board as b
        (user_id, earned, earned_games, earned_passive, wins, updated_at)
    values
        (p_user_id, v_games + v_passive, v_games, v_passive,
         case when p_win then 1 else 0 end, now())
    on conflict (user_id) do update set
        earned         = b.earned + excluded.earned,
        earned_games   = b.earned_games + excluded.earned_games,
        earned_passive = b.earned_passive + excluded.earned_passive,
        wins           = b.wins + excluded.wins,
        updated_at     = now()
    returning * into v_row;

    return jsonb_build_object(
        'ok', true,
        'earned', v_row.earned,
        'earned_games', v_row.earned_games,
        'earned_passive', v_row.earned_passive,
        'wins', v_row.wins
    );
end;
$$;

create or replace function public.newyear_board_health()
returns jsonb
language sql
security definer
set search_path = public
as $$
    select jsonb_build_object(
        'ok',      to_regclass('public.newyear_board') is not null
                   and to_regprocedure('public.newyear_board_add(bigint,bigint,bigint,boolean)') is not null,
        'version', '20261211000000',
        'table',   to_regclass('public.newyear_board') is not null,
        'rpc',     to_regprocedure('public.newyear_board_add(bigint,bigint,bigint,boolean)') is not null
    );
$$;

-- ---------------------------------------------------------------------------
-- 3. Самопроверка «таблица реально пишется»
--    Пишет строку user_id = 0 (нулевого Telegram ID не бывает) и сразу
--    удаляет — рейтинг не портится.
-- ---------------------------------------------------------------------------
create or replace function public.newyear_board_selftest()
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.newyear_board (user_id, earned, earned_games, earned_passive, wins, updated_at)
    values (0, 0, 0, 0, 0, now())
    on conflict (user_id) do update set updated_at = now();
    delete from public.newyear_board where user_id = 0;
    return jsonb_build_object('ok', true, 'version', '20261211000000');
exception when others then
    return jsonb_build_object('ok', false, 'error', sqlerrm);
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. Права и RLS
--    Частая причина «таблица есть, а записи нет» — включённый RLS без политики.
--    Включаем RLS и сразу даём разрешающую политику + права ролям Supabase.
--    Роли anon/authenticated/service_role есть в Supabase всегда, но файл
--    может прогнать и обычный PostgreSQL — там их нет, поэтому гранты выдаём
--    только существующим ролям.
-- ---------------------------------------------------------------------------
alter table public.newyear_board enable row level security;
drop policy if exists newyear_board_service_all on public.newyear_board;
create policy newyear_board_service_all on public.newyear_board
    for all using (true) with check (true);

do $$
declare
    r text;
begin
    foreach r in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('grant all on table public.newyear_board to %I', r);
            execute format(
                'grant execute on function public.newyear_board_add(bigint, bigint, bigint, boolean) to %I', r);
            execute format(
                'grant execute on function public.newyear_board_health() to %I', r);
            execute format(
                'grant execute on function public.newyear_board_selftest() to %I', r);
        end if;
    end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- 5. Чтобы PostgREST сразу увидел таблицу и функции
-- ---------------------------------------------------------------------------
notify pgrst, 'reload schema';

-- ---------------------------------------------------------------------------
-- 6. Итог применения (должна быть одна строка: table=true, rpc=true, write=ok)
-- ---------------------------------------------------------------------------
select 'лидерборд' as check_name,
       to_regclass('public.newyear_board') is not null as table_ok,
       to_regprocedure('public.newyear_board_add(bigint,bigint,bigint,boolean)') is not null as rpc_ok,
       (public.newyear_board_selftest()->>'ok') as write_ok;
