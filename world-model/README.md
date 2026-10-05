# Пакет мира empty

`simdrone-empty` 1.0.0, Blender 5.2.2 LTS. Площадка 200×200 м,
центр (0, 0, 0), поверхность Z=0, без препятствий и runtime-дронов.

- `empty.blend` — исходник, ground_plane и sun, метры, применённые transforms.
- `world.glb` — статическая площадка со встроенным материалом, без внешних ресурсов.
- `world.sdf` — визуальная и collision plane, свет, physics, геопривязка и плагины
  исходного `gazebo-service/app/worlds/empty.sdf`.
- `manifest.json` — identity, bounds, матрицы осей, четыре контрольные точки,
  версия Blender, SHA-256 файлов.
- `export.py` — повторный экспорт; шаблон SDF хранится в свойствах сцены.
- `verify.py` — хеши, структура исходника и обратный импорт GLB.

Gazebo: XYZ, Z вверх, метры. GLB: `(x, y, z) = (X, Z, -Y)`.
В Three.js с Z вверх применить к корню GLB поворот +90° вокруг X
(`glb_to_gazebo`). Дроны используют Gazebo XYZ напрямую. Матрицы manifest
записаны строками; для Matrix4.set передавать элементы в порядке строк.
Bounds имеют нулевую высоту: это поверхность, не объём полёта.

Collision plane Gazebo бесконечна, как в исходном empty; визуальная площадка
ограничена ±100 м. Bounds не ограничивают полёт. SDF задаёт аналитическую
плоскость по размерам mesh Blender; отдельный collision mesh и текстуры не нужны.
Свет и фон GLB не содержит: браузер задаёт освещение отдельно.

## Экспорт и проверки

Из корня проекта:

```sh
~/Downloads/blender-5.2.2-linux-x64/blender --background world-model/empty.blend --python world-model/export.py
~/Downloads/blender-5.2.2-linux-x64/blender --background world-model/empty.blend --python world-model/verify.py
docker run --rm --entrypoint gz -v "$PWD/world-model:/world-model:ro" uav-gazebo-service:local sdf -k /world-model/world.sdf
```

Редактировать исходник, затем экспортировать. Поддерживается центрированная
горизонтальная прямоугольная плоскость Z=0; сложная геометрия требует расширения
экспортёра. При смене поставки обновить свойство сцены `package_version`.
Резервные `.blend1` не входят в поставку. Генерация через MCP использует отдельную
сцену и сохраняет ранее открытые сцены; повторный background экспорт сохраняет
обычный .blend с единственной сценой empty.

Compose уже монтирует manifest в Backend. GLB пока не подключён к сборке frontend.
Экспорт не меняет WORLD_SDF работающего сервиса; геометрия соответствует текущему empty.

## Результат 05.10.2026

Исходник повторно открыт Blender background, GLB импортирован обратно:
максимальная ошибка координат четырёх углов **0 м**, transforms, единицы и хеши
проверены. `gz sdf -k` в Docker: `Valid`, с унаследованным предупреждением
о `magnetic_field` внутри `physics` (элемент не описан схемой SDF).

Изолированный реальный `gz sim -s -r --headless-rendering` загрузил empty,
ground_plane 200×200 и sun, ответил `/world/empty/scene/info`.
В локальном образе отсутствуют `simdrone-navsat-system` и
`simdrone-magnetometer-system`, поэтому проверка всех плагинов не пройдена.
Для полной приёмки нужна пересборка актуального gazebo-service и проверка
пространственного совпадения в браузере с настоящими Gazebo/PX4.
Контакт/посадка, направление маркеров и browser rendering пока не проверены.
Пакет экспортирован; полная интеграционная приёмка остаётся открытой.
