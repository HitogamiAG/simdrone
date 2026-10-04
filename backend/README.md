# Общий Backend платформы

Backend — публичный FastAPI-оркестратор Gazebo Service и PX4 Hub. `main.py` собирает FastAPI и lifespan, `api.py` содержит HTTP/WebSocket-маршруты, `service.py` — сценарии, `adapters.py` — HTTP-клиенты зависимостей, `realtime.py` — общие подписки. Backend выдаёт устойчивый в пределах работы процесса `drone_id`, сериализует изменяющие сценарии одним lock и сводит диагностику. Gazebo Service владеет миром/моделями, PX4 Hub — SITL/MAVSDK процессами, MediaMTX — публикацией видео. Реестр Backend хранится только в памяти.

**Статус Flight API:** исправлены пять дополнительно воспроизведённых ошибок частичного arm, паузы и конкурентных переходов RTL/land/watchdog. После исправлений Backend прошёл 21 тест, Hub — 44, включая шесть новых сценариев. Реальный изолированный прогон подтвердил Offboard, отмену и один проход MissionRaw; во второй чистой серии MissionRaw завис в `active/mission` над станцией и превысил 180 секунд. Полная приёмка Flight API не пройдена; перед продолжением необходимо сохранять PX4 ULog до cleanup и локализовать нестабильность запуска миссии. Подробности: [результаты flight-тестирования](../docs/flight-testing.md) и [повторный аудит](../docs/flight-api-followup-audit.md).

## Запуск и конфигурация

Из корня репозитория:

```sh
docker compose up --build
```

Backend доступен на `http://localhost:8003`; OpenAPI — `/docs`, healthcheck приложения — `/healthz`. Все процессы и тесты платформы работают в Docker; для Backend не требуется Gazebo на хосте. Контейнер запускает один Uvicorn worker, непривилегированного пользователя и `init`.

Backend использует `GAZEBO_API_URL`, `PX4_HUB_API_URL`, `MEDIAMTX_API_URL`, `MEDIAMTX_WHEP_URL`, `BACKEND_REQUEST_TIMEOUT`, `BACKEND_STARTUP_TIMEOUT`, `BACKEND_MAX_SUBSCRIPTIONS` и `MISSION_DB`. Порты, origin браузера и ICE hostnames MediaMTX заданы в корневом [`.env.example`](../.env.example). `WEBRTC_IPS_FROM_INTERFACES=no` заставляет MediaMTX объявлять только настроенные hostnames; `WEBRTC_ADDITIONAL_HOSTS` должен содержать адрес, разрешимый и доступный браузеру. Прямой WebRTC рассчитан на доступные друг другу сети и не включает TURN.

## API

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/v1/system/status` | Backend, Gazebo, Hub и MediaMTX |
| POST | `/api/v1/system/gazebo/reboot` | Согласованный reset мира через reboot Gazebo |
| GET / PATCH | `/api/v1/world` | Чтение и изменение поддерживаемых настроек мира |
| POST | `/api/v1/world/reset` | Вернуть исходный SDF и удалить все runtime-дроны |
| POST | `/api/v1/world/pause`, `/resume` | Пауза / продолжение симуляции |
| GET / POST | `/api/v1/drones/` | Список / создание `x500_gimbal` с начальной позой |
| GET / DELETE | `/api/v1/drones/{id}/` | Состояние / удаление дрона |
| POST | `/api/v1/drones/{id}/reset` | Сброс модели, сенсоров и PX4 к исходным значениям |
| GET / POST | `/api/v1/drones/{id}/autopilot`, `/start`, `/stop`, `/restart` | Состояние и lifecycle PX4 |
| GET / PATCH | `/api/v1/drones/{id}/autopilot/parameters` | Разрешённые параметры PX4 без физического reset |
| GET / PATCH | `/api/v1/drones/{id}/sensors/{sensor}/` | Чтение и изменение `update_rate` |
| POST | `/api/v1/drones/{id}/sensors/{sensor}/reset` | Полный drone-reset и возврат исходной частоты |
| GET | `/api/v1/drones/{id}/sensors/{sensor}/video` | WHEP URL и состояние камеры |
| POST | `/api/v1/drones/{id}/sensors/{sensor}/{activate,deactivate}` | Управление публикацией камеры |
| WS | `/api/v1/realtime` | Общий поток с командами `subscribe` / `unsubscribe` |
| CRUD | `/api/v1/missions/` | Определения миссий и optimistic revision updates в SQLite |
| POST | `/api/v1/missions/{id}/validate` | Проверить маршрут для дрона и текущего PX4 instance |
| GET | `/api/v1/drones/{id}/flight` | Состояние полёта |
| POST | `/api/v1/drones/{id}/flight/missions` | Принять запуск миссии |
| GET | `/api/v1/drones/{id}/flight/executions/{execution_id}` | Состояние выполнения |
| POST | `/api/v1/drones/{id}/flight/executions/{execution_id}/cancel` | Запросить отмену и RTL |
| POST | `/api/v1/drones/{id}/flight/{return,land}` | RTL или посадка в текущем месте |
| POST | `/api/v1/drones/{id}/flight/offboard/sessions` | Создать сессию управления |
| GET / DELETE | `/api/v1/drones/{id}/flight/offboard/sessions/{session_id}` | Прочитать / закрыть сессию |
| POST | `/api/v1/drones/{id}/flight/offboard/sessions/{session_id}/{arm,disarm}` | Включение Offboard на земле / disarm после посадки |
| WS | `/api/v1/drones/{id}/flight/offboard/sessions/{session_id}/control` | Ввод джойстика с token и возрастающим `seq` |

Создание принимает `model: "x500_gimbal"`, необязательные `name` и `pose`; успешный ответ выдаётся после подключения PX4 и позиционной телеметрии. Поза задаётся только при создании. PATCH PX4 разрешает `MPC_XY_VEL_MAX`, `MPC_Z_VEL_MAX_UP`, `MPC_Z_VEL_MAX_DN`. PATCH сенсора сначала валидируется, затем выполняет полный drone-reset; PX4 запускается только после применения частоты. Изменение мира объединяет переданные поля с текущими значениями, удаляет все дроны через world-reset и применяет настройки. Следующий world-reset/reboot возвращает исходный SDF.

World-reset/reboot не создаёт runtime-дроны снова: Backend и Hub реестры очищаются, прежние публичные ID отвечают `404`. Успешный reset повторно проверяет мир, поэтому после перезапуска Backend, обнаружившего оставшиеся runtime-модели, можно создать дроны после явной очистки мира. Drone-reset сохраняет публичный ID и намерение запуска PX4; параметры PX4 сбрасываются к defaults. Stop/start/restart PX4 сохраняют подтверждённые значения параметров. Изменение PX4 и создание/restart дрона на паузе возвращает `409 simulation_paused`. Удаление и сброс мира разрешены на паузе; мир возвращается к режиму исходного SDF. Все изменения мира/сенсоров/дронов относятся к последовательности внешних вызовов, а не транзакции; при неполном отказе запись остаётся `failed` с этапом и известными ресурсами. GET дрона и списка сохраняют доступ к такой записи и показывают ошибки Gazebo/PX4 отдельно; при недоступном Gazebo актуальная поза возвращается `null`.

Ошибки имеют вид `{"error":{"code":"...","message":"...","details":...}}`; ошибки схемы также сериализуются в этот формат и возвращают `422`. При создании Backend заранее проверяет занятость имени. Если ответ Gazebo потерян, Backend ищет созданную модель по уникальному имени и удаляет её только после подтверждения, что PX4 instance отсутствует или удалён. При неизвестном состоянии PX4 модель сохраняется для безопасного согласования. Mission definitions сохраняются в SQLite (`MISSION_DB`, по умолчанию `/data/missions.sqlite3`) на Compose volume `backend-missions`; запуски и их история остаются в памяти. Миссия привязана к имени мира и снимку его `spherical_coordinates`; Hub повторно проверяет их перед стартом. Маршрут задаётся точками XYZ Gazebo в метрах, общей скоростью и высотами взлёта/возврата. Обход препятствий и точная посадка по метке отсутствуют.

Все управляющие POST, включая создание Offboard-сессии и arm/disarm, требуют `request_id`; повтор того же запроса возвращает сохранённый ответ, повтор ID с другой командой даёт `409`. Миссия требует явные `mission_id`, `revision` и `request_id`. В коде предусмотрены загрузка MissionRaw в PX4, запрос RTL при отмене и Offboard через поток `forward/right/up/yaw` с token и возрастающим `seq`. Watchdog задаёт нулевую скорость после 0,5 с и RTL после 5 с; состояние возврата подтверждается свежими landed/armed/position samples у станции. Тесты используют подменённые зависимости и не подтверждают реальное движение, прямой взлёт или возврат. Потеря самого Hub отдельно настроена на PX4 через `COM_OF_LOSS_T=1` и `COM_OBL_RC_ACT=3` с проверкой чтения параметров при старте; этот failsafe ещё не проверен реальным отключением.

Канал `drone.{drone_id}.flight` добавлен в `/api/v1/realtime`; сквозное наблюдение flight-состояния через Backend пока не проверено.

## Realtime и видео

WebSocket принимает, например:

```json
{"action":"subscribe","channels":["drone.PUBLIC_ID.telemetry","drone.PUBLIC_ID.sensor.imu_sensor"]}
```

Backend разделяет одну upstream-подписку между клиентами канала; клиентский mailbox сохраняет последнее сообщение каждого типа. Сообщения содержат публичный ID, channel/type, UTC-время получения, world/runtime generations и исходное время только когда оно присутствует. Reset инвалидирует старый поток и буферы; клиент получает `invalidated` и подписывается заново. Число подписок на соединение ограничено.

Видео публикуется по внутреннему MediaMTX пути с Gazebo ID, а Backend возвращает WHEP URL. Первый WHEP-зритель запускает camera activate через on-demand hook, последний останавливает. React-клиент должен создать `RTCPeerConnection`, добавить `recvonly` video transceiver, отправить SDP offer методом POST на WHEP URL и установить SDP answer. Кадры камер через общий WebSocket не передаются.

## Docker-проверки

Контрактные тесты Backend и Hub, а также реальную последовательную интеграцию выполнять внутри Docker:

```sh
docker compose build backend px4-hub gazebo-service
docker compose up -d --no-deps backend px4-hub gazebo-service mediamtx
docker run --rm --network none -v "$PWD/backend:/work:ro" -w /work -e PYTHONPATH=/work --entrypoint pytest uav-platform-backend:local -q -p no:cacheprovider tests/test_platform.py
docker run --rm --network uav-simulation -v "$PWD/backend/tests:/tests:ro" -w /tests --entrypoint python uav-platform-backend:local integration.py
docker run --rm --network uav-simulation -v "$PWD/backend/tests:/tests:ro" -w /tests --entrypoint python uav-platform-backend:local realtime_integration.py
docker compose exec -T gazebo-service pytest -q /opt/uav/tests/test_contract.py
docker compose exec -T gazebo-service python /opt/uav/tests/regression.py
docker compose exec -T gazebo-service python /opt/uav/tests/smoke.py
docker compose config --quiet
```

`tests/test_platform.py` проверяет контракты на подменённых адаптерах, включая JSON-ошибки валидации, потерянный ответ Gazebo create, безопасную компенсацию, failed-записи и сериализацию camera actions. `tests/integration.py` проверяет два работающих дрона с независимыми PX4 параметрами и свежей телеметрией, stop/start/restart, sensor PATCH, drone-reset, паузу, world PATCH и reset. `tests/realtime_integration.py` проверяет два клиента одного канала, медленного читателя и invalidation при DELETE. Скрипты меняют мир; запускать без параллельных клиентов. Браузерный тест WHEP находится в `tests/webrtc_browser.js`, Dockerfile фиксирует Chrome и Playwright. В Docker Chrome получил и декодировал кадр 1280×720; два зрителя разделили одну публикацию, закрытие первого не остановило второго, уход последнего выключил камеру. RTSP/H.264 smoke запускается отдельно и не заменяет проверку браузера.

Сборка и запуск браузерной проверки:

```sh
docker build -f backend/tests/Dockerfile.browser -t uav-webrtc-browser:test backend/tests
docker run --rm --network uav-simulation uav-webrtc-browser:test
```

Проверка создаёт и удаляет runtime-дрон, получает настоящий кадр Chrome через WHEP, подключает двух зрителей к одной публикации и проверяет, что публикация завершается после ухода последнего. Запускать её последовательно с другими изменяющими мир тестами.

### Результат проверки исправлений 03.10.2026

В Docker прошли Backend contract tests (**13 passed**), Hub tests (**21 passed**), реальная интеграция Backend с двумя PX4, realtime-интеграция, Gazebo `regression.py` и `smoke.py`, а также браузерный WebRTC-тест с декодированием кадра 1280×720 и двумя зрителями. `docker compose config --quiet` прошёл. Backend contract suite проверяет reconciliation после потерянного ответа, сериализацию camera actions, ошибки Pydantic и доступность failed-записей. Hub suite проверяет сохранение monitor при отказе parameter snapshot.

Backend не восстанавливает соответствия после собственного рестарта и не усыновляет найденные Hub/Gazebo ресурсы; `/system/status` показывает неучтённые модели и instances. Явный world-reset удаляет все Hub instances перед перезагрузкой Gazebo; при отказе удаления Gazebo не перезапускается. Автоматического rollback внешних вызовов и автоматического восстановления сервисов нет.

## Реальные полётные тесты

Изолированный SITL набор запускается командой `sh backend/tests/flight/run.sh`. Он использует отдельный Compose project без опубликованных host ports и управляет дроном через публичный Backend API. Offboard и Mission проверяются по PX4 realtime телеметрии и независимым runtime-позам Gazebo Transport; JSON-артефакты находятся в игнорируемом `artifacts/flight/`. Первый полный прогон прошёл, второй обнаружил зависание миссии на взлётной высоте. Открытые сценарии и точные результаты перечислены в [документе тестирования полётов](../docs/flight-testing.md); полный контракт пока не принят.

## Live-логи для UI

Подписка через существующий `WS /api/v1/realtime`:

```json
{"action":"subscribe","channels":["world.logs","drone.PUBLIC_ID.logs"]}
```

`world.logs` — объединённые stdout/stderr Gazebo Server; `drone.PUBLIC_ID.logs` — stdout/stderr PX4 соответствующего instance. MAVSDK, ULog и события действий Backend не входят в поток. UI самостоятельно добавляет записи пользовательских действий и их результатов. Новый подписчик получает только новые сообщения, история и replay отсутствуют.

Формат соответствует realtime: `channel`, `type: "log"`, публичный `drone_id` (для мира — null), UTC `received_at` Backend, `world_generation`, `runtime_generation`, `data`. В `data` находятся `source`, исходное UTC-время получения сервисом и `message`; время симуляции и severity не выводятся из текста. Пауза мира не прекращает чтение вывода процессов.

Backend открывает один upstream WebSocket на канал для всех зрителей. Для логов используется отдельная FIFO-очередь до 128 записей на клиента, вместо mailbox телеметрии с заменой последних значений. Переполнение удаляет старые записи и выдаёт `{"type":"gap","channel":"...","dropped":N}`. Upstream также ограничивает очередь: его сообщение `gap` передаётся в realtime с количеством пропущенных строк в `data`. Число подписок ограничивается существующим `BACKEND_MAX_SUBSCRIPTIONS`.

Stop/restart/reset/delete, падение процесса и reboot завершают прежний поток; приходит `invalidated`, клиент подписывается повторно после запуска. Смена поколения очищает ожидающие записи. Логи не возвращаются через HTTP и не накапливаются без клиентов. Для дрона подписка возможна после назначения Hub instance; первоначальный POST создания синхронный, поэтому ранний вывод старта до получения публичного ID в UI недоступен. Внутренние диагностические файлы сервисов сохраняют прежнее поведение.

### Проверка live-логов, 04.10.2026

Добавлены unit-тесты bounded FIFO, пропуска старых строк при backpressure, отсутствия replay, общей upstream-подписки, инвалидирования до перезапуска upstream, гонки закрытия WebSocket и сохранения последней fatal-строки при естественном завершении процесса. `tests/live_logs_integration.py` запускается в Docker на Compose-сети и проверяет реальные сообщения Gazebo/PX4 с двумя зрителями, паузу, stop/start/restart/delete дрона, world-reset и reboot. Все изменяющие мир интеграционные скрипты запускаются последовательно.

```sh
docker run --rm --network none -v "$PWD/backend:/work:ro" -w /work -e PYTHONPATH=/work --entrypoint pytest uav-platform-backend:local -q -p no:cacheprovider tests/test_platform.py tests/test_live_logs.py
docker run --rm --network uav-simulation -v "$PWD/backend/tests:/tests:ro" -w /tests --entrypoint python uav-platform-backend:local live_logs_integration.py
```

Проверка 04.10.2026: Backend unit tests **16 passed**, Gazebo contract/live-log tests **20 passed**, Hub tests **27 passed**. Сквозной `live_logs_integration.py` получил настоящие строки Gazebo и PX4 двумя клиентами, проверил pause, stop/start/restart/delete, world-reset и reboot. Также прошли Backend integration (два дрона, параметры, sensor reset, pause и world reset), realtime integration (общие клиенты и медленный читатель), Gazebo `regression.py`, Gazebo `smoke.py` и `docker compose config --quiet`. Исправлен порядок закрытия: Backend инвалидирует upstream до команд PX4/reset, чтобы зритель получил `invalidated`, а не ошибку закрывшегося сокета.
