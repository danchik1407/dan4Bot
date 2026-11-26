-- ============================================================================
-- AURA GAME — ЗАДАЧА 105: ПОЛ В ПРОФИЛЕ + КУЛДАУН РП, КОТОРЫЙ НЕ ВЫКЛЮЧАЕТСЯ
--
-- Что лечит (со слов заказчика):
--   1) «пишет что обнимает саму себя, а я парень» — нужен пол игрока для
--      склонений в РП: «обнимает саму себя» / «обнимает самого себя» /
--      нейтральное «обнимает себя»;
--   2) «кулдаун в паре не работает, тестировал выключая и включая обратно
--      через админку — эффекта нет». В game_settings оставалось значение
--      «выключен» (love_cooldown_enabled = 0 / love_cooldown_seconds = off),
--      бот читал его из своего кэша и не ставил ожидание. Теперь КД РП —
--      жёсткое правило: 5 часов на все РП-команды, всегда, в паре и в
--      одиночке. Миграция перезаписывает старые значения на «включён».
--
-- Файл идемпотентный: применять можно сколько угодно раз.
-- Supabase → SQL Editor → New query → вставить ВЕСЬ файл → Run.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. ПОЛ ИГРОКА
--    Хранится в users.gender: 'female' | 'male' | NULL (не указан).
--    Значение ставит игрок сам в профиле Mini App (/api/user/gender),
--    бот читает ту же колонку.
-- ---------------------------------------------------------------------------
do $$
begin
    if to_regclass('public.users') is null then
        raise notice 'таблицы public.users нет — сначала примените полную схему';
        return;
    end if;
    alter table public.users add column if not exists gender text;
    raise notice 'users.gender готова';
end $$;

-- Старые/чужие значения приводим к двум допустимым (или NULL).
update public.users
   set gender = case
                    when lower(btrim(gender)) in ('female', 'f', 'ж', 'женский', 'жен', 'девушка', 'woman', 'girl')
                        then 'female'
                    when lower(btrim(gender)) in ('male', 'm', 'м', 'мужской', 'муж', 'парень', 'man', 'boy')
                        then 'male'
                    else null
                end
 where gender is not null
   and lower(btrim(gender)) not in ('female', 'male');

-- Права: колонка читается/пишется и сервисной ролью, и anon (аппка).
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'grant select, insert, update on public.users to anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'grant select, insert, update on public.users to authenticated';
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute 'grant select, insert, update on public.users to service_role';
    end if;
end $$;

-- ---------------------------------------------------------------------------
-- 2. КУЛДАУН РП: ВСЕГДА 5 ЧАСОВ
--    Пишем в game_settings «включён» и 18000 секунд — даже если раньше стояло
--    «выключен»/«off». Код (bot.py / api.py) это значение больше не
--    переключает, поэтому КД работает одинаково для всех.
-- ---------------------------------------------------------------------------
do $$
begin
    if to_regclass('public.game_settings') is null then
        return;
    end if;

    insert into public.game_settings (key, value, updated_at)
    values ('love_cooldown_enabled', '1', now())
    on conflict (key) do update set value = '1', updated_at = now();

    insert into public.game_settings (key, value, updated_at)
    values ('love_cooldown_seconds', '18000', now())
    on conflict (key) do update set value = '18000', updated_at = now();

    raise notice 'КД РП: 5 часов, всегда включён';
end $$;

-- ---------------------------------------------------------------------------
-- 3. ПРОВЕРКА (табличка в конце Run)
-- ---------------------------------------------------------------------------
select 'users.gender' as проверка,
       case when exists (
                select 1 from information_schema.columns
                 where table_schema = 'public' and table_name = 'users'
                   and column_name = 'gender')
            then 'ok — пол можно выбирать в профиле'
            else 'НЕТ — колонки нет, примените миграцию целиком' end as результат
union all
select 'КД РП включён',
       case when exists (
                select 1 from public.game_settings
                 where key = 'love_cooldown_enabled' and value = '1')
            then 'ok — 5 часов на любые РП'
            else 'НЕТ — значение не записалось' end;
