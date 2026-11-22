-- ############################################################################
-- ЧАСТЬ 0. ТИПЫ КОЛОНОК: старая база могла держать created_at как BIGINT
--          (эпоха), coins как TEXT и т.п. Тогда любая запись/дефолт падает:
--          «column "created_at" is of type bigint but expression is of type
--          timestamp with time zone». Здесь типы приводятся к рабочим.
--          Старые значения НЕ удаляются: колонка переименовывается в
--          <имя>_legacy, а в новую переносится то, что читается (эпоха → дата,
--          «750» → 750). Если перенести нельзя — остаётся дефолт.
-- ############################################################################
create or replace function public._aura_fix_column(
    p_table   text,
    p_col     text,
    p_want    text,
    p_default text default null
) returns void
language plpgsql
as $$
declare
    v_type    text;
    v_ok      boolean;
    v_legacy  text;
    v_n       integer := 0;
    v_moved   boolean;
    v_rec     record;
    v_base    text := split_part(p_want, '(', 1);
begin
    if to_regclass('public.' || p_table) is null then
        return;                                   -- таблицы ещё нет: её создаст ЧАСТЬ 1
    end if;

    select data_type into v_type
      from information_schema.columns
     where table_schema = 'public' and table_name = p_table and column_name = p_col;

    if v_type is null then                        -- колонки нет — добавляем сразу нужного типа
        execute format('alter table public.%I add column %I %s %s',
                       p_table, p_col, p_want,
                       case when p_default is null then '' else 'default ' || p_default end);
        return;
    end if;

    -- Совместимые типы не трогаем (int → bigint, varchar → text и т.п.).
    v_ok := case
        when p_want like 'timestamptz%' then v_type in ('timestamp with time zone',
                                                        'timestamp without time zone', 'date')
        when p_want like 'bigint%'      then v_type in ('bigint', 'integer', 'smallint', 'numeric')
        when p_want like 'integer%'     then v_type in ('integer', 'smallint', 'bigint', 'numeric')
        when p_want like 'numeric%'     then v_type in ('numeric', 'integer', 'bigint',
                                                       'smallint', 'real', 'double precision')
        when p_want like 'boolean%'     then v_type = 'boolean'
        when p_want like 'jsonb%'       then v_type in ('jsonb', 'json')
        when p_want like 'text%'        then v_type in ('text', 'character varying', 'character')
        else false end;
    if v_ok then
        return;
    end if;

    -- Несовместимый тип: старое сохраняем рядом, колонку делаем рабочей.
    v_legacy := p_col || '_legacy';
    while exists (select 1 from information_schema.columns
                   where table_schema = 'public' and table_name = p_table
                     and column_name = v_legacy) loop
        v_n := v_n + 1;
        v_legacy := p_col || '_legacy' || v_n;
    end loop;

    execute format('alter table public.%I rename column %I to %I', p_table, p_col, v_legacy);
    execute format('alter table public.%I add column %I %s %s',
                   p_table, p_col, p_want,
                   case when p_default is null then '' else 'default ' || p_default end);

    -- Переносим значения ПОСТРОЧНО: одна «плохая» строка (например coins =
    -- 'not money') не должна откатывать перенос у остальных игроков.
    for v_rec in execute format('select ctid as c, %I::text as v from public.%I where %I is not null',
                                v_legacy, p_table, v_legacy)
    loop
        v_moved := false;
        begin
            -- 1) прямая попытка: «750»::bigint, '2026-10-01…'::timestamptz, '[]'::jsonb
            execute format('update public.%I set %I = %L::%s where ctid = %L',
                           p_table, p_col, v_rec.v, v_base, v_rec.c::text);
            v_moved := true;
        exception when others then
            v_moved := false;
        end;

        if not v_moved and p_want like 'timestamptz%'
           and v_rec.v ~ '^-?[0-9]+(\.[0-9]+)?$' then
            -- 2) время, записанное числом: эпоха в секундах или миллисекундах
            begin
                execute format(
                    'update public.%I set %I = to_timestamp(case when abs(%L::numeric) > 100000000000 '
                    'then %L::numeric / 1000 else %L::numeric end) where ctid = %L',
                    p_table, p_col, v_rec.v, v_rec.v, v_rec.v, v_rec.c::text);
                v_moved := true;
            exception when others then
                v_moved := false;
            end;
        end if;

        if not v_moved and p_want like 'jsonb%' and btrim(v_rec.v) <> '' then
            -- 3) старый инвентарь мог быть строкой «capsule» или «capsule,capsule»
            begin
                execute format('update public.%I set %I = %L::jsonb where ctid = %L',
                               p_table, p_col,
                               '[' || (select string_agg(to_jsonb(btrim(x))::text, ',')
                                         from unnest(string_to_array(v_rec.v, ',')) as x) || ']',
                               v_rec.c::text);
                v_moved := true;
            exception when others then
                v_moved := false;
            end;
        end if;
        -- не перенеслось — остаётся дефолт, старое значение цело в <колонка>_legacy
    end loop;

    raise notice 'тип исправлен: public.%.% % → % (старые значения в %)',
                 p_table, p_col, v_type, p_want, v_legacy;
end;
$$;

do $$
declare rec record;
begin
    for rec in
        select * from (values
            ('users', 'username',        'text',        null),
            ('users', 'chat_id',         'bigint',      '0'),
            ('users', 'coins',           'bigint',      '0'),
            ('users', 'keys',            'integer',     '0'),
            ('users', 'inventory',       'jsonb',       '''[]''::jsonb'),
            ('users', 'last_chest',      'bigint',      '0'),
            ('users', 'created_at',      'timestamptz', 'now()'),
            ('users', 'photo_url',       'text',        null),
            ('users', 'ice',             'integer',     '0'),
            ('users', 'premium',         'boolean',     'false'),
            ('users', 'daily_buys',      'text',        '''{}'''),
            ('users', 'last_shop_reset', 'bigint',      '0'),

            ('chat_warns', 'warns',       'integer',     '0'),
            ('chat_warns', 'muted_until', 'timestamptz', null),
            ('chat_warns', 'updated_at',  'timestamptz', 'now()'),

            ('relationships', 'user_a',     'bigint',      null),
            ('relationships', 'user_b',     'bigint',      null),
            ('relationships', 'love',       'integer',     '0'),
            ('relationships', 'bank',       'integer',     '0'),
            ('relationships', 'status',     'text',        '''active'''),
            ('relationships', 'created_at', 'timestamptz', 'now()'),
            ('relationships', 'updated_at', 'timestamptz', 'now()'),

            ('solo_love', 'love',       'integer',     '0'),
            ('solo_love', 'created_at', 'timestamptz', 'now()'),
            ('solo_love', 'updated_at', 'timestamptz', 'now()'),

            ('love_stage_rewards',        'paid_at',   'timestamptz', 'now()'),
            ('solo_love_stage_rewards',   'paid_at',   'timestamptz', 'now()'),
            ('love_cooldowns',            'action_at', 'timestamptz', 'now()'),
            ('passive_income',            'last_at',   'timestamptz', 'now()'),
            ('passive_carry',             'carry',     'numeric(10,6)', '0'),
            ('app_first_seen',            'first_seen','timestamptz', 'now()'),

            ('subscriptions_history', 'bonus_claimed', 'boolean',     'true'),
            ('subscriptions_history', 'reward',        'integer',     '200'),
            ('subscriptions_history', 'created_at',    'timestamptz', 'now()'),

            ('referrals', 'referrer_id', 'bigint',      null),
            ('referrals', 'reward',      'integer',     '0'),
            ('referrals', 'status',      'text',        '''paid'''),
            ('referrals', 'created_at',  'timestamptz', 'now()'),
            ('referrals', 'paid_at',     'timestamptz', null),

            ('stars_payments', 'user_id',    'bigint',      null),
            ('stars_payments', 'charge_id',  'text',        null),
            ('stars_payments', 'coins',      'integer',     '0'),
            ('stars_payments', 'stars',      'integer',     '0'),
            ('stars_payments', 'created_at', 'timestamptz', 'now()'),

            ('idempotency_keys', 'user_id',    'bigint',      null),
            ('idempotency_keys', 'endpoint',   'text',        ''''''),
            ('idempotency_keys', 'response',   'text',        '''{}'''),
            ('idempotency_keys', 'created_at', 'timestamptz', 'now()'),

            ('game_settings', 'value',      'text',        null),
            ('game_settings', 'updated_at', 'timestamptz', 'now()'),

            ('bans', 'reason',     'text',        null),
            ('bans', 'created_at', 'timestamptz', 'now()'),

            ('crash_history', 'round_id',    'text',          null),
            ('crash_history', 'crash_point', 'numeric(10,2)', '0'),
            ('crash_history', 'players',     'jsonb',         '''[]''::jsonb'),
            ('crash_history', 'created_at',  'timestamptz',   'now()'),

            ('promocodes', 'uses',        'integer',     '1'),
            ('promocodes', 'amount',      'integer',     '0'),
            ('promocodes', 'reward_type', 'text',        '''coins'''),
            ('promocodes', 'item_id',     'text',        null),
            ('promocodes', 'created_at',  'timestamptz', 'now()'),

            ('promocode_uses', 'created_at', 'timestamptz', 'now()'),

            ('shop_purchases', 'user_id',    'bigint',      null),
            ('shop_purchases', 'item_id',    'text',        null),
            ('shop_purchases', 'price',      'integer',     '0'),
            ('shop_purchases', 'source',     'text',        '''mini'''),
            ('shop_purchases', 'created_at', 'timestamptz', 'now()'),

            ('capsule_stock', 'stock',      'integer',     '0'),
            ('capsule_stock', 'total',      'integer',     '0'),
            ('capsule_stock', 'updated_at', 'timestamptz', 'now()'),

            ('chest_claims', 'user_id',            'text',        null),
            ('chest_claims', 'chest_type',         'text',        '''normal'''),
            ('chest_claims', 'reward_coins',       'integer',     '0'),
            ('chest_claims', 'reward_keys',        'integer',     '0'),
            ('chest_claims', 'reward_item',        'text',        ''''''),
            ('chest_claims', 'reward_item_amount', 'integer',     '0'),
            ('chest_claims', 'claimed',            'boolean',     'false'),
            ('chest_claims', 'created_at',         'timestamptz', 'now()'),
            ('chest_claims', 'claimed_at',         'timestamptz', null),

            ('upgrade_log', 'user_id',         'bigint',       null),
            ('upgrade_log', 'item_from',       'text',         null),
            ('upgrade_log', 'amount_from',     'integer',      '0'),
            ('upgrade_log', 'item_to',         'text',         null),
            ('upgrade_log', 'chance',          'numeric(6,2)', '0'),
            ('upgrade_log', 'roll',            'numeric(6,2)', '0'),
            ('upgrade_log', 'won',             'boolean',      'false'),
            ('upgrade_log', 'win_item',        'text',         null),
            ('upgrade_log', 'win_amount',      'bigint',       '0'),
            ('upgrade_log', 'coins_before',    'bigint',       '0'),
            ('upgrade_log', 'coins_after',     'bigint',       '0'),
            ('upgrade_log', 'inventory_after', 'jsonb',        '''[]''::jsonb'),
            ('upgrade_log', 'seen',            'boolean',      'false'),
            ('upgrade_log', 'created_at',      'timestamptz',  'now()'),

            ('arena_rounds', 'round_id',   'text',        null),
            ('arena_rounds', 'state',      'jsonb',       '''{}''::jsonb'),
            ('arena_rounds', 'updated_at', 'timestamptz', 'now()'),

            ('arena_payouts', 'amount',     'bigint',      '0'),
            ('arena_payouts', 'status',     'text',        '''pending'''),
            ('arena_payouts', 'error',      'text',        null),
            ('arena_payouts', 'created_at', 'timestamptz', 'now()'),
            ('arena_payouts', 'paid_at',    'timestamptz', null)
        ) as t(tbl, col, want, dflt)
    loop
        perform public._aura_fix_column(rec.tbl, rec.col, rec.want, rec.dflt);
    end loop;
end $$;

drop function if exists public._aura_fix_column(text, text, text, text);
