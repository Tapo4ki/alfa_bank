#!/bin/sh
# Восстановление бэкапа при первой инициализации БД (ошибки владельцев/ролей не критичны).
pg_restore -U postgres -d courses --no-owner --no-privileges /backup/course.backup || true
