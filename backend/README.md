# Общий Backend платформы

Backend — публичный FastAPI-оркестратор Gazebo Service и PX4 Hub. Он выдаёт устойчивый в пределах работы процесса `drone_id`, сериализует изменяющие сценарии одним lock и сводит диагностику, управление сенсорами и realtime. Gazebo Service владеет миром/моделями, PX4 Hub — SITL/MAVSDK процессами, MediaMTX — публикацией видео. Реестр Backend хранится только в памяти.

## Запуск и конфигурация

Из корня репозитория:

```sh
docker compose up --build
```

Backend доступен на `http://localhost:8003`; OpenAPI — `/docs`, healthcheck приложения — `/healthz`. Все процессы и тесты платформы работают в Docker; для Backend не требуется Gazebo на хосте. Контейнер запускает один Uvicorn worker, непривилегированного пользователя и `init`.

Backend использует `GAZEBO_API_URL`, `PX4_HUB_API_URL`, `MEDIAMTX_API_URL`, `MEDIAMTX_WHEP_URL`, `BACKEND_REQUEST_TIMEOUT`, `BACKEND_STARTUP_TIMEOUT` и `BACKEND_MAX_SUBSCRIPTIONS`. Порты, origin браузера и ICE hostnames MediaMTX заданы в корневом [`.env.example`](../.env.example). `WEBRTC_IPS_FROM_INTERFACES=no` заставляет MediaMTX объявлять только настроенные hostnames; `WEBRTC_ADDITIONAL_HOSTS` должен содержать адрес, разрешимый и доступный браузеру. Прямой WebRTC рассчитан на доступные друг другу сети и не включает TURN.

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

Создание принимает `model: "x500_gimbal"`, необязательные `name` и `pose`; успешный ответ выдаётся после подключения PX4 и позиционной телеметрии. Поза задаётся только при создании. PATCH PX4 разрешает `MPC_XY_VEL_MAX`, `MPC_Z_VEL_MAX_UP`, `MPC_Z_VEL_MAX_DN`. PATCH сенсора сначала валидируется, затем выполняет полный drone-reset; PX4 запускается только после применения частоты. Изменение мира объединяет переданные поля с текущими значениями, удаляет все дроны через world-reset и применяет настройки. Следующий world-reset/reboot возвращает исходный SDF.

World-reset/reboot не создаёт runtime-дроны снова: Backend и Hub реестры очищаются, прежние публичные ID отвечают `404`. Drone-reset сохраняет публичный ID и намерение запуска PX4; параметры PX4 сбрасываются к defaults. Stop/start/restart PX4 сохраняют подтверждённые значения параметров. Изменение PX4 и создание/restart дрона на паузе возвращает `409 simulation_paused`. Удаление и сброс мира разрешены на паузе; мир возвращается к режиму исходного SDF. Все изменения мира/сенсоров/дронов относятся к последовательности внешних вызовов, а не транзакции; при неполном отказе запись остаётся `failed` с этапом и известными ресурсами.

Ошибки имеют вид `{"error":{"code":"...","message":"...","details":...}}`. `capabilities.flight_control`, `manual_control` и `missions` пока `false`. API не реализует missions, control, загрузку SDF и произвольные модели.

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

`tests/integration.py` проверяет живые create, PX4 parameter PATCH + stop/start/restart, sensor PATCH, drone-reset, паузу, world PATCH и reset. `tests/realtime_integration.py` проверяет два клиента одного канала, медленного читателя и invalidation при DELETE. Скрипты меняют мир; запускать без параллельных клиентов. Браузерный тест WHEP находится в `tests/webrtc_browser.js`, Dockerfile фиксирует Chrome и Playwright. В Docker Chrome получил и декодировал кадр 1280×720; два зрителя разделили одну публикацию, закрытие первого не остановило второго, уход последнего выключил камеру. RTSP/H.264 smoke запускается отдельно и не заменяет проверку браузера.

Сборка и запуск браузерной проверки:

```sh
docker build -f backend/tests/Dockerfile.browser -t uav-webrtc-browser:test backend/tests
docker run --rm --network uav-simulation uav-webrtc-browser:test
```

Проверка создаёт и удаляет runtime-дрон, получает настоящий кадр Chrome через WHEP, подключает двух зрителей к одной публикации и проверяет, что публикация завершается после ухода последнего. Запускать её последовательно с другими изменяющими мир тестами.

Backend не восстанавливает соответствия после собственного рестарта и не усыновляет найденные Hub/Gazebo ресурсы; `/system/status` показывает неучтённые модели и instances. Явный world-reset удаляет все Hub instances перед перезагрузкой Gazebo; при отказе удаления Gazebo не перезапускается. Автоматического rollback внешних вызовов и автоматического восстановления сервисов нет.
