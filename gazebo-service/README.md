# Gazebo Server API

Сервис запускает один Gazebo Server с одним миром, предоставляет HTTP API для управления миром, дронами и сенсорами, WebSocket для измерений и H.264/RTSP через MediaMTX для камер. Реализация использует Python, FastAPI и Gazebo Harmonic. PX4 SITL и MAVSDK не запускаются.

## Запуск

Из корня проекта:

```sh
docker compose up --build
```

Рабочая конфигурация находится в `../.env`, образец — в [`.env.example`](../.env.example). По умолчанию загружается мир `empty`, симуляция работает. На хосте не нужны Gazebo и GPU: контейнер использует headless Ogre2/EGL и Mesa software rendering. Текущий Dockerfile MediaMTX скачивает бинарник для Linux amd64.

| Назначение | Адрес по умолчанию |
| --- | --- |
| HTTP API | `http://localhost:8000` |
| OpenAPI UI / схема | `http://localhost:8000/docs` / `/openapi.json` |
| Готовность | `http://localhost:8000/healthz` |
| Видео | `rtsp://localhost:8554/drones/{drone-id}/sensors/{sensor-id}` |

Compose проверяет `/healthz`, ожидая запуска мира. `server-alive` отдельно возвращает `process_alive` и `world_ready`: живой процесс ещё не означает готовность Transport. Приложение запускается одним worker, поскольку процесс Gazebo и реестры принадлежат этому экземпляру сервиса. Дополнительную авторизацию HTTP API первая поставка не включает.

## Маршруты

Все пути ниже указаны полностью. `/api/v1/world/list` отсутствует: сервис управляет одним миром.

| Метод | Путь | Назначение |
| --- | --- | --- |
| GET | `/api/v1/server/server-alive/` | Состояние процесса и готовность мира |
| POST | `/api/v1/server/server-reboot/` | Перезапуск исходного мира и процесса Gazebo |
| WebSocket | `/api/v1/world/logs` | Live stdout/stderr Gazebo Server без истории |
| GET | `/api/v1/world` | Настройки, модели, свет, статистика, источники и возможности изменения |
| GET | `/api/v1/world/spawn-pads` | Каталог площадок, поверхности и позы спауна из SDF |
| PATCH | `/api/v1/world` | Частичное изменение поддерживаемых настроек |
| POST | `/api/v1/world/world-reset` | Восстановление мира через перезапуск исходного SDF |
| POST | `/api/v1/world/pause` | Пауза симуляции |
| POST | `/api/v1/world/resume` | Возобновление симуляции |
| GET / POST | `/api/v1/drones/` | Список / создание дрона |
| GET / DELETE | `/api/v1/drones/{drone-id}/` | Чтение актуального состояния / удаление |
| POST | `/api/v1/drones/{drone-id}/reset` | Восстановление начального состояния дрона |
| GET | `/api/v1/drones/{drone-id}/sensors/` | Список сенсоров |
| GET / PATCH | `/api/v1/drones/{drone-id}/sensors/{sensor-id}/` | Чтение / изменение частоты |
| POST | `/api/v1/drones/{drone-id}/sensors/{sensor-id}/reset` | Восстановление исходной частоты и очистка кеша |
| WebSocket | `/api/v1/drones/{drone-id}/sensors/{sensor-id}/stream` | Измерения обычного сенсора |
| WebSocket | `/api/v1/drones/{drone-id}/pose/stream` | Кешированная поза Gazebo не чаще 10 раз в секунду |
| POST | `/api/v1/drones/{drone-id}/sensors/{sensor-id}/activate` | Запуск видеопубликации камеры |
| POST | `/api/v1/drones/{drone-id}/sensors/{sensor-id}/deactivate` | Остановка видеопубликации камеры |

Поток поз использует уже работающую общую подписку Gazebo Transport `/world/{world}/pose/info`: HTTP handler читает только последний кешированный образец, не ждёт Gazebo Transport и ограничивает передачу 10 сообщениями в секунду. Событие содержит публичный Gazebo drone ID, entity ID, world generation, sequence, возраст образца, UTC `received_at` и полный XYZ/quaternion. Если источник pose/info временно недоступен или модель не найдена, WebSocket закрывается и клиенту следует перечитать состояние дрона и подписаться заново. Это внутренний транспортный контракт между Backend и Gazebo Service, не прямой endpoint для браузера.

Pause/resume и activate/deactivate идемпотентны. Создание дрона возвращает HTTP `201`. API-id дрона отделён от `entity_id` Gazebo; идентификатор сенсора используется в пределах своего дрона.

`GET /api/v1/world/spawn-pads` читает `<include>model://drone_pad</include>` и соответствующие
world frames `spawn_pad__<include-name>` из исходного SDF. Ответ содержит pose площадки,
surface pose, размер из box collision модели и явный `spawn_pose` для `x500_gimbal`.
Координаты — Gazebo XYZ, метры, quaternion; service подтверждает существование каждой
площадки в текущем `scene/info`. В `empty` `landing_pad_01` и `landing_pad_02` стоят
на X=-3 м и X=+3 м. Spawn-frame расположен на Z=0,292 м: коллизии исходного
`x500_gimbal` оставляют шасси на верхней поверхности 0,3 м с зазором 5 мм.
Площадки не обнаруживаются как runtime-дроны. Некорректные frames, ссылки, геометрия
или отсутствие хотя бы одной площадки блокируют старт Gazebo Service.

Внутренний `POST /api/v1/drones/` сохраняет контракт `model`, `name`, `pose`.
Выбор и эксклюзивное владение площадкой принадлежат публичному Backend.

## Шаг физики для PX4

Исходный `empty.sdf` использует шаг **0,001 с** и желаемую частоту 1000 Гц.
С шагом 0,004 с короткая миссия из production UI теряла устойчивость при изменении
курса: ULog показывал колебания угловых скоростей, затем переворот и посадку вне
станции. Тот же маршрут с шагом 0,001 с завершился RTL, посадкой и disarm в 0,17 м
от станции. API сохраняет возможность редактировать шаг; увеличение шага требует
новой физической полётной проверки. Успешные HTTP/reset проверки не доказывают
устойчивость полёта с произвольными physics. World-reset/reboot возвращают 1 мс.

## Запросы и текущие данные

### Мир

Пример частичного PATCH:

```json
{
  "physics": {"max_step_size": 0.004, "real_time_factor": 1.0},
  "gravity": {"x": 0, "y": 0, "z": -9.80665},
  "spherical_coordinates": {"latitude_deg": 43.2389, "longitude_deg": 76.8897}
}
```

Можно передать только нужные поля. Значения объединяются с текущими компонентами мира, включая изменения сторонних Transport-клиентов. PATCH временно ставит работающий мир на паузу и восстанавливает прежний режим; результат подтверждается чтением состояния.

| Настройка | Изменение через API | Ограничение |
| --- | --- | --- |
| `physics.max_step_size` | Да | Конечное положительное число |
| `physics.real_time_factor` | Да | Конечное положительное число; желаемый RTF, фактический зависит от нагрузки |
| `gravity` | Да | Конечный вектор `{x,y,z}` |
| `spherical_coordinates` | Да | Частичные `latitude_deg`, `longitude_deg`, `elevation`, `heading_deg`; только `EARTH_WGS84` |
| `magnetic_field` | Нет | Обработчик PhysicsCmd этой версии не применяет поле |
| `physics.real_time_update_rate` | Нет | Обработчик PhysicsCmd этой версии не применяет поле |
| Physics backend | Нет | Выбирается при загрузке мира |
| `atmosphere` | Нет | Runtime setter не реализован в интеграции |
| `scene/environment` | Нет | Изменение не реализовано в API |

GET читает physics, gravity, географическую привязку, магнитное поле и scene через текущий снимок компонентов `/world/{world}/state`. Позы моделей берутся из `pose/info`; `scene/info` используется для структуры сцены и света. `sources` указывает источник, актуальность и время симуляции для настроек.

`current_sdf` — отдельный экспорт Gazebo с `available` и `fresh: false`: экспорт этой версии содержит исходные настройки мира даже после runtime-изменений. Он не служит источником текущих physics/gravity. Atmosphere возвращается с пометкой `source: startup SDF`, `fresh: false`.

PATCH мира не является транзакцией. Если поздняя операция отказала, ранее подтверждённые изменения сохраняются. Ошибка содержит `details.applied` и `details.unconfirmed`. Неподдерживаемые поля отклоняются до отправки команд. Runtime-изменения не записываются в SDF.

### Дроны

Создание:

```json
{
  "model": "test_quad",
  "name": "quad_one",
  "pose": {
    "position": {"x": 0, "y": 0, "z": 2},
    "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}
  }
}
```

`model` — ключ установленного каталога; произвольные пути и загрузка SDF через API не принимаются. Имя и поза необязательны: по умолчанию генерируется имя, позиция равна `(0,0,1)`, ориентация единичная. Поставляются `test_quad`, `x500_gimbal` и модели из закреплённого PX4-gazebo-models.

Каталог и поиск включённых моделей используют только `MODEL_ROOTS`. Скрытых дополнительных каталогов сервиса нет; при переопределении `MODEL_ROOTS` модель `test_quad` доступна только если присутствует в выбранном каталоге.

Поза задаётся при создании и сохраняется как начальная поза для drone-reset. Публичного перемещения существующего дрона нет: после создания им управляет симуляция. GET дрона, список и позы моделей в GET мира используют актуальные позы из Gazebo.

Каждое API-создание получает уникальное внутреннее имя модели Gazebo, включая удаление и повторное создание с тем же пользовательским именем. Drone-reset также меняет внутреннее имя, сохраняя публичные `name` и `drone_id`. Это изолирует sensor topics от publishers старых моделей: при диагностике повторного создания `Drone-01` обнаружены три NavSat publisher на одном topic, а новый PX4 получал конфликтующие измерения. Поле `gazebo_model` — фактическое имя runtime-сущности для прямого наблюдения Gazebo; `name` остаётся логическим именем. Начальная поза, площадка и reset-семантика не меняются.

### Сенсоры и WebSocket

PATCH частоты:

```json
{"update_rate": 20}
```

Операция доступна, когда Gazebo объявляет соответствующий `/set_rate`. При ненулевом `update_rate` в SDF допустима частота `0 < rate <= исходная`; при исходном нуле допускается ноль и положительные значения. Ноль означает режим обновления сенсора, а не универсальную команду выключения.

Успех подтверждается последовательными интервалами измерений во времени симуляции с допуском на шаг физики. Ответ различает номинальный `update_rate`, `update_rate_source` и эффективный `observed_update_rate`. Последний sample и наблюдаемая частота доступны по мере получения данных подпиской или видеопубликацией. При паузе PATCH/reset частоты возвращает `409 simulation_paused` до отправки setter: нужно возобновить симуляцию. Таймаут подтверждения не доказывает, что команда не применена.

Пример адреса WebSocket:

```text
ws://localhost:8000/api/v1/drones/{drone-id}/sensors/imu_sensor/stream
```

Сообщение содержит `sensor_id`, `type`, `sim_time` и `data` с protobuf-данными, преобразованными в JSON. Например:

```json
{
  "sensor_id": "imu_sensor",
  "type": "imu",
  "sim_time": {"sec": "12", "nsec": 4000000},
  "data": {"header": {"stamp": {"sec": "12", "nsec": 4000000}}}
}
```

`sec` может быть строкой согласно JSON-представлению protobuf int64. Обычная Transport-подписка разделяется между клиентами. Очередь каждого клиента хранит один свежий sample; при паузе искусственные измерения не создаются. Drone-reset/delete закрывает прежние соединения с кодом `1008`, reboot/world-reset — с `1012`; после reset клиент подключается заново. Camera WebSocket отклоняется: внутренний видеоканал идёт по RTSP в MediaMTX, а браузерный WHEP-контракт предоставляет Backend.

## Reset и освобождение ресурсов

| Операция | Результат |
| --- | --- |
| Drone pose | Задаётся только при создании; текущая поза изменяется симуляцией, reset возвращает модель в начальную точку |
| Drone-reset | Удаляет модель и создаёт из сохранённого начального SDF и позы; API-id сохраняется, entity-id меняется. Физическое состояние и состояния плагинов создаются заново |
| Sensor-reset | Восстанавливает исходную частоту, очищает кеш и инвалидирует старые сообщения очереди; не сбрасывает шум, bias и калибровку |
| DELETE дрона | Останавливает камеры, снимает подписки, закрывает WebSocket и удаляет модель |
| World-reset / server-reboot | Останавливает публикации и подписки, завершает принадлежащую сервису группу процессов Gazebo и заново загружает исходный SDF. Сбрасывает время и настройки, восстанавливает исходные модели, удаляет runtime-дроны |
| Shutdown | Освобождает подписки, кодировщики и процесс Gazebo |

Перед drone-reset проверяется наличие сохранённого описания модели. Если оно недоступно, возвращается `409 reset_unsupported` до удаления. Если повторное создание отказало после удаления, ошибка `drone_recreate_failed` явно сообщает `removed: true`. Reset — последовательность операций, а не атомарное восстановление.

При drone-reset прежние записи сенсоров заменяются. Ранее активные камеры запускаются после готовности новых сенсоров. World-reset/reboot не восстанавливает публикации runtime-дронов.

## Видео и MediaMTX

Путь публикации: `drones/{drone-id}/sensors/{sensor-id}`. Поток проходит через Gazebo Image → ограниченную очередь rawvideo → FFmpeg/libx264 → RTSP с RTP/UDP → MediaMTX. Текущая реализация поддерживает RGB/BGR с тремя каналами и учитывает stride. Частота входа FFmpeg задана как 25 fps; encoder отправляет H.264 baseline, ключевой кадр раз в секунду и повторяет SPS/PPS, чтобы поздно подключившийся WebRTC-клиент начал декодирование без ожидания длинного GOP.

Первый RTSP-читатель запускает `runOnDemand`, который вызывает camera activate и остаётся запущенным на время demand. Читатели используют одну публикацию; через пять секунд после ухода последнего MediaMTX завершает hook и вызывает deactivate через `runOnUnDemand`. Activate/deactivate доступны также вручную; GET камеры возвращает `active` и `stream_url`.

Для браузера используйте WHEP URL из Backend `GET /api/v1/drones/{public-id}/sensors/{sensor-id}/video`; MediaMTX путь внутри URL построен по Gazebo ID. Compose публикует HTTP-порт `WEBRTC_PORT` и фиксированный ICE UDP-порт `WEBRTC_UDP_PORT`. По умолчанию MediaMTX объявляет только hostnames из `WEBRTC_ADDITIONAL_HOSTS`; укажите там имя/IP, достижимое браузером. `WEBRTC_ALLOWED_ORIGIN` задаёт разрешённый browser origin. MVP не настраивает TURN, поэтому клиент и MediaMTX должны иметь прямую UDP-доступность.

Пример получения одного кадра **в контейнере** после создания дрона:

```sh
docker compose exec -T gazebo-service ffmpeg \
  -rtsp_transport udp \
  -i 'rtsp://mediamtx:8554/drones/DRONE_ID/sensors/forward_camera' \
  -frames:v 1 -y /tmp/camera.jpg
```

Порт API MediaMTX `9997` доступен внутри Compose. Его учётные данные должны совпадать в `.env` (`MEDIAMTX_API_USER`, `MEDIAMTX_API_PASSWORD`) и [`mediamtx.yml`](../mediamtx/mediamtx.yml). Эти данные относятся к управлению MediaMTX, а не к авторизации HTTP API Gazebo.

## Конфигурация и версии

| Параметры | Назначение |
| --- | --- |
| `WORLD_FILE`, `WORLD_NAME` | SDF из корневой `worlds/` и имя мира внутри него |
| `API_PORT`, `RTSP_PORT`, `RTP_PORT`, `RTCP_PORT`, `HLS_PORT`, `WEBRTC_PORT`, `WEBRTC_UDP_PORT` | Публикуемые Compose порты |
| `WEBRTC_ALLOWED_ORIGIN`, `WEBRTC_ADDITIONAL_HOSTS`, `WEBRTC_IPS_FROM_INTERFACES` | Origin браузера и ICE адреса, публикуемые MediaMTX |
| `GZ_PARTITION` | Изоляция Gazebo Transport |
| `GZ_RENDER_ENGINE`, `GZ_VERBOSITY` | Рендеринг и уровень логов Gazebo |
| `STARTUP_TIMEOUT`, `GZ_REQUEST_TIMEOUT_MS`, `SENSOR_SAMPLE_TIMEOUT` | Ожидание старта, запросов и первого кадра камеры |
| `FFMPEG_PRESET` | Preset кодировщика H.264 |
| `MEDIAMTX_API_USER`, `MEDIAMTX_API_PASSWORD` | Доступ сервиса к API MediaMTX |
| `LOG_LEVEL` | Логирование приложения |

`WORLD_SDF`, `MODEL_ROOTS` и `GZ_SIM_RESOURCE_PATH` задаются в [`docker-compose.yml`](../docker-compose.yml). Все модели и их meshes хранятся в корневой `models/`, SDF миров — в `worlds/`. Образ копирует их в `/opt/uav/models` и `/opt/uav/worlds`; скачивания моделей при сборке больше нет. Каталог PX4-gazebo-models из прежнего закреплённого commit включён в `models/` с сохранением локального `x500_gimbal`; источник и лицензия описаны в [`models/README.md`](../models/README.md). Gazebo World-клиент остаётся в `gazebo-service/app`. Gazebo Service не использует внешний каталог исходников. Для собственного размещения SDF или каталога нужно менять также пути/тома Compose. Формат RTSP URL учитывает опубликованный `RTSP_PORT`; адрес читателя по умолчанию — `localhost`.

Закреплены основные зависимости:

| Компонент | Версия |
| --- | --- |
| Базовый образ | Ubuntu 24.04 |
| `gz-harmonic` | `1.0.0-1~noble` |
| `gz-sim8-cli` | `8.15.0-1~noble` |
| `python3-gz-transport13` | `13.6.0-1~noble` |
| `python3-gz-msgs10` | `10.4.0-1~noble` |
| FastAPI / Pydantic / Uvicorn | `0.115.12` / `2.11.4` / `0.34.2` |
| MediaMTX | `1.12.3` |
| PX4-gazebo-models | commit `a15af9628536914ff7201c992fce5e3cb5d70db9` |

Python-зависимости перечислены в [`requirements.txt`](requirements.txt). Все транзитивные apt/pip-зависимости отдельным lock-файлом не закреплены; тег Ubuntu также не закреплён digest в Dockerfile.

Сервис собирает небольшой [нативный адаптер Transport](app/native/transport_request.cc), освобождающий GIL во время блокирующего запроса и unsubscribe. Стандартный Python binding Transport 13 удерживает GIL при запросе, тогда как callbacks подписок требуют его, что приводило к таймаутам при активных подписках. Transport-запросы и ожидания выполняются вне HTTP event loop.

Для полётов с ненулевым `spherical_coordinates.heading_deg` образ также собирает исправленные NavSat и Magnetometer из исходников Gazebo Sim **8.15.0**, commit `446a44335a45b704b4d36dabcc5508ee34eeb3d8`. [Сборочный патч](build/patch_georeferenced_sensors.py) устраняет рассогласование сенсоров: штатный NavSat преобразует положение в WGS84, но выдаёт скорость в осях мира как географическую ENU; штатный Magnetometer не поворачивает географическое поле в оси мира. Исправление использует нативный `SphericalCoordinates::VelocityTransform` с `LOCAL2`, сохраняя соглашения магнитометра для PX4 v1.16. Версии Gazebo и PX4 не меняются.

В `empty.sdf` явно выбраны `simdrone-navsat-system` и `simdrone-magnetometer-system`, расположенные в `/opt/uav/georeferenced-plugins`; одноимённые штатные библиотеки не заменяются. Для своего SDF необходимо выбрать эти же два filename, сохранив имена классов `gz::sim::systems::NavSat` / `Magnetometer`. Исходники зависимости скачиваются только при сборке по закреплённому commit и удаляются после компиляции; каталога `third-party` не требуется. Публичный API, параметры геопривязки и reset-семантика сохраняются. Результаты реальных полётов: [flight-testing.md](../docs/flight-testing.md).

## Ошибки

Ошибки ресурсных API имеют формат:

```json
{"error": {"code": "drone_not_found", "message": "...", "details": null}}
```

Основные статусы: `404` — неизвестный объект; `422` — неверный ввод или неподдерживаемые поля; `409` — невозможная операция в текущем состоянии; `503` — недоступность Gazebo или отказ команды; `504` — неподтверждённый результат/таймаут. `/healthz` при неготовности возвращает стандартный FastAPI HTTP `503` с `detail`.

## Проверки в Docker

```sh
docker compose exec -T gazebo-service pytest -q /opt/uav/tests/test_contract.py
docker compose exec -T gazebo-service python /opt/uav/tests/regression.py
docker compose exec -T gazebo-service python /opt/uav/tests/smoke.py
```

Интеграционные скрипты изменяют и сбрасывают мир. Запускать их последовательно, без других клиентов, меняющих состояние.

| Проверка | Подтверждённое покрытие |
| --- | --- |
| `test_contract.py` | 15 тестов: валидация/API-контракт, сравнение quaternion, декодирование текущего состояния, безопасный отказ reset, инвалидация кеша, частичный отказ PATCH, идемпотентное закрытие SensorSubscription и EncoderSession, наличие локальных ресурсов и необходимых Gazebo bindings |
| `regression.py` | Отказ PATCH позы, текущие позы GET/list/world, внешние Transport-изменения и сохранение соседних настроек, реальная частота/reset сенсора, закрытие нескольких WebSocket при reset/delete, восстановление активной камеры, полный world-reset |
| `smoke.py` | Старт и готовность, pause/resume, создание с начальной позой/reset/delete, sensor WebSocket и rate PATCH, on-demand получение и декодирование H.264 через MediaMTX и остановка публикации, сенсоры x500_gimbal, отказ неподдерживаемого PATCH, world-reset и reboot |

Проверка 04.10.2026: Gazebo contract — **15 passed**, `regression.py` и `smoke.py` прошли на обновлённом контейнере с реальными Gazebo Transport и MediaMTX. Это подтвердило загрузку локальных world/model ресурсов, наблюдаемые изменения мира, сенсоры и H.264 RTSP. Браузерный Chrome ранее получил и декодировал WHEP H.264 кадр 1280×720; два клиента разделили одну публикацию, уход первого не прервал второго, уход последнего привёл к camera deactivate. Отдельные сценарии аварии FFmpeg, нескольких независимых камер и медленного Backend WebSocket-клиента ещё нужно расширить.

После исправления сенсорных координат при ненулевом heading повторно пересобран изолированный стенд: contract — **15/15**, `regression.py` и `smoke.py` прошли последовательно с настоящими Gazebo Transport и MediaMTX. Реальная миссия при сменённых origin/elevation/heading=90° прошла точки по порядку и села в **0,084 м** от станции. Базовые миссия и Offboard также проверялись; результаты и неуспешные диагностические прогоны перечислены в [flight-testing.md](../docs/flight-testing.md).

## Структура приложения

[`app/main.py`](app/main.py) содержит `create_app`, настройку lifespan и подключение маршрутов. Схемы, конфигурация и записи сущностей размещены в отдельных модулях. HTTP API разбит на routers в [`app/api`](app/api); сценарии мира, дронов, сенсоров и камер находятся в [`app/services`](app/services).

[`RuntimeCoordinator`](app/runtime.py) собирает сервисы и управляет поколением мира. Общие записи хранятся в `SessionState`; [`GazeboProcess`](app/gazebo/process.py) владеет группой процессов и логом; [`GazeboClient`](app/gazebo/client.py) скрывает детали Gazebo Transport и wrapper’а; [`PoseTracker`](app/gazebo/poses.py) владеет подпиской поз и импортирует вложенные protobuf-типы из канонического пакета `gz.msgs`, используемого сгенерированной схемой `Pose_V`; sensor service и camera service отвечают за свои подписки и FFmpeg. Модели и исходные частоты сенсоров читает [`ModelCatalog`](app/catalog.py). Нативный GIL-адаптер расположен в [`app/native`](app/native), декодер состояния — в [`app/gazebo/state.py`](app/gazebo/state.py).

Порядок разбиения и инварианты описаны в [плане рефакторинга](docs/refactoring-plan.md). Маршруты и их JSON-контракт остаются прежними; unit API теперь создаётся с fake runtime через `create_app`, без глобального singleton.

Проверка результата от 2026-10-02 выявила незавершённое разделение зависимостей и гонку закрытия Transport-подписки при одновременном подключении WebSocket-клиента. Текущие тесты проходят, но весь план пока не закрыт. Замечания и границы проверки описаны в [отчёте проверки рефакторинга](docs/refactoring-review.md).

## Live-логи процессов

`WS /api/v1/world/logs` передаёт только новые строки объединённых stdout/stderr Gazebo Server. История и replay отсутствуют; диагностические файлы продолжают записываться по прежним правилам и через этот API не доступны. Сообщение: `{"type":"log","source":"gazebo","received_at":"UTC ISO8601","message":"текст"}`. ANSI CSI-коды удаляются, некорректный UTF-8 заменяется; длинные строки делятся на фрагменты до 8192 байт. Severity не определяется.

Вывод непрерывно читается с момента запуска, даже без зрителей. У каждого подписчика очередь до 128 записей; при переполнении старые строки отбрасываются, перед следующими строками приходит `{"type":"gap","dropped":N}`. Это буфер доставки, а не история. При естественном завершении перед закрытием отдаётся уже считанный хвост вывода; при принудительном stop/reset буфер очищается. Закрытие потока использует код 1012; следующий запуск требует новой подписки. Пауза симуляции не останавливает чтение логов.

Тесты live-логов: `tests/test_live_logs.py` (fanout, отсутствие replay, переполнение, ограничение строк, непрерывное чтение настоящего дочернего процесса без подписчиков и cleanup). Эти тестовые процессы не заменяют интеграционную проверку Gazebo/PX4.

Проверка 03.10.2026 в Docker: contract + live logs — **18 passed**; `regression.py` и `smoke.py` прошли. Закрытие процесса отменяет заблокированную отправку WebSocket и освобождает подписчиков.

Повторная проверка live-логов 04.10.2026: contract/live-log tests — **20 passed**; реальный Gazebo stdout получен через Backend двумя подписчиками. `regression.py` и `smoke.py` прошли на обновлённом образе.

## Проверка единого каталога ресурсов 05.10.2026

`models/` и `worlds/` скопированы в пересобранный образ. На изолированном
`docker-compose.flight.yml` последовательно выполнены:

```sh
docker compose -f docker-compose.flight.yml build gazebo-service backend
docker compose -f docker-compose.flight.yml up -d --no-build gazebo-service mediamtx
docker compose -f docker-compose.flight.yml exec -T gazebo-service pytest -q /opt/uav/tests/test_contract.py
docker compose -f docker-compose.flight.yml exec -T gazebo-service python /opt/uav/tests/regression.py
docker compose -f docker-compose.flight.yml exec -T gazebo-service python /opt/uav/tests/smoke.py
docker compose -f docker-compose.flight.yml exec -T gazebo-service gz sdf -k /opt/uav/worlds/empty.sdf
```

Сборка выполнена с временным build.network=host overlay из-за DNS в Docker,
конфигурация сети приложения не изменялась. Contract: **17 passed**, regression
и smoke прошли: текущие позы, настройки мира, sensor stream/reset, cleanup,
восстановление камеры, world-reset и reboot; smoke декодировал H.264 RTSP.
Проверена загрузка обоих геопривязанных плагинов через maps процесса Gazebo.
SDF: Valid с прежним предупреждением о magnetic_field внутри physics.
Обе Compose-конфигурации прошли config --quiet. Все 172 перенесённых upstream
файла побайтно совпадают с закреплённым commit; исходное форматирование сохранено.
Браузерное видео и реальные PX4-полёты в этом изменении не запускались.
