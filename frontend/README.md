# Frontend платформы дронов

React 19 + TypeScript 7 + Vite 8, Mantine 9, Three.js / React Three Fiber, react-rnd, TanStack Query и Zustand. Детали интерфейса и известные ограничения: [`docs/frontend-plan.md`](../docs/frontend-plan.md).

## Запуск

```sh
npm ci
npm run dev
npm test
npm run build
```

Vite слушает `0.0.0.0:5173` и проксирует `/api` и WebSocket в Backend `localhost:8003`. Production образ собирает приложение и раздаёт его nginx на порту 80; Docker Compose публикует порт 3000. Nginx проксирует `/api` в Backend. MediaMTX WHEP остаётся прямым адресом.

Vitest + React Testing Library выполняют компонентные проверки в jsdom. Playwright входит в зафиксированный стек для последующих браузерных сценариев с реальными сервисами; e2e-набор пока не добавлен.

## Реализовано

Одно desktop workspace: сворачиваемая панель, accordion для дронов/миссий/мира, запросы к основным read API, выбор дрона с realtime позой, переключатель представления, pause/resume симуляции с перечитыванием состояния, заготовка resizable окон. Пакет `empty` экспортирован в [`world-model`](../world-model/README.md), но GLB ещё не подключён к браузерной карте и полная приёмка пакета не завершена.

Создание окон телеметрии, видео, логов и manual пока показывает оболочку/заглушку. Редактор миссий, lifecycle подтверждения, настоящая R3F загрузка принятого GLB, offboard, WebRTC и полные диагностические сценарии не реализованы.
