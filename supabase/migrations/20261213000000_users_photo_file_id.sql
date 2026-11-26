-- ============================================================================
--  АВАТАРКИ ИГРОКОВ ДЛЯ ЛИДЕРБОРДА
--  Версия: 20261213000000
--
--  Заказчик: «почему в лидерборде кто-то с авой, кто-то без — исправь».
--  Причина: в базе лежит только photo_url, который Telegram отдаёт в initData
--  и только тем, у кого фото открыто для всех. У остальных его нет, и на
--  экране рисовалась буква.
--
--  Здесь храним file_id фото, полученный ботом через getUserProfilePhotos
--  (бот может получить фото любого, кто запускал его). Сам файл отдаёт наш
--  /api/avatar/{id}: токен бота в ссылках не светится.
--
--  avatar_checked_at — когда последний раз спрашивали Telegram. Если у
--  игрока фото нет вовсе, повторно не дёргаем чаще, чем раз в сутки.
--
--  Файл идемпотентный, применять в Supabase → SQL Editor.
-- ============================================================================

alter table public.users add column if not exists photo_file_id   text;
alter table public.users add column if not exists avatar_checked_at timestamptz;

-- Проверочный запрос: должно быть true / true
select 'аватарки' as check_name,
       exists (select 1 from information_schema.columns
                where table_schema = 'public' and table_name = 'users'
                  and column_name = 'photo_file_id') as photo_file_id_ok,
       exists (select 1 from information_schema.columns
                where table_schema = 'public' and table_name = 'users'
                  and column_name = 'avatar_checked_at') as avatar_checked_at_ok;
