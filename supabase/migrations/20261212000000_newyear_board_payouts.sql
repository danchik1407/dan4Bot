-- ============================================================================
--  ПРИЗЫ НОВОГОДНЕГО ЛИДЕРБОРДА — АВТОВЫПЛАТА ПО ФИНИШУ
--  Версия: 20261212000000
--
--  Заказчик: «после таймера система сама должна выдать на аккаунт каждому
--  приз и сообщить в личных сообщениях — ты выиграл, занял такое-то место,
--  вот твой приз».
--
--  Таблица хранит, кому и сколько уже выдали. Один игрок — одна строка,
--  поэтому повторный запуск (рестарт сервиса, ручная кнопка в админке)
--  НИКОГДА не начисляет приз дважды.
--
--  status:  pending — выплата начата; paid — коины начислены;
--  notified: отправлено ли личное сообщение (если Telegram был недоступен,
--  сервис допишет на следующем проходе).
--
--  Применять: Supabase → SQL Editor. Файл идемпотентный.
-- ============================================================================

create table if not exists public.newyear_board_payouts (
    user_id    bigint primary key,
    place      integer not null default 0,
    prize      bigint  not null default 0,
    status     text    not null default 'pending',
    notified   boolean not null default false,
    paid_at    timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists newyear_board_payouts_place_idx
    on public.newyear_board_payouts (place);

create index if not exists newyear_board_payouts_status_idx
    on public.newyear_board_payouts (status);

-- Права и политика: та же история, что с newyear_board — «таблица есть,
-- а записи нет» из-за RLS без политики.
alter table public.newyear_board_payouts enable row level security;
drop policy if exists newyear_board_payouts_service_all on public.newyear_board_payouts;
create policy newyear_board_payouts_service_all on public.newyear_board_payouts
    for all using (true) with check (true);

do $$
declare
    r text;
begin
    foreach r in array array['anon', 'authenticated', 'service_role'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('grant all on table public.newyear_board_payouts to %I', r);
        end if;
    end loop;
end
$$;

notify pgrst, 'reload schema';

-- Итог применения: table_ok = true
select 'призы лидерборда' as check_name,
       to_regclass('public.newyear_board_payouts') is not null as table_ok,
       (select count(*) from public.newyear_board_payouts) as rows_now;
