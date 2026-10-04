# Проверка исправлений Flight API, 04.10.2026

Проверен commit `9d02331` (`Fix Flight API safety and idempotency`). Рабочее дерево на входе чистое. **Полное устранение дефектов не подтверждено:** исходные проверки проходят, но расширение сценариев выявило пять воспроизводимых нарушений. Код приложения в этой проверке не изменён.

## Подтверждённые результаты

Backend: **21 passed**, Hub: **37 passed**; в каждом наборе одно предупреждение Starlette о deprecated BlockingPortal. Исходные аудиторские проверки: Hub **9/9**, Backend **3/3**. Это подтверждает конкретные проверенные сценарии эксклюзивности, владельца WS, свежести/близости посадочной телеметрии, admission после close, ревизии/replay и forwarding поколения. При прогоне Hub-харнесса также наблюдалось `Task exception was never retrieved` с `WebSocketDisconnect`; exit 0 не подтверждает корректное освобождение всех WS-задач.

Нативный Gazebo math снова подтвердил направление heading=90°, x=10: latitude `+0.000090436947704967°`. Формула Hub для этого примера совпадает. Установка/чтение failsafe проверены подменённым SDK; отключение живого PX4/MAVSDK не проверено.

Пересобраны оба образа, обновлены контейнеры. SHA-256 всех 12 Python-файлов каждого приложения внутри контейнеров совпали с checkout. Код приложения в тесты не монтировался; монтировались только тесты/харнессы. `docker compose config --quiet` прошёл.

## Открытые дефекты

### P1: частичный arm при задержке телеметрии остаётся без восстановления

[`session_action`](../px4-hub/app/flight.py) после успешного arm и отказа Offboard.start использует sample `armed=False`, полученный **до** команды, если он моложе `telemetry_stale_after`. Тогда устанавливает `failed`, оставляет `session.armed=False`, не выполняет disarm/RTL; watchdog не реагирует на последующий armed sample.

Воспроизведение: arm успешно возвращается, обновление subscription задерживается, start выбрасывает исключение. Получены `setpoint → arm → offboard_start → offboard_stop`, без восстановления. Исходная fixture сразу меняет `armed` внутри arm, поэтому скрывает этот случай. Требуется согласовать телеметрию после команды и безопасно обрабатывать неизвестный результат.

### P1: пауза во время подготовки не предотвращает arm

После `_preflight` выполняется `sleep(1.1)`; затем повторно проверяется только `closed`. Если мир поставлен на паузу в этом интервале, Hub всё равно выполняет arm и Offboard.start. Воспроизведены оба вызова после установления паузы. Нужна повторная проверка готовности непосредственно перед arm и согласование с pause/lifecycle.

### P1: land не останавливает задачу возврата миссии

[`action`](../px4-hub/app/flight.py) отменяет только `active.task`, оставляя `active.return_task`. После land ранее начатая `_complete_return` продолжает выполнять RTL и менять статус execution.

Воспроизведение: задержана отправка RTL в return_task, принят land, затем снята задержка. Последовательность команд: **land → RTL**. Нужны отмена и ожидание всех прежних задач перехода перед посадкой.

### P1: watchdog перезаписывает состояние после RTL

Watchdog не сериализует переходы с `action/session_action`. Во время ожидающего нейтрального setpoint команда return устанавливает `returning` и выключает `session.armed`. После завершения setter watchdog без повторной проверки статуса/владения записывает `input_lost_hold`.

Воспроизведено состояние `input_lost_hold` после принятого RTL. Его освобождение через `create_session` ожидает именно `returning`, поэтому сессия может блокировать следующее управление и после посадки. Нужны сериализация переходов или проверка актуальности перехода после каждого await.

### P2: невооружённая сессия переживает паузу

Ветвь паузы watchdog проверяет `session.armed`. Зарезервированная сессия `ready` сохраняет статус, token и владельца при pause/resume. Это нарушает требование отзыва управляющей сессии на паузе. Воспроизведено сохранение `ready` спустя несколько итераций watchdog при подтверждённом paused-флаге.

## Воспроизведение

Из корня репозитория, с локальным `.env` (в аудите временно использован образец):

```sh
docker compose build backend px4-hub
docker compose up -d --no-deps backend px4-hub
docker build -t uav-px4-hub:audit-tests -f docs/flight-audit/Dockerfile.tests .
docker run --rm --network none -v "$PWD/backend/tests:/tests:ro" -e PYTHONPATH=/app --entrypoint pytest uav-platform-backend:local -q -p no:cacheprovider /tests
docker run --rm --network none -v "$PWD/px4-hub/tests:/tests:ro" --entrypoint pytest uav-px4-hub:audit-tests -q -p no:cacheprovider /tests
docker run --rm --network none -v "$PWD/docs/flight-audit:/audit:ro" --entrypoint python uav-px4-hub:local /audit/check_hub.py
docker run --rm --network none -e PYTHONPATH=/app -v "$PWD/docs/flight-audit:/audit:ro" --entrypoint python uav-platform-backend:local /audit/check_backend.py
docker run --rm --network none -v "$PWD/docs/flight-audit:/audit:ro" --entrypoint python uav-px4-hub:local /audit/check_followup.py
docker run --rm --network none -v "$PWD/docs/flight-audit/coordinates.cpp:/audit.cpp:ro" uav-px4-hub:audit-build sh -c 'g++ -std=c++17 /audit.cpp -o /tmp/audit $(pkg-config --cflags --libs gz-math7) && /tmp/audit'
docker compose config --quiet
```

`check_followup.py`: **5 failed, exit 1**, без xfail. `audit-build` — существующий образ с нативной gz-math7; этот прогон не пересобирал его. Все новые проверки используют настоящее приложение и подменённые MAVSDK/телеметрию/Gazebo. Они доказывают ошибки порядка вызовов и переходов приложения, а не физическое поведение дрона.

Реальные полёты, Gazebo contract/regression/smoke и публичные сквозные HTTP/WS-сценарии не выполнены: `third-party` отсутствует, рабочий Gazebo stack не запускался. Единственный уже работавший сторонний контейнер не затрагивался. Созданный временный `.env` удалён, запущенные для проверки Backend/Hub остановлены. Существующие архитектурные расхождения плана (единый Offboard Execution, наблюдаемое завершение отдельных return/land и другие) этой проверкой не устранены.
