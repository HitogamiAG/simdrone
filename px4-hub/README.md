# PX4 Hub

PX4 Hub запускает до трёх независимых PX4 SITL и MAVSDK backend, каждый привязан к уже существующему `x500_gimbal` в Gazebo. Все процессы принадлежат одному FastAPI-контейнеру и одному Uvicorn worker. Используются PX4 v1.16.0 и gRPC-обёртка MAVSDK `mavsdk-grpc` 3.17.4. Начиная с `mavsdk` 4.x имя пакета обозначает native SDK без `mavsdk_server`, поэтому Hub использует отдельный пакет с прежним gRPC API.

## Запуск

Из корня репозитория:

```sh
docker compose up --build
```

Образ PX4 Hub собирает закреплённый commit PX4 и использует Gazebo Harmonic. PX4 Hub и Gazebo Service подключены к общей Compose bridge-сети и используют общий `GZ_PARTITION`. PX4 работает в standalone-режиме и подключается к точному имени модели Gazebo. Внешний порт API по умолчанию — `http://localhost:8002`; MAVLink и MAVSDK gRPC с хоста не публикуются.

Создание модели выполняется через Gazebo Service, затем создаётся её PX4 instance:

```sh
curl -X POST http://localhost:8000/api/v1/drones/ \
  -H 'Content-Type: application/json' \
  -d '{"model":"x500_gimbal","name":"drone-1"}'

curl -X POST http://localhost:8002/api/v1/instances/ \
  -H 'Content-Type: application/json' \
  -d '{"drone_id":"DRONE_API_ID"}'
```

Настройки Hub задаются в `.env.example`: `PX4_HUB_PORT`, `MAX_PX4_INSTANCES`, `PX4_STARTUP_TIMEOUT`, `PX4_STOP_TIMEOUT`, `GZ_PARTITION` и `LOG_LEVEL`. Максимум — три instance. PX4 logs и параметры хранятся в отдельном каталоге каждого instance внутри контейнера и удаляются при DELETE. Реестр в памяти; после перезапуска контейнера процессы не восстанавливаются. В production volume для логов сейчас не предусмотрен.

## API

| Метод | Путь | Назначение |
| --- | --- | --- |
| GET / POST | `/api/v1/instances/` | Список / создание для `drone_id`; профиль по умолчанию `x500_gimbal` |
| GET / DELETE | `/api/v1/instances/{id}/` | Состояние / остановка PX4 и MAVSDK; модель Gazebo сохраняется |
| POST | `/api/v1/instances/{id}/restart` | Перезапуск процессов с теми же slot и параметрами |
| GET / PATCH | `/api/v1/instances/{id}/parameters/` | Чтение / изменение разрешённых параметров |
| WebSocket | `/api/v1/instances/{id}/telemetry` | Поток телеметрии |
| GET | `/healthz` | Готовность процесса Hub, независимо от Gazebo |

Создание проверяет наличие модели `x500_gimbal` и работающего мира. PX4 instance numbers занимают свободные slots `0–2`; system ID равен slot + 1. MAVLink и gRPC endpoints каждому процессу выделяются из своих диапазонов. Одну Gazebo-модель нельзя привязать повторно.

Hub запускает выделенные MAVSDK и PX4 процессы, затем подключает Python-клиент к gRPC после обнаружения MAVLink-системы и ждёт позиционную телеметрию. Это сохраняет независимый backend для каждого instance и избегает взаимного ожидания старта PX4 и MAVSDK.

GET instance отдельно сообщает процессное состояние, привязку, доступность Gazebo, паузу, MAVSDK connection, свежесть телеметрии и `ready_to_arm`. Последний флаг является диагностикой PX4 health, а не гарантией успешной команды arm.

PATCH параметров разрешает только `MPC_XY_VEL_MAX`, `MPC_Z_VEL_MAX_UP` и `MPC_Z_VEL_MAX_DN`. Значения должны быть конечными положительными числами. Каждый setter подтверждается обратным чтением; при частичном отказе ошибка содержит поля `applied` и `unconfirmed`. Изменение параметров запрещено на паузе симуляции.

WebSocket публикует `armed`, `flight_mode`, `position`, `velocity`, `attitude`, `battery`, `gps`, `health` и `status_text` с временем получения. Клиенты имеют отдельные ограниченные очереди, поэтому медленный клиент не блокирует остальных. Для WS-протокола закреплён `websockets==15.0.1`. Flight commands, control и missions пока не реализованы; capabilities возвращают `false`.

## Reset и ограничения

DELETE instance завершает только PX4 и MAVSDK, не затрагивая Gazebo-модель. Restart перезапускает только эти процессы; физическая поза модели сохраняется, а PX4 parameters остаются в его рабочем каталоге.

Общий backend должен остановить связанные instances до drone/world reset, удаления или set_pose и восстановить их после операции. Hub обнаруживает пропажу модели или смену entity-id в диагностике, но сам не согласовывает действия и не создаёт новую привязку. Во время pause процессы остаются запущенными, watchdog не трактует отсутствие измерений как отказ; create/restart и PATCH параметров возвращают `409 simulation_paused`.

## Проверки

Контрактные тесты:

```sh
docker run --rm --network none \
  -v "$PWD/px4-hub:/opt/px4-hub:ro" \
  -e PYTHONPATH=/opt/px4-hub \
  --entrypoint /opt/venv/bin/pytest uav-gazebo-service:local \
  -q /opt/px4-hub/tests
```

Gazebo Service проверяется последовательно в том же Compose окружении:

```sh
docker compose exec -T gazebo-service pytest -q /opt/uav/tests/test_contract.py
docker compose exec -T gazebo-service python /opt/uav/tests/regression.py
docker compose exec -T gazebo-service python /opt/uav/tests/smoke.py
docker compose config --quiet
```

Реальные интеграционные сценарии запускаются последовательно, поскольку тесты Gazebo меняют мир. Проверки, выполненные в Docker 02.10.2026:

- PX4 SITL действительно обнаруживает `/world/empty/clock` через `gz-transport13-cli`, подключается к уже созданному `x500_gimbal` и возвращает `201` после свежей телеметрии позиции. Runtime-образ должен содержать как `gz-sim8-cli`, так и `gz-transport13-cli`.
- Одновременно запущены три SITL: system ID `1–3`, UDP `14540–14542`, gRPC `50051–50053`; instance GET показал живые парные процессы и свежую телеметрию. Четвёртый запрос получил `409 instance_limit`.
- PATCH `MPC_XY_VEL_MAX` подтвердился обратным чтением и сохранился после restart. Pause оставил процессы живыми; изменение параметра ответило `409 simulation_paused`, после resume телеметрия восстановилась.
- Принудительное завершение PX4 и, отдельно, MAVSDK переводило только соответствующий instance в `failed` и завершало его парный процесс; соседний instance оставался `running`.
- Два WebSocket-клиента получали телеметрию независимо. Отключение одного не остановило второго; DELETE закрыл поток и освободил процессы.
- World-reset после DELETE всех Hub instances вернул список дронов к `[]`.
- PX4 Hub: контрактные тесты — `5 passed`; Gazebo Service: `15 passed`, `regression.py` passed, `smoke.py` passed (включая H.264 RTSP); `docker compose config --quiet` прошёл.

Не проверялись конкурентное создание нескольких instances в один момент, поведение медленного WS-клиента в реальном потоке, а также согласование reset/delete/set_pose между Hub и Gazebo через будущий общий backend.

## Сборка образа и доступ к GitHub

Dockerfile разделён на общий Ubuntu/OSRF base, `px4-build` и `runtime`. В build-стадии загружается настоящий Git tag `v1.16.0`, проверяется commit `6ea3539157ca358c70a515878b77077af7d4611d`, затем инициализируются закреплённые в нём submodules для SITL (MAVLink, XRCE DDS, GPS, heatshrink, libevents и Gazebo). NuttX и другие симуляторы не скачиваются. Исходники PX4 не патчатся; локальные архивы и искусственная Git-история не используются.

Модели из build-submodule нужны штатной сборке PX4; это не меняет каталог моделей Gazebo Service и его закреплённую версию. В runtime переносятся `bin`, сгенерированный `etc` и шаблон `rootfs`. Компилятор, исходники и dev-пакеты остаются в build-стадии. `python3-venv` устанавливается явно. Число параллельных задач ограничено двумя; изменить его можно через `--build-arg BUILD_JOBS=4`.

Сборка с подробным выводом:

```sh
docker compose build --progress plain px4-hub
```

Проверка сети 02.10.2026: на хосте и в настоящем Docker BuildKit `RUN` одновременно выполнялся одинаковый `git fetch --progress --depth 1 https://github.com/PX4/PX4-Autopilot.git v1.16.0` в пустой репозиторий. На обе операции установлен одинаковый лимит 40 секунд; измерен размер поступившего pack-файла. Это замер передачи, а не завершённый clone.

| Среда | Получено за 40 секунд | Средняя скорость |
| --- | --- | --- |
| Хост | 184 459 263 байта (175,9 MiB) | 4,40 MiB/s |
| Docker build, стандартная сеть | 183 500 799 байт (175,0 MiB) | 4,38 MiB/s |

Разница составляет около 0,5%; замедление в build не воспроизведено. DNS в этом замере в обеих средах вернул `140.82.121.3`. HTTPS-загрузка одинакового `.gitmodules` с `raw.githubusercontent.com` заняла 1,32 секунды на хосте и 0,37 секунды в build. Скорость последних секунд Git менялась; процент объектов длительно оставался на 43%, хотя счётчик байтов рос. Количество объектов не отражает объём оставшихся данных, поэтому неподвижный процент не означает зависание сети. Специальные proxy, host networking или отключение TLS для сборки не требуются.

При проверке исправленного Dockerfile полный fetch PX4 получил 253,5 MiB примерно за 60 секунд; checkout подтвердил закреплённый commit. Fetch использует `--progress`, чтобы объём переданных данных был виден в Docker build-логе.

Проверки исправленного Dockerfile:

- `docker compose build px4-hub` — полная сборка прошла без патчей исходников PX4.
- Runtime-образ после добавления Transport CLI: 996 548 063 байта (около 950 MiB); Git и компилятор отсутствуют.
- `px4 -h`, наличие `rootfs/etc/init.d-posix/rcS`, `ldd` PX4 и наличие штатного `mavsdk_server` — проверены в собранном образе; отсутствующих библиотек нет.
- Изолированный контейнер (`docker run --network none uav-px4-hub:local`) запускается от пользователя `px4`; `/healthz` отвечает `200`, Docker healthcheck проходит, остановка контейнера завершает приложение штатно.
- `mavsdk-grpc==3.17.4` поставляет отдельный `mavsdk_server`; `mavsdk==4.0.3` больше не является gRPC-клиентом. `websockets==15.0.1` требуется Uvicorn для обслуживания telemetry WebSocket.
- `gz topic -l` из контейнера Hub показал `/world/empty/clock` и сенсорные топики модели; оба контейнера используют `GZ_PARTITION=uav-sim` и одну Compose-сеть.
- Полный интеграционный прогон Hub и Gazebo Service описан в разделе «Проверки» выше.
