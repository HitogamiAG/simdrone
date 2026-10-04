# Аудит Flight API

Проверены исходники `c4dbe6a` (включая `db17e3b`), тесты, Docker-артефакты и соответствие согласованному плану. На входе рабочее дерево было чистым. **Реализация не проходит приёмку.** Числа существующих тестов подтверждены повторным запуском; вывод о завершённости Flight API и заявленное покрытие были завышены.

В аудите исправлены статус и описания документации, пересобран устаревший Backend-образ и добавлены воспроизводимые проверки нарушений. Код приложения не изменялся; дефекты ниже остаются открытыми. Настоящие полёты и Gazebo-регрессия не выполнялись. Исторические результаты прежних прогонов в README не подтверждают новые flight-сценарии.

## Результаты

| Проверка | Результат | Границы доказательства |
| --- | --- | --- |
| Существующий набор Backend | 18 passed, 1 deprecation warning | Новых Flight API тестов только 2 |
| Существующий набор Hub | 33 passed, 1 deprecation warning | Новых Flight API тестов только 6 |
| Аудит Hub | 9 failed, exit 1 | Ошибки admission, владельца, восстановления, посадки, паузы и закрытия |
| Аудит Backend | 3 failed, exit 1 | Default revision, сломанный replay, потеря generation в HTTP payload |
| Нативное преобразование Gazebo | Расхождение с Hub | Противоположное направление при heading 90° и x=10 |
| Код внутри образов | Backend первоначально не совпадал; после rebuild совпадает. Hub совпадает | SHA-256 всех `.py` приложения сравнены с checkout |
| `docker compose config --quiet` | Успех с временным `.env` из образца | Проверка конфигурации, а не запуск стека |

`test_missions.py` проверяет переоткрытие SQLite, последовательный compare-and-swap, удаление и наличие маршрутов OpenAPI. Он не запускает миссию, не проверяет immutable active snapshot и не перезапускает Backend или Docker volume. Шесть `test_flight.py` проверяют две собственные формулы `_geo`, ограничение скорости, регистрацию маршрутов, штатный watchdog и доступность RTL при неготовности к arm. Token/seq, два владельца, конфликт режимов, MissionRaw upload/start, отмена и cleanup активного полёта отсутствовали. Старые lifecycle-тесты не создают активного полётного выполнения.

Mock-тесты и импорт/сигнатура MAVSDK не доказывают движение или возвращение PX4.

## Блокирующие дефекты

### 1. Эксклюзивность режима и владельца — P1

[`start_mission`](../px4-hub/app/flight.py) проверяет `active`, но не `session`. После создания Offboard-сессии миссия принимается: одновременно существуют оба режима. `session_action(arm)` не проверяет активную миссию. Аудиторская проверка `mission_conflicts_with_reserved_offboard` ожидает `409`, но получает `execution_id`.

[`flight_control`](../px4-hub/app/api.py) в `finally` сбрасывает `session.owner_connected`, даже если клиент отклонён до получения владения. Настоящий клиент остаётся подключённым, а запрос с неверным token снимает его флаг; третье подключение может получить управление. Проверка использует настоящий FastAPI WS-handler и подменённый flight service.

### 2. Частичный arm и отказ watchdog — P1

В [`session_action`](../px4-hub/app/flight.py) arm выполняется до Offboard.start, а `session.armed=True` присваивается после обоих вызовов. Если arm успешен и start отказал, наблюдаемое `armed=True`, локальное `armed=False`; disarm/RTL отсутствуют, watchdog не реагирует. Воспроизведены вызовы `['setpoint','arm','offboard_start']` без восстановления.

Исключение в `_watchdog` только меняет статус на `failed` и завершает задачу. Если отказал нейтральный setpoint, RTL не запрашивается. Подменённый setter воспроизвёл `status=failed`, `calls=['setpoint']`, без RTL. Требуется согласование наблюдаемого состояния и обработка неизвестных результатов, а не повторный arm вслепую.

### 3. Отзыв сессии после RTL и паузы — P1

`session_action(arm)` не проверяет `returning` и подтверждённую землю. Проверка на `IN_AIR` снова перевела возвращающуюся сессию в `active` и вызвала arm/Offboard.start. После watchdog RTL сессия остаётся в `self.session`: новая сессия после посадки конфликтует до явного DELETE.

На паузе watchdog только присваивает `paused` и спит. Последний setpoint не заменяется нейтральным, token не отзывается. Проверка с движением вперёд получила последний setpoint `(3.0,0.0,0.0,0.0)` и после паузы. После resume код всегда вызывает RTL, включая нахождение на земле, вместо требуемого disarm. Повторение setpoint внутри MAVSDK в этой проверке не моделировалось; отсутствие нейтрали подтверждено по вызовам приложения.

### 4. Подтверждение посадки у станции — P1

[`_wait_landed`](../px4-hub/app/flight.py) принимает кешированные `armed=False` и `ON_GROUND`. Свежесть armed/landed/position, расстояние до станции и поколение не проверяются. Две независимые проверки немедленно завершились при телеметрии возрастом 1000 с и при свежей позиции примерно в 111 км от станции. Код может выставить `completed`/`cancelled` без выполнения критерия возврата.

### 5. Координаты Gazebo — P1

`_geo` вращает XY с противоположным знаком heading. Нативный `gz::math::SphericalCoordinates::PositionTransform(LOCAL2, SPHERICAL)` при origin=(0°,0°), heading=90°, XYZ=(10,0,0) вернул latitude **+0.000090436947704967°**. Hub вернул **−0.00008983152841195215°**. Существующий тест проверяет отрицательный знак и закрепляет ошибку.

Вторая разница — сферическое приближение вместо эллипсоидального WGS84. Нет ограничения радиуса маршрута, при котором погрешность допустима. Проверка своей формулы не доказывает согласованность с NavSat/PX4.

NavSat использует `sphericalCoordinates`, вызывающий `LOCAL2`: [gz-sim Util.cc](https://github.com/gazebosim/gz-sim/blob/gz-sim8/src/Util.cc), [gz-math SphericalCoordinates.cc](https://github.com/gazebosim/gz-math/blob/gz-math7/src/SphericalCoordinates.cc). Аудит запускал нативную библиотеку в Docker, а не сервер Gazebo или полёт.

### 6. Поколения и lifecycle — P1

[`HubApi.flight_start_mission`](../backend/app/adapters.py) выполняет `del generation`; поле отсутствует в реальном HTTP payload, перехваченном `httpx.MockTransport`. Hub не принимает ожидаемое поколение. Session/execution UUID защищают некоторые старые обращения, но не заменяют generation-контракт admission.

Flight-переходы используют собственный lock, lifecycle — `record.operation_lock`. `close()` не устанавливает закрытое состояние и не сериализуется с незавершённой validation. Проверка остановила `gazebo.world()` во время валидации, выполнила `close()`, разрешила ответ: новая миссия принята после закрытия. В stop close предшествует смене статуса record, поэтому окно существует и в lifecycle.

Дополнительно `_watch` при неожиданном завершении процесса вызывает `px4.stop` без `flight.close`. Flight events каждый раз получают текущий контроллер и могут пережить restart без invalidation. Control WS ждёт следующего ввода без наблюдателя закрытия/поколения. Полное освобождение полётных задач и WS не доказано.

### 7. Failsafe потери Offboard — P1

Нет установки/обратного чтения `COM_OBL_RC_ACT` и `COM_OF_LOSS_T`. В исходниках закреплённого PX4 commit `6ea3539157ca358c70a515878b77077af7d4611d` дефолт `COM_OBL_RC_ACT=0` означает Position; Return — 3. В стартовых скриптах найдено только изменение таймаута по коэффициенту скорости симуляции. Это чтение источников, а не параметра живого автопилота; RTL при потере Hub/MAVSDK не подтверждён. [Параметры закреплённого PX4](https://github.com/PX4/PX4-Autopilot/blob/6ea3539157ca358c70a515878b77077af7d4611d/src/modules/commander/commander_params.c).

## Прочие расхождения

- [`FlightPlatform.start_mission`](../backend/app/flight.py) проверяет текущую ревизию до replay. Повтор принятого запроса после редактирования определения даёт `409`, после удаления — `404`, вместо исходной операции. `revision=None` допускается схемой/OpenAPI, но отклоняется сервисом.
- Offboard create/arm/disarm не принимают `request_id`; отмена в Hub его отбрасывает. Fingerprint миссии — только id/revision, а не всё содержимое snapshot. Полного согласования неизвестных результатов MAVSDK нет.
- Нет единого Execution для Offboard; отдельные return/land возвращают строковый статус без наблюдаемого завершения. Доступные действия перечислены без учёта готовности. Backend хранит generation выполнения, но не проверяет его для GET/cancel после reset.
- Hub берёт станцию из текущей runtime-позы при создании instance, а не явно из сохранённой начальной позы Backend. Прогресс содержит total=N+2 при current не выше N. Validation не отдаёт полный рассчитанный маршрут взлёта/возврата.
- Internal mission body — `dict` без схемы; отсутствующие ключи могут давать `500`. Yaw-предел не сверяется с PX4. Capabilities включены безусловно, включая неподтверждённые flight-сценарии.
- README показывал неправильные публичные пути запуска, cancel и session GET/DELETE. Таблицы исправлены в аудите.

## Артефакты и повторные проверки

SHA-256 исходников внутри прежнего Backend-образа отличался в `api.py`: отсутствовал общий lock для cancel и Offboard arm/disarm/delete. Прежние тесты монтировали исходник и проверяли checkout, а не содержимое образа. Образ Hub совпадал с checkout. Backend пересобран; затем повторно сравнены все `.py` обоих приложений.

Команды из корня репозитория:

```sh
docker build -t uav-platform-backend:local -f backend/Dockerfile backend
docker build -t uav-px4-hub:audit-tests -f docs/flight-audit/Dockerfile.tests .
docker run --rm --network none -v "$PWD/backend/tests:/tests:ro" -e PYTHONPATH=/app --entrypoint pytest uav-platform-backend:local -q -p no:cacheprovider /tests/test_platform.py /tests/test_live_logs.py /tests/test_missions.py
docker run --rm --network none -v "$PWD/px4-hub/tests:/tests:ro" --entrypoint pytest uav-px4-hub:audit-tests -q -p no:cacheprovider /tests/test_flight.py /tests/test_contract.py /tests/test_diagnostics.py /tests/test_lifecycle.py /tests/test_live_logs.py
```

В финальном прогоне **код приложения не монтировался**. Hub test image добавляет pytest к runtime-образу, сохраняя приложение и MAVSDK. Наборы используют подменённые зависимости; прежние live-log тесты запускают обычные дочерние процессы, которые не являются PX4/Gazebo.

Проверки известных дефектов (обе команды на проверенной версии возвращают **exit 1**, проверки failed; xfail не используется):

```sh
docker run --rm --network none -v "$PWD/docs/flight-audit:/audit:ro" --entrypoint python uav-px4-hub:local /audit/check_hub.py
docker run --rm --network none -e PYTHONPATH=/app -v "$PWD/docs/flight-audit:/audit:ro" --entrypoint python uav-platform-backend:local /audit/check_backend.py
```

Харнессы подменяют внешние команды, telemetry, HTTP-транспорт и Gazebo. Admission, watchdog, landing, close, replay и WS-handler берутся из приложения. В Hub используется настоящий SDK-тип скорости. Fixture завершает созданные задачи; настоящих дронов нет.

Независимая координатная проверка:

```sh
docker build --target px4-build -t uav-px4-hub:audit-build -f px4-hub/Dockerfile .
docker run --rm --network none -v "$PWD/docs/flight-audit/coordinates.cpp:/audit.cpp:ro" uav-px4-hub:audit-build sh -c 'g++ -std=c++17 /audit.cpp -o /tmp/audit $(pkg-config --cflags --libs gz-math7) && /tmp/audit'
docker run --rm --network none --entrypoint python uav-px4-hub:local -c 'from app.flight import _geo; print(_geo({"latitude_deg":0,"longitude_deg":0,"heading_deg":90},10,0))'
```

Для `docker compose config --quiet` временно создан `.env` из образца и удалён в `finally`; секреты не выводились/коммитились. `third-party` отсутствует, Gazebo runtime-образа и запущенного стека нет. `docker compose up -d --build`, Gazebo contract/regression/smoke и настоящие полёты не выполнены.

## Повторная приёмка

Устранить открытые дефекты, добиться прохождения аудиторских проверок и добавить публичные HTTP/WS-тесты Flight API. Затем восстановить закреплённые ресурсы Gazebo, пересобрать/обновить Compose и последовательно проверить ручной взлёт, движение, потерю ввода/Backend/Hub, миссию, отмену, посадку у станции и lifecycle. Подтверждения должны опираться на свежую PX4-телеметрию и runtime-позы Gazebo. До этого оба режима нельзя объявлять завершёнными по плану.
