-- Знешкодження dev-копії прод-БД (deploy/README.md, «Dev-стенд на тому ж сервері»).
--
-- Навіщо: у копії ті самі Telegram-акаунти, що й на проді. Другий вхід тим самим
-- session_string з іншого процесу/IP = AuthKeyDuplicated, і сесія згорає назавжди
-- І НА ПРОДІ. Тому в dev акаунти не мають змоги увійти взагалі, а публікація в
-- канали вимкнена, щоб випадково піднятий worker-publish не дублював пости.
--
-- Запуск (у дев-клоні, після pg_restore):
--   docker compose exec -T db psql -U tg_events -d tg_events < deploy/dev-sanitize.sql
-- Ідемпотентний — можна запускати після кожного оновлення копії.

BEGIN;

UPDATE accounts_telegramaccount
   SET session_string   = '',
       two_fa_password  = '',
       auth_code_hash   = '',
       is_authenticated = false;

UPDATE analysis_publishconfig
   SET is_active = false;

COMMIT;

SELECT count(*) AS accounts_disarmed FROM accounts_telegramaccount WHERE session_string = '';
SELECT count(*) AS publish_active   FROM analysis_publishconfig    WHERE is_active;
