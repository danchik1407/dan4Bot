-- ============================================================================
-- AURA GAME: задача 47 — раунд арены переживает перезапуск сервиса.
--
-- Render на бесплатном тарифе усыпляет и перезапускает Web Service. Раунды
-- арен жили только в памяти процесса: если перезапуск попадал на раунд, ставки
-- пропадали («арена просто сбрасывается, баланс не отдаёт»).
--
-- Теперь каждый раунд сохраняется снимком в arena_rounds, а на старте сервис:
--   • доигрывает незакрытый раунд (мяч/стрелка уже определены) и платит;
--   • если исход неизвестен — возвращает ставки живым игрокам.
--
--   arena_rounds       — один снимок на арену (normal / team), jsonb;
--   arena_payouts      — журнал выплат (одна строка на арену/раунд/игрока);
--   arena_payout_apply — атомарное идемпотентное начисление по журналу.
--
-- Файл ИДЕМПОТЕНТНЫЙ: применять можно повторно.
-- Применять: Supabase → SQL Editor → вставить целиком → Run.
-- После применения: NOTIFY pgrst, 'reload schema';
-- ============================================================================

-- --- журнал выплат ---
create table if not exists public.arena_payouts (
    arena      text        not null,
    round_id   text        not null,
    user_id    bigint      not null,
    amount     bigint      not null default 0,
    status     text        not null default 'pending',
    error      text,
    created_at timestamptz not null default now(),
    paid_at    timestamptz,
    primary key (arena, round_id, user_id)
);

alter table public.arena_payouts enable row level security;
drop policy if exists arena_payouts_all on public.arena_payouts;
create policy arena_payouts_all on public.arena_payouts
    for all using (true) with check (true);
create index if not exists arena_payouts_pending_idx
    on public.arena_payouts (status, created_at);

create or replace function public.arena_payout_apply(
    p_arena    text,
    p_round_id text,
    p_user_id  bigint,
    p_amount   bigint
) returns boolean
language plpgsql
security definer
as $$
declare
    v_rows integer;
begin
    if p_amount is null or p_amount <= 0 then
        return false;
    end if;

    -- Забираем строку журнала «в работу»: повторный вызов её уже не получит,
    -- поэтому и рестарт, и повторная попытка не могут задвоить выигрыш.
    update public.arena_payouts
       set status = 'paid', paid_at = now(), error = null, amount = p_amount
     where arena = p_arena
       and round_id = p_round_id
       and user_id = p_user_id
       and status <> 'paid';

    get diagnostics v_rows = row_count;
    if v_rows = 0 then
        return false;
    end if;

    update public.users
       set coins = coalesce(coins, 0) + p_amount
     where user_id = p_user_id;

    return true;
end;
$$;

grant execute on function public.arena_payout_apply(text, text, bigint, bigint) to service_role;
grant execute on function public.arena_payout_apply(text, text, bigint, bigint) to authenticated;

-- --- снимок раунда ---
create table if not exists public.arena_rounds (
    arena      text        primary key,
    round_id   text        not null,
    state      jsonb       not null,
    updated_at timestamptz not null default now()
);

alter table public.arena_rounds enable row level security;

drop policy if exists arena_rounds_all on public.arena_rounds;
create policy arena_rounds_all on public.arena_rounds
    for all using (true) with check (true);
