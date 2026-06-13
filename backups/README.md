# Резервное копирование Pulsar через Restic

Данная директория содержит инструменты для резервного копирования и восстановления данных системы Pulsar с использованием Restic.

## Требования

Убедитесь, что в вашей системе установлены `restic` и CLI-утилита `sqlite3`.

## Конфигурация

Все параметры конфигурации считываются из общего файла [.env](file:///home/devman/workspace/pulsar-qwin-embeding/.env) в корне проекта (секция **Restic Backup Settings**).

## Использование

Основные исполняемые скрипты расположены в директории `cron/`:

* **Резервное копирование**: [cron/restic_backup.sh](file:///home/devman/workspace/pulsar-qwin-embeding/cron/restic_backup.sh)
* **Восстановление из бэкапа**: [cron/restic_restore.sh](file:///home/devman/workspace/pulsar-qwin-embeding/cron/restic_restore.sh)
