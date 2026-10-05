import { useEffect, useMemo, useState } from 'react'
import { Accordion, ActionIcon, Badge, Button, Group, Modal, NumberInput, Select, Stack, Text, TextInput, Tooltip, MantineProvider } from '@mantine/core'
import { useDisclosure } from '@mantine/hooks'
import { Notifications } from '@mantine/notifications'
import { IconActivity, IconAntennaBars5, IconChevronLeft, IconChevronRight, IconCirclePlus, IconDrone, IconMap2, IconMaximize, IconMinus, IconPlayerPause, IconPlayerPlay, IconRefresh, IconRoute, IconSettings, IconVideo, IconX } from '@tabler/icons-react'
import { Rnd } from 'react-rnd'
import { useQuery } from '@tanstack/react-query'
import { create } from 'zustand'
import './App.css'

type Resource = { id: string; name: string; [key: string]: unknown }
type FloatingWindow = { id: string; drone: string; kind: 'telemetry' | 'video' | 'logs' | 'manual'; minimized: boolean; x: number; y: number; width: number; height: number; z: number }
type WorkspaceState = { selectedDrone: string | null; view: '2d' | '3d'; windows: FloatingWindow[]; setSelectedDrone: (id: string | null) => void; setView: (view: '2d' | '3d') => void; openWindow: (drone: string, kind: FloatingWindow['kind']) => void; patchWindow: (id: string, patch: Partial<FloatingWindow>) => void; closeWindow: (id: string) => void }

const useWorkspace = create<WorkspaceState>((set) => ({
  selectedDrone: null, view: '2d', windows: [],
  setSelectedDrone: (selectedDrone) => set({ selectedDrone }), setView: (view) => set({ view }),
  openWindow: (drone, kind) => set((state) => {
    const id = `${drone}:${kind}`
    if (state.windows.some((window) => window.id === id)) return { windows: state.windows.map((window) => ({ ...window, z: window.id === id ? Math.max(...state.windows.map((item) => item.z)) + 1 : window.z, minimized: window.id === id ? false : window.minimized })) }
    const sizes = { telemetry: [360, 360], video: [480, 320], logs: [640, 320], manual: [440, 420] } as const
    const [width, height] = sizes[kind]
    return { windows: [...state.windows, { id, drone, kind, minimized: false, x: 48 + state.windows.length * 28, y: 90 + state.windows.length * 28, width, height, z: state.windows.length + 1 }] }
  }),
  patchWindow: (id, patch) => set((state) => ({ windows: state.windows.map((window) => window.id === id ? { ...window, ...patch } : window) })),
  closeWindow: (id) => set((state) => ({ windows: state.windows.filter((window) => window.id !== id) })),
}))

const get = async <T,>(path: string): Promise<T> => { const response = await fetch(path); if (!response.ok) throw new Error(`${response.status} ${response.statusText}`); return response.json() }
const queryOptions = { retry: 1, refetchInterval: 5000, staleTime: 2000 }

function Workbench() {
  const [collapsed, setCollapsed] = useState(false)
  const [active, setActive] = useState<string[]>(['drones', 'missions'])
  const [createDroneOpen, droneModal] = useDisclosure(false)
  const [positionMode, setPositionMode] = useState(false)
  const [droneName, setDroneName] = useState('Drone-01')
  const [yaw, setYaw] = useState('0')
  const [coords, setCoords] = useState({ x: 0, y: 0, z: 1 })
  const [viewFollow, setViewFollow] = useState(false)
  const [layers, setLayers] = useState({ drones: true, routes: true, grid: true })
  const [pose, setPose] = useState<Record<string, unknown> | null>(null)
  const { selectedDrone, setSelectedDrone, view, setView, windows, openWindow, patchWindow, closeWindow } = useWorkspace()
  const status = useQuery({ queryKey: ['status'], queryFn: () => get<Record<string, unknown>>('/api/v1/system/status'), ...queryOptions })
  const world = useQuery({ queryKey: ['world'], queryFn: () => get<Record<string, unknown>>('/api/v1/world'), ...queryOptions })
  const drones = useQuery({ queryKey: ['drones'], queryFn: () => get<Resource[]>('/api/v1/drones/'), ...queryOptions })
  const missions = useQuery({ queryKey: ['missions'], queryFn: () => get<Resource[]>('/api/v1/missions/'), ...queryOptions })
  const map = useQuery({ queryKey: ['world-map'], queryFn: () => get<Record<string, unknown>>('/api/v1/world/map'), retry: false })

  useEffect(() => {
    if (!selectedDrone) return
    let socket: WebSocket | undefined
    let closed = false
    const connect = () => {
      if (closed) return
      const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(`${protocol}//${location.host}/api/v1/realtime`)
      socket.onopen = () => socket?.send(JSON.stringify({ action: 'subscribe', channels: [`drone.${selectedDrone}.pose`] }))
      socket.onmessage = (event) => { try { const message = JSON.parse(event.data) as { channel?: string; data?: Record<string, unknown> }; if (message.channel?.endsWith('.pose')) setPose(message.data ?? null) } catch { /* ignore malformed messages */ } }
      socket.onclose = () => { if (!closed) window.setTimeout(connect, 1500) }
    }
    connect()
    return () => { closed = true; socket?.close() }
  }, [selectedDrone])

  const droneRows = useMemo(() => Array.isArray(drones.data) ? drones.data : [], [drones.data])
  const missionRows = useMemo(() => Array.isArray(missions.data) ? missions.data : [], [missions.data])
  const servicesOk = status.isSuccess && !status.isError
  const mapReady = false // Enable only when a local GLB is loaded and its control points are accepted.

  const createDrone = async () => {
    const yawRadians = Number(yaw) * Math.PI / 180
    const response = await fetch('/api/v1/drones/', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: droneName, model: 'x500_gimbal', pose: { position: { x: coords.x, y: coords.y, z: coords.z }, orientation: { x: 0, y: 0, z: Math.sin(yawRadians / 2), w: Math.cos(yawRadians / 2) } } }) })
    if (!response.ok) return
    await drones.refetch(); droneModal.close(); setPositionMode(false)
  }

  const windowTitle: Record<FloatingWindow['kind'], string> = { telemetry: 'Телеметрия', video: 'Видео', logs: 'Логи', manual: 'Ручное управление' }
  return <div className="app-shell">
    <aside className={`control-panel ${collapsed ? 'is-collapsed' : ''}`}>
      <header className="panel-header">
        {!collapsed && <div><div className="app-title">SIMDRONE</div><Text size="xs" c="dimmed">Рабочее пространство</Text></div>}
        <ActionIcon variant="subtle" aria-label={collapsed ? 'Развернуть панель' : 'Свернуть панель'} onClick={() => setCollapsed(!collapsed)}>{collapsed ? <IconChevronRight /> : <IconChevronLeft />}</ActionIcon>
      </header>
      {collapsed ? <nav className="collapsed-nav">
        <Tooltip label="Дроны"><ActionIcon aria-label="Дроны" onClick={() => { setCollapsed(false); setActive(['drones']) }}><IconDrone /></ActionIcon></Tooltip>
        <Tooltip label="Миссии"><ActionIcon aria-label="Миссии" onClick={() => { setCollapsed(false); setActive(['missions']) }}><IconRoute /></ActionIcon></Tooltip>
        <Tooltip label="Настройки мира"><ActionIcon aria-label="Настройки мира" onClick={() => { setCollapsed(false); setActive(['world']) }}><IconSettings /></ActionIcon></Tooltip>
      </nav> : <>
        <div className="panel-scroll">
          <Accordion multiple value={active} onChange={setActive} variant="separated" className="control-accordion">
            <Accordion.Item value="drones"><Accordion.Control icon={<IconDrone size={17} />}>Дроны <span className="count">{droneRows.length}</span></Accordion.Control><Accordion.Panel>
              <Group justify="space-between" mb="xs"><Text size="xs" c="dimmed">РЕСУРСЫ МИРА</Text><ActionIcon size="sm" aria-label="Создать дрон" onClick={droneModal.open}><IconCirclePlus size={17} /></ActionIcon></Group>
              {drones.isLoading && <Text size="sm" c="dimmed">Загрузка…</Text>}
              {drones.isError && <InlineError message="Backend недоступен" onRetry={() => void drones.refetch()} />}
              {droneRows.map((drone) => <div className={`resource-row ${selectedDrone === drone.id ? 'selected' : ''}`} key={drone.id} onClick={() => setSelectedDrone(drone.id)} onContextMenu={(event) => { event.preventDefault(); setSelectedDrone(drone.id); openWindow(drone.id, 'telemetry') }}>
                <span className="drone-dot" /><div className="resource-main"><Text size="sm" fw={600}>{drone.name || drone.id}</Text><Text size="xs" c="dimmed">{String(drone.status ?? 'Состояние неизвестно')}</Text></div><Badge size="xs" variant="outline" color="gray">PX4</Badge>
              </div>)}
              {!droneRows.length && !drones.isLoading && !drones.isError && <EmptyHint text="В мире пока нет дронов" />}
            </Accordion.Panel></Accordion.Item>
            <Accordion.Item value="missions"><Accordion.Control icon={<IconRoute size={17} />}>Миссии <span className="count">{missionRows.length}</span></Accordion.Control><Accordion.Panel>
              <Group grow mb="sm"><Button size="xs" leftSection={<IconCirclePlus size={14} />} variant="light">Новая миссия</Button><Button size="xs" variant="default" onClick={() => void missions.refetch()}><IconRefresh size={14} /></Button></Group>
              {missionRows.map((mission) => <div className="mission-row" key={mission.id}><IconRoute size={16} /><div className="resource-main"><Text size="sm" fw={500}>{mission.name || mission.id}</Text><Text size="xs" c="dimmed">{String(mission.revision ? `Редакция ${mission.revision}` : 'Сохранённая миссия')}</Text></div></div>)}
              {!missionRows.length && !missions.isError && <EmptyHint text="Выберите сохранённую миссию или создайте новую" />}
              {missions.isError && <InlineError message="Не удалось загрузить миссии" onRetry={() => void missions.refetch()} />}
              <Text size="xs" c="dimmed" mt="sm">Редактор маршрута появится после подключения пакета участка.</Text>
            </Accordion.Panel></Accordion.Item>
            <Accordion.Item value="world"><Accordion.Control icon={<IconSettings size={17} />}>Настройки мира</Accordion.Control><Accordion.Panel>
              <Text size="sm" fw={600}>{String(world.data?.name ?? 'Gazebo')}</Text><Text size="xs" c="dimmed" mb="sm">{String(world.data?.state ?? 'Состояние недоступно')}</Text>
              <Group grow><Button size="xs" variant="default" leftSection={<IconPlayerPause size={14} />}>Пауза</Button><Button size="xs" variant="default" leftSection={<IconPlayerPlay size={14} />}>Продолжить</Button></Group>
              <Text size="xs" c="dimmed" mt="sm">Сброс и перезапуск будут доступны с подтверждением.</Text>
            </Accordion.Panel></Accordion.Item>
          </Accordion>
        </div>
        <button className="service-status" onClick={() => void status.refetch()}><span className={`status-dot ${servicesOk ? 'online' : 'offline'}`} /><span>{status.isLoading ? 'Проверка сервисов…' : servicesOk ? 'Сервисы доступны' : 'Нет связи с Backend'}</span><IconAntennaBars5 size={16} /></button>
      </>}
    </aside>

    <main className="workspace">
      <section className={`map-stage ${layers.grid ? 'show-grid' : ''}`} onClick={(event) => { if (positionMode) { const rect = event.currentTarget.getBoundingClientRect(); setCoords({ x: Number(((event.clientX - rect.left) / rect.width * 100).toFixed(2)), y: Number(((event.clientY - rect.top) / rect.height * 100).toFixed(2)), z: 1 }); setPositionMode(false); droneModal.open() } }}>
        <div className="map-toolbar map-toolbar-left">
          <Group gap={0} className="segmented"><Button size="xs" variant={view === '2d' ? 'filled' : 'subtle'} onClick={() => setView('2d')}>2D</Button><Button size="xs" variant={view === '3d' ? 'filled' : 'subtle'} onClick={() => setView('3d')}>3D</Button></Group>
          <ActionIcon variant="default" aria-label="Уменьшить масштаб"><IconMinus size={15} /></ActionIcon><Badge variant="outline" color="gray">100%</Badge><ActionIcon variant="default" aria-label="Весь участок"><IconMaximize size={15} /></ActionIcon>
          <Button size="xs" variant={viewFollow ? 'light' : 'default'} onClick={() => setViewFollow(!viewFollow)}>Следовать</Button>
        </div>
        <div className="map-toolbar map-toolbar-right">
          {Object.entries(layers).map(([key, enabled]) => <Button key={key} size="xs" variant={enabled ? 'light' : 'default'} onClick={() => setLayers({ ...layers, [key]: !enabled })}>{key === 'drones' ? 'Дроны' : key === 'routes' ? 'Маршруты' : 'Сетка'}</Button>)}
        </div>
        <div className="map-prerequisite"><IconMap2 size={34} stroke={1.4} /><Text fw={600} mt="md">{mapReady ? String(map.data?.world_name ?? 'Участок') : map.data?.available ? 'Модель участка ожидает подключения' : 'Пакет участка не подключён'}</Text><Text size="sm" c="dimmed" ta="center" maw={440}>{mapReady ? 'Проверенный пакет участка.' : map.data?.available ? 'Backend сообщает о наличии manifest, но GLB-загрузчик включается после проверки контрольных точек и преобразования координат пакета.' : 'Для корректной карты нужен принятый Blender-пакет с world.glb и manifest. Дроны и маршруты появятся на общей модели участка после проверки координат с Gazebo.'}</Text><Badge variant="outline" color="yellow" mt="sm">Карта и управление полётом заблокированы</Badge></div>
        {selectedDrone && <div className="selected-drone-chip"><span className="drone-dot" /><div><Text size="sm" fw={600}>Выбранный дрон</Text><Text size="xs" c="dimmed">{selectedDrone} · {pose?.drone_id === selectedDrone ? `обновлено ${String(pose.age_s ?? '—')} с назад` : 'ожидание позы'}</Text></div><ActionIcon variant="subtle" aria-label="Открыть телеметрию" onClick={() => openWindow(selectedDrone, 'telemetry')}><IconActivity size={16} /></ActionIcon></div>}
        <div className="coordinate-readout">XYZ Gazebo · м <span>{coords.x.toFixed(2)}, {coords.y.toFixed(2)}, {coords.z.toFixed(2)}</span></div>
        {positionMode && <div className="map-pick-hint">Нажмите на карте, чтобы указать позицию <Button size="compact-xs" variant="subtle" onClick={(e) => { e.stopPropagation(); setPositionMode(false); droneModal.open() }}>Отмена</Button></div>}
        <div className="floating-windows">{windows.map((window) => <Rnd key={window.id} bounds="parent" minWidth={window.kind === 'manual' ? 400 : 300} minHeight={window.kind === 'manual' ? 380 : 220} size={{ width: window.width, height: window.minimized ? 42 : window.height }} position={{ x: window.x, y: window.y }} style={{ zIndex: window.z }} onDragStop={(_, data) => patchWindow(window.id, { x: data.x, y: data.y })} onResizeStop={(_, __, ref, ___, position) => patchWindow(window.id, { width: ref.offsetWidth, height: ref.offsetHeight, ...position })} onMouseDown={() => patchWindow(window.id, { z: Math.max(0, ...windows.map((item) => item.z)) + 1 })} dragHandleClassName="window-titlebar">
            <div className="floating-window"><header className="window-titlebar"><div><Text size="sm" fw={600}>{windowTitle[window.kind]}</Text><Text size="xs" c="dimmed">{window.drone}</Text></div><Group gap={4}><ActionIcon size="sm" variant="subtle" aria-label="Свернуть окно" onClick={() => patchWindow(window.id, { minimized: !window.minimized })}>{window.minimized ? <IconMaximize size={14} /> : <IconMinus size={14} />}</ActionIcon><ActionIcon size="sm" variant="subtle" aria-label="Закрыть окно" onClick={() => closeWindow(window.id)}><IconX size={14} /></ActionIcon></Group></header>{!window.minimized && <div className="window-content">{window.kind === 'video' ? <div className="video-placeholder"><IconVideo /><Text size="sm" c="dimmed">Видео подключается через WHEP</Text></div> : window.kind === 'logs' ? <Text size="sm" c="dimmed">Ожидание сообщений…</Text> : window.kind === 'manual' ? <Text size="sm" c="dimmed">Захват управления · WASD / RF / QE</Text> : <><Text size="xs" c="dimmed">ПОЗА GAZEBO</Text><Text size="sm">{pose ? JSON.stringify(pose.pose ?? pose) : 'Ожидание свежего образца'}</Text></>}</div>}</div>
          </Rnd>)}</div>
        <div className="window-dock">{windows.map((window) => <Button key={window.id} size="xs" variant="default" onClick={() => patchWindow(window.id, { minimized: false, z: Math.max(...windows.map((item) => item.z)) + 1 })}>{windowTitle[window.kind]} · {window.drone}</Button>)}</div>
      </section>
    </main>
    <Modal opened={createDroneOpen} onClose={() => { droneModal.close(); setPositionMode(false) }} title="Создать дрон" centered>
      <Stack><TextInput label="Имя" value={droneName} onChange={(event) => setDroneName(event.currentTarget.value)} /><Text size="sm" c="dimmed">Модель: x500_gimbal</Text><Text size="sm" fw={600}>Начальная позиция · XYZ Gazebo</Text><Group grow><NumberInput label="X" value={coords.x} onChange={(value) => setCoords({ ...coords, x: Number(value) || 0 })} /><NumberInput label="Y" value={coords.y} onChange={(value) => setCoords({ ...coords, y: Number(value) || 0 })} /><NumberInput label="Z" value={coords.z} min={0} onChange={(value) => setCoords({ ...coords, z: Number(value) || 0 })} /></Group><Select label="Ориентация" value={yaw} onChange={(value) => setYaw(value ?? '0')} data={[{ value: '0', label: 'Yaw 0°' }, { value: '90', label: 'Yaw 90°' }, { value: '180', label: 'Yaw 180°' }, { value: '270', label: 'Yaw 270°' }]} /><Button variant="default" disabled={!mapReady} onClick={() => { droneModal.close(); setPositionMode(true) }}>Указать на карте</Button>{!mapReady && <Text size="xs" c="yellow">Создание заблокировано до подключения пакета участка.</Text>}<Group justify="flex-end"><Button variant="default" onClick={droneModal.close}>Отмена</Button><Button disabled={!mapReady} onClick={() => void createDrone()}>Создать</Button></Group></Stack>
    </Modal>
  </div>
}

function EmptyHint({ text }: { text: string }) { return <div className="empty-hint">{text}</div> }
function InlineError({ message, onRetry }: { message: string; onRetry: () => void }) { return <Group justify="space-between"><Text size="xs" c="red">{message}</Text><Button size="compact-xs" variant="subtle" onClick={onRetry}>Повторить</Button></Group> }

export default function App() { return <MantineProvider defaultColorScheme="dark"><Notifications position="top-right" /><Workbench /></MantineProvider> }
