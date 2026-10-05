# Модели

Единый каталог ресурсов проекта. Подкаталог модели содержит `model.sdf`,
`model.config` и локальные meshes/materials. Gazebo разрешает `model://` через
`/opt/uav/models`; Docker копирует каталог целиком. Модели выбираются API из
каталога, произвольные пути и загрузка SDF не добавлены.

`empty/` содержит исходник Blender, GLB и manifest участка, без `model.sdf`:
он не появляется в списке создаваемых дронов. Единственное описание мира
находится в [`worlds/empty.sdf`](../worlds/empty.sdf).

[`drone_pad/`](drone_pad/README.md) — локальная статическая площадка
2,5×2,5×0,3 м с ArUco `DICT_4X4_50`, ID `0`, размером 0,5 м.
Содержит Blender-исходник, GLB и готовые `model.sdf`/`model.config`;
подключается к миру через `<include><uri>model://drone_pad</uri></include>`.
World frame `spawn_pad__<имя include>` задаёт позу корня дрона над площадкой;
Gazebo Service публикует каталог через `/api/v1/world/spawn-pads`.

## Источники

`test_quad`, `gimbal_camera` и локальный `x500_gimbal` перенесены из сервиса
без изменения SDF и meshes. Остальные каталоги скопированы из PX4-gazebo-models:
https://github.com/PX4/PX4-gazebo-models/tree/a15af9628536914ff7201c992fce5e3cb5d70db9/models
Commit `a15af9628536914ff7201c992fce5e3cb5d70db9` совпадает с прежней Docker-зависимостью.
Upstream `x500_gimbal` не копировался поверх локального варианта. Сохранён полный
остальной каталог, чтобы перенос не сужал список моделей API и include-зависимости.
Лицензия upstream: [`PX4-LICENSE`](PX4-LICENSE). Новые версии ресурсов обновлять
отдельным изменением с указанием commit; сборка не скачивает модели из сети.
