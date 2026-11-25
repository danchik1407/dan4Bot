-- Задача 93: новогодний лидерборд «кто больше заработал».
--
-- Заказчик: «повышать планку и занимать первые места нужно активно играть в
-- разные режимы; за каждую победу в таблицу идёт ЧИСТЫЙ выигрыш — своя ставка
-- не засчитывается. Пассивный доход с капсул тоже засчитывается».
--
-- Считаем именно доход игрока, а не оборот:
--   * победа в игре → выигрыш минус ставка (ставил 500, выиграл 2000 → 1500);
--   * проигрыш → 0 (просто потерял коины);
--   * пассивный доход с капсул → полностью в таблицу.
-- Записывает всё api.py одной функцией newyear_board_add (атомарный UPDATE,
-- параллельные победы не теряют друг друга).
--
-- Миграция идемпотентная: можно запускать повторно.
-- Не применить — игра работает, лидерборд просто пустой (ошибок не будет).

create table if not exists public.newyear_board (
    user_id       bigint primary key,
    earned        bigint not null default 0,   -- чистыми, за всё время акции
    earned_games  bigint not null default 0,   -- из них с игр
    earned_passive bigint not null default 0,  -- из них с капсул
    wins          integer not null default 0,  -- сколько побед учтено
    updated_at    timestamptz not null default now()
);

create index if not exists newyear_board_earned_idx
    on public.newyear_board (earned desc);

-- Начисления. Отрицательные и нулевые прибавки игнорируем: в таблицу идёт
-- только чистый ДОХОД, проигрыш её не уменьшает.
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
    v_games bigint := greatest(0, coalesce(p_games, 0));
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

grant execute on function public.newyear_board_add(bigint, bigint, bigint, boolean)
    to anon, authenticated, service_role;

-- Проверка «миграция применена?» — как у остальных таблиц, чтобы приложение
-- могло честно сказать «лидерборд временно недоступен», а не показывать нули.
create or replace function public.newyear_board_health()
returns jsonb
language sql
security definer
set search_path = public
as $$
    select jsonb_build_object(
        'ok', to_regclass('public.newyear_board') is not null,
        'version', '20261209000000'
    );
$$;

grant execute on function public.newyear_board_health() to anon, authenticated, service_role;
grant select, insert, update on public.newyear_board to anon, authenticated, service_role;

notify pgrst, 'reload schema';
