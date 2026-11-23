-- ############################################################################
-- ЗАДАЧА 85: донат должен зачисляться сразу и не теряться.
--
-- Причина «оплатил — и ничего»: начисление делал только бот в обработчике
-- successful_payment. Если бот спал/перезапускался или апдейт терялся, деньги
-- не приходили, а игрок видел «напиши администратору». Теперь API сверяет
-- операции Telegram Stars (getStarTransactions) с этой таблицей и досчитывает
-- пропущенное — но только если таблица ведёт себя как надо:
--   • charge_id уникален (иначе один платёж мог записаться дважды);
--   • колонки stars/created_at есть (по сумме звёзд восстанавливается пакет);
--   • RLS не мешает сервисному ключу писать и читать.
--
-- Миграция безопасна для повторного запуска.
-- ############################################################################

create table if not exists public.stars_payments (
    id         bigserial   primary key,
    user_id    bigint      not null default 0,
    charge_id  text,
    coins      integer     not null default 0,
    stars      integer     not null default 0,
    created_at timestamptz not null default now()
);

alter table public.stars_payments add column if not exists user_id    bigint;
alter table public.stars_payments add column if not exists charge_id  text;
alter table public.stars_payments add column if not exists coins      integer default 0;
alter table public.stars_payments add column if not exists stars      integer default 0;
alter table public.stars_payments add column if not exists created_at timestamptz default now();

update public.stars_payments set coins = 0 where coins is null;
update public.stars_payments set stars = 0 where stars is null;
update public.stars_payments set created_at = now() where created_at is null;
alter table public.stars_payments alter column coins      set default 0;
alter table public.stars_payments alter column stars      set default 0;
alter table public.stars_payments alter column created_at set default now();

-- Один charge_id = один платёж. Дубли (если уже накопились) сначала убираем,
-- оставляя самую раннюю запись.
delete from public.stars_payments a
 using public.stars_payments b
 where a.charge_id is not null
   and a.charge_id = b.charge_id
   and (a.created_at, a.id) > (b.created_at, b.id);

create unique index if not exists stars_payments_charge_key
    on public.stars_payments (charge_id)
 where charge_id is not null;

create index if not exists stars_payments_user_idx
    on public.stars_payments (user_id);

-- Права и RLS: сервисный ключ обходит RLS, но если политики нет — код получит
-- «42501 row-level security policy» и платёж не запишется.
do $$
begin
    if to_regclass('public.stars_payments') is not null then
        execute 'alter table public.stars_payments enable row level security';
        execute 'drop policy if exists stars_payments_service_all on public.stars_payments';
        execute 'create policy stars_payments_service_all on public.stars_payments '
                'for all using (true) with check (true)';
    end if;
exception when others then
    raise notice 'stars_payments: политику создать не удалось: %', sqlerrm;
end $$;

grant all on public.stars_payments to service_role;
grant all on sequence public.stars_payments_id_seq to service_role;

-- Проверка (должно вернуть 1 строку с уникальным индексом):
--   select indexname from pg_indexes
--    where tablename = 'stars_payments' and indexname = 'stars_payments_charge_key';
