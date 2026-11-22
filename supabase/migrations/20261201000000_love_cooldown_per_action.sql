-- ############################################################################
-- Задача 61. Кулдаун отношений — НА КОМАНДУ.
--
-- Было: таблица love_cooldowns (user_id — первичный ключ, action_at) хранила
-- ОДНУ отметку на игрока. Поэтому «обнять» и «поцеловать» считались одним
-- действием: получил +10 за объятие — и поцелуй уже «на кулдауне».
--
-- Стало: отметка на пару (игрок + действие). Старая строка не выбрасывается и
-- не мешает: её действие помечается звёздочкой '*all' — это «общая» отметка,
-- которую код читает как «последний раз игрок получал награду за любое
-- действие ДО перехода на новую схему». Так игроки не получают награду
-- повторно сразу после миграции.
--
-- Файл идемпотентный: его можно применять повторно, данные не теряются.
-- ############################################################################

-- ЧАСТЬ 1. Новая колонка action + перенос старых отметок в '*all'
do $$
begin
    if to_regclass('public.love_cooldowns') is null then
        create table public.love_cooldowns (
            user_id   bigint,
            action    text,
            action_at timestamptz default now()
        );
    end if;
end $$;

alter table public.love_cooldowns add column if not exists action text;
alter table public.love_cooldowns add column if not exists action_at timestamptz default now();

-- Пустое действие = старая строка (одна на игрока) → '*all'.
update public.love_cooldowns
   set action = '*all'
 where action is null or btrim(action) = '';

alter table public.love_cooldowns alter column action set default '*all';
alter table public.love_cooldowns alter column action_at set default now();

-- Дыры в данных: без user_id/времени строка бесполезна.
delete from public.love_cooldowns where user_id is null;
update public.love_cooldowns set action_at = now() where action_at is null;

-- ЧАСТЬ 2. Первичный ключ: было (user_id), нужно (user_id, action)
do $$
declare
    v_pk_cols text;
begin
    select coalesce(string_agg(a.attname, ',' order by a.attname), '')
      into v_pk_cols
      from pg_constraint c
      join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
     where c.conrelid = 'public.love_cooldowns'::regclass
       and c.contype = 'p';

    -- Уже правильный ключ — ничего не делаем (повторный прогон).
    if v_pk_cols = 'action,user_id' then
        return;
    end if;

    -- Дубли по (user_id, action) невозможны на старом ключе, но на всякий
    -- случай оставляем самую свежую отметку.
    delete from public.love_cooldowns d
     using public.love_cooldowns newer
     where d.ctid <> newer.ctid
       and d.user_id = newer.user_id
       and d.action = newer.action
       and d.action_at < newer.action_at;

    execute 'alter table public.love_cooldowns drop constraint if exists love_cooldowns_pkey';
    execute 'alter table public.love_cooldowns add constraint love_cooldowns_pkey primary key (user_id, action)';

    if exists (select 1 from information_schema.columns
                where table_schema = 'public' and table_name = 'love_cooldowns'
                  and column_name = 'action' and is_nullable = 'YES') then
        execute 'alter table public.love_cooldowns alter column action set not null';
    end if;
exception
    when others then
        -- Не роняем миграцию из-за ключа: код умеет жить и без него
        -- (upsert тогда не используется, отметка пишется update+insert).
        raise notice 'love_cooldowns: ключ (user_id, action) не поставлен: %', sqlerrm;
end $$;

create index if not exists love_cooldowns_user_idx on public.love_cooldowns (user_id);

-- ЧАСТЬ 3. Права и RLS (как у остальных таблиц проекта)
do $$
begin
    if to_regclass('public.love_cooldowns') is null then
        return;
    end if;
    execute 'alter table public.love_cooldowns enable row level security';
    execute 'drop policy if exists love_cooldowns_service_all on public.love_cooldowns';
    execute 'drop policy if exists love_cooldowns_all on public.love_cooldowns';
    execute 'create policy love_cooldowns_service_all on public.love_cooldowns '
            'for all to anon, authenticated, service_role using (true) with check (true)';
    execute 'grant all on table public.love_cooldowns to anon, authenticated, service_role';
exception
    when others then
        raise notice 'love_cooldowns: политики не поставлены: %', sqlerrm;
end $$;

-- ЧАСТЬ 4. Значения кулдаунов в game_settings не трогаем, только сообщаем:
--   love_cooldown_seconds        — общий кулдаун (секунды), его ставит `кд 5ч`
--   love_cooldown_actions        — JSON персональных кулдаунов, его ставит
--                                  `кд обнять 1ч`
insert into public.game_settings (key, value)
values ('love_cooldown_once_note', 'per-action cooldowns enabled')
on conflict (key) do nothing;

-- ЧАСТЬ 5. Кэш схемы PostgREST: без этого ещё минуты летит PGRST204
notify pgrst, 'reload schema';

-- ЧАСТЬ 6. Проверка: должно быть «ИТОГ | ok»
with problems as (
    select 'нет таблицы: love_cooldowns' as what
     where to_regclass('public.love_cooldowns') is null
    union all
    select 'нет колонки: love_cooldowns.action'
     where not exists (select 1 from information_schema.columns
                        where table_schema = 'public' and table_name = 'love_cooldowns'
                          and column_name = 'action')
    union all
    select 'нет колонки: love_cooldowns.action_at'
     where not exists (select 1 from information_schema.columns
                        where table_schema = 'public' and table_name = 'love_cooldowns'
                          and column_name = 'action_at')
    union all
    select 'нет политики: love_cooldowns'
     where not exists (select 1 from pg_policies
                        where schemaname = 'public' and tablename = 'love_cooldowns')
)
select coalesce((select string_agg(what, '; ') from problems),
                'ИТОГ | ok — кулдаун теперь на каждую РП-команду') as result;
