-- Задача 96: «перезагружаю — снежинки обнуляются».
--
-- Причина была не в вёрстке, а в записи: сервер считал итог как
--     current = кэш (до 20 секунд) ; total = current + 5
-- и писал total через upsert. Если в базе лежало 40, а кэш помнил 0, запись
-- ставила 5 — накопленное ЗАТИРАЛОСЬ. Плюс клиент терял ещё не отправленную
-- пачку при закрытии приложения (это исправлено в app.js).
--
-- Здесь атомарное прибавление: count = count + p_amount внутри одной операции.
-- Идемпотентно: можно применять повторно.

create table if not exists public.snowflakes (
    user_id     bigint primary key,
    count       integer not null default 0,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index if not exists snowflakes_count_idx
    on public.snowflakes (count desc);

-- Прибавить пойманные снежинки и вернуть новый счётчик.
-- Одна строка на игрока, вставка или инкремент — без гонок между вкладками.
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
    if p_user_id is null or v_add = 0 then
        select coalesce(s.count, 0) into v_total
          from public.snowflakes s where s.user_id = p_user_id;
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

grant execute on function public.snowflakes_add(bigint, integer)
    to anon, authenticated, service_role;

grant select, insert, update on public.snowflakes to anon, authenticated, service_role;

notify pgrst, 'reload schema';
