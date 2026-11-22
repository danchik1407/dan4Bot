-- ############################################################################
-- СТРАХОВКА users.id: код (апгрейд, магазин, бот) обращается к строке профиля
-- по автоинкрементному id. В старых базах этой колонки нет — PostgREST
-- отвечает PGRST204 «нет колонки id в users», и апгрейд падал целиком.
-- Здесь колонка появляется (или чинится), id уникален, значения заполнены.
-- ############################################################################
create or replace function public._aura_users_seq_sync() returns bigint
language plpgsql as $$
declare v_max bigint := 0;
begin
    -- Максимум по id безопасно: если в колонке мусор — начинаем с 1.
    begin
        execute 'select coalesce(max(id), 0)::bigint from public.users' into v_max;
    exception when others then
        v_max := 0;
    end;
    perform setval('public.users_id_seq', greatest(coalesce(v_max, 0), 1));
    return greatest(coalesce(v_max, 0), 1);
end $$;

do $$
declare
    v_type text;
    v_dup  integer;
begin
    if to_regclass('public.users') is null then
        return;                                   -- таблицы ещё нет: её создаст ЧАСТЬ 1
    end if;

    create sequence if not exists public.users_id_seq;

    select data_type into v_type
      from information_schema.columns
     where table_schema = 'public' and table_name = 'users' and column_name = 'id';

    if v_type is null then
        -- Колонки нет: добавляем с автоинкрементом. Существующие строки
        -- получат значения сами (default nextval срабатывает при ADD COLUMN).
        begin
            execute 'alter table public.users add column id bigint not null default nextval(''public.users_id_seq'')';
            perform public._aura_users_seq_sync();
            raise notice 'users.id: колонка добавлена (bigint, автоинкремент)';
        exception when others then
            raise notice 'users.id: не удалось добавить автоматически: %', sqlerrm;
        end;
    else
        -- Колонка есть, но тип не числовой: сначала гасим нечисловые значения,
        -- потом приводим тип. Так «abc» не роняет преобразование всей таблицы.
        if v_type not in ('bigint', 'integer', 'smallint') then
            begin
                execute 'update public.users set id = null where id is not null and id::text !~ ''^[0-9]+$''';
            exception when others then
                raise notice 'users.id: не удалось обнулить нечисловые значения: %', sqlerrm;
            end;
            begin
                execute 'alter table public.users alter column id type bigint using id::bigint';
                raise notice 'users.id: тип приведён к bigint (было %)', v_type;
            exception when others then
                raise notice 'users.id: тип оставлен как %: %', v_type, sqlerrm;
            end;
        end if;
    end if;

    -- Заполняем пустые id уникальными значениями.
    if exists (select 1 from public.users where id is null) then
        begin
            perform public._aura_users_seq_sync();
            update public.users set id = nextval('public.users_id_seq') where id is null;
            raise notice 'users.id: пустые значения заполнены';
        exception when others then
            raise notice 'users.id: не удалось заполнить пустые значения: %', sqlerrm;
        end;
    end if;

    -- Дубли id (бывает, когда колонку добавляли вручную): раздаём новые.
    select count(*) into v_dup
      from (select id from public.users group by id having count(*) > 1) as d;
    if v_dup > 0 then
        begin
            perform public._aura_users_seq_sync();
            with dups as (
                select ctid, row_number() over (partition by id order by ctid) as rn
                  from public.users
                 where id in (select id from public.users group by id having count(*) > 1)
            )
            update public.users u
               set id = nextval('public.users_id_seq')
              from dups
             where u.ctid = dups.ctid and dups.rn > 1;
            raise notice 'users.id: дубли разведены новыми значениями';
        exception when others then
            raise notice 'users.id: дубли не разведены: %', sqlerrm;
        end;
    end if;

    -- Уникальность нужна для записи «именно в эту строку» (.eq("id", row_id)).
    if not exists (
        select 1 from pg_indexes
         where schemaname = 'public' and tablename = 'users'
           and indexdef ilike '%unique%' and indexdef ilike '%(id)%'
    ) then
        begin
            execute 'create unique index if not exists users_id_key on public.users (id)';
            raise notice 'users.id: добавлен уникальный индекс';
        exception when others then
            raise notice 'users.id: уникальный индекс не добавлен: %', sqlerrm;
        end;
    end if;
end $$;

drop function if exists public._aura_users_seq_sync();
