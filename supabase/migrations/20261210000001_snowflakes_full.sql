-- ============================================================================
--  СНЕЖИНКИ — ПОЛНАЯ МИГРАЦИЯ (одним файлом, ничего до неё применять не нужно)
--  Версия: 20261210000001
--
--  Включает ВСЁ, что нужно снежинкам:
--    1) таблицу public.snowflakes (одна строка на игрока);
--    2) индекс для топа «кто сколько насобирал»;
--    3) функцию public.snowflakes_add() — атомарное прибавление;
--    4) функцию public.snowflakes_health() — проверка, что всё на месте;
--    5) права для anon / authenticated / service_role;
--    6) перезагрузку схемы PostgREST.
--
--  Зачем нужна атомарность (баг «перезагружаю — снежинки обнуляются»):
--  раньше сервер считал итог как «кэш (до 20 секунд) + пойманное» и писал
--  сумму целиком. Если в базе лежало 40, а кэш помнил 0 — запись ставила 5,
--  и накопленное ЗАТИРАЛОСЬ. Теперь прибавление идёт внутри одной операции:
--      count = count + p_amount
--  и вернуть новое значение, поэтому гонки между вкладками и кэш больше
--  ни на что не влияют.
--
--  Правила заказчика (задача 90): снежинки — чистая декорация. Их нельзя
--  передать, обменять или потратить, поэтому здесь ТОЛЬКО счётчик: ни
--  истории переводов, ни списаний, ни баланса «в минус».
--
--  Миграция ИДЕМПОТЕНТНА: можно применять повторно и поверх старых
--  снежиночных миграций (20261208000000 / 20261210000000) — данные не
--  теряются, ничего не падает.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Таблица: одна строка на игрока
-- ---------------------------------------------------------------------------
create table if not exists public.snowflakes (
    user_id     bigint primary key,
    count       integer not null default 0,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- Если таблица уже была создана старой миграцией без какой-то колонки —
-- добавляем недостающие, чтобы скрипт не падал на существующей базе.
alter table public.snowflakes add column if not exists count      integer     not null default 0;
alter table public.snowflakes add column if not exists created_at timestamptz not null default now();
alter table public.snowflakes add column if not exists updated_at timestamptz not null default now();

-- Счётчик не может быть отрицательным (на всякий случай — правило бизнеса).
alter table public.snowflakes drop constraint if exists snowflakes_count_nonneg;
alter table public.snowflakes
    add constraint snowflakes_count_nonneg check (count >= 0);

-- ---------------------------------------------------------------------------
-- 2. Индекс для топа
-- ---------------------------------------------------------------------------
create index if not exists snowflakes_count_idx
    on public.snowflakes (count desc);

-- ---------------------------------------------------------------------------
-- 3. Атомарное прибавление
--    Возвращает новое значение счётчика (одна строка, колонка count).
--    p_amount ограничивается сверху 5 — это тот же антифлуд, что и в API
--    (SNOWFLAKE_MAX_PER_CATCH): за одно нажатие больше пяти не начислить.
--    Игрока с id = null или с p_amount <= 0 функция не создаёт.
-- ---------------------------------------------------------------------------
create or replace function public.snowflakes_add(p_user_id bigint, p_amount integer)
returns table (count integer)
language plpgsql
security definer
set search_path = public
as $$
declare
    v_add   integer := greatest(0, least(5, coalesce(p_amount, 0)));
    v_total integer;
begin
    -- Нет игрока или прибавлять нечего — просто честно возвращаем текущее.
    if p_user_id is null or v_add = 0 then
        select coalesce(s.count, 0) into v_total
          from public.snowflakes s
         where s.user_id = p_user_id;
        return query select coalesce(v_total, 0);
        return;
    end if;

    insert into public.snowflakes as s (user_id, count, created_at, updated_at)
    values (p_user_id, v_add, now(), now())
    on conflict (user_id) do update
        set count      = s.count + v_add,
            updated_at = now()
    returning s.count into v_total;

    return query select v_total;
end;
$$;

comment on function public.snowflakes_add(bigint, integer) is
    'Атомарно прибавляет пойманные снежинки (0..5) и возвращает новый счётчик.';

-- ---------------------------------------------------------------------------
-- 4. Проверка «всё на месте» — приложение не молчит, если миграцию не применили
-- ---------------------------------------------------------------------------
create or replace function public.snowflakes_health()
returns jsonb
language sql
security definer
set search_path = public
as $$
    select jsonb_build_object(
        'ok',      to_regclass('public.snowflakes') is not null
                   and to_regprocedure('public.snowflakes_add(bigint,integer)') is not null,
        'version', '20261210000001',
        'table',   to_regclass('public.snowflakes') is not null,
        'rpc',     to_regprocedure('public.snowflakes_add(bigint,integer)') is not null
    );
$$;

-- ---------------------------------------------------------------------------
-- 5. Права
--    В Supabase роли anon / authenticated / service_role есть всегда. Но эту
--    миграцию может прогнать и обычный PostgreSQL (локальные тесты, свой
--    хост) — там таких ролей нет, и прямой `grant ... to anon` падает с
--    «role "anon" does not exist». Поэтому выдаём права только тем ролям,
--    которые реально существуют.
-- ---------------------------------------------------------------------------
do $$
declare
    r text;
begin
    foreach r in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format(
                'grant execute on function public.snowflakes_add(bigint, integer) to %I', r);
            execute format(
                'grant execute on function public.snowflakes_health() to %I', r);
            execute format(
                'grant select, insert, update on public.snowflakes to %I', r);
        end if;
    end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- 6. Чтобы PostgREST сразу увидел новую функцию и таблицу
-- ---------------------------------------------------------------------------
notify pgrst, 'reload schema';
