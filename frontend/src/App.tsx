import { useEffect, useMemo, useState } from 'react'
import { Accordion, ActionIcon, Badge, Button, Group, Modal, Stack, Text, TextInput, Tooltip, MantineProvider } from '@mantine/core'
import { useDisclosure } from '@mantine/hooks'
import { Notifications, notifications } from '@mantine/notifications'
import { IconActivity, IconAntennaBars5, IconChevronLeft, IconChevronRight, IconCirclePlus, IconDrone, IconMap2, IconMaximize, IconMinus, IconPlayerPause, IconPlayerPlay, IconRefresh, IconRoute, IconSettings, IconVideo, IconX } from '@tabler/icons-react'
import { Rnd } from 'react-rnd'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { create } from 'zustand'
import './App.css'
import MapView, { type MapManifest } from './MapView'
import { worldGlbUrl, worldManifestUrl } from './mapAssets'
import { realtimeClient, type RealtimeEnvelope } from './realtime'
import TelemetryPanel from './TelemetryPanel'

type Resource = { id: string; name: string; spawn_pad_id?: string; status?: string; binding?: { generation?: number }; simulation?: { pose?: { position?: { x: number; y: number; z: number }; orientation?: { x: number; y: number; z: number; w: number } } }; [key: string]: unknown }
type SpawnPad = { id: string; name: string; model: string; availability: 'available' | 'occupied' | 'unavailable'; assigned_drone_id: string | null; unavailable_reason: string | null; surface_pose: { position: { x: number; y: number; z: number } }; spawn_pose: { position: { x: number; y: number; z: number }; orientation: { x: number; y: number; z: number; w: number } }; supported_models: string[] }
type SpawnPadCatalog = { world: string; world_generation: number; coordinate_system: string; units: string; pads: SpawnPad[] }
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
  const [padPickerMode, setPadPickerMode] = useState(false)
  const [droneMenu, setDroneMenu] = useState<{ id: string; x: number; y: number } | null>(null)
  const [droneName, setDroneName] = useState('Drone-01')
  const [selectedPadId, setSelectedPadId] = useState<string | null>(null)
  const [creatingDrone, setCreatingDrone] = useState(false)
  const [createDroneError, setCreateDroneError] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const [viewFollow, setViewFollow] = useState(false)
  const [mapZoom, setMapZoom] = useState(1)
  const [focusWorld, setFocusWorld] = useState(true)
  const [layers, setLayers] = useState({ drones: true, routes: true, pads: true, grid: true })
  const [poses, setPoses] = useState<Record<string, RealtimeEnvelope>>({})
  const [telemetry, setTelemetry] = useState<Record<string, Record<string, RealtimeEnvelope>>>({})
  const { selectedDrone, setSelectedDrone, view, setView, windows, openWindow, patchWindow, closeWindow } = useWorkspace()
  const status = useQuery({ queryKey: ['status'], queryFn: () => get<Record<string, unknown>>('/api/v1/system/status'), ...queryOptions })
  const world = useQuery({ queryKey: ['world'], queryFn: () => get<Record<string, unknown>>('/api/v1/world'), ...queryOptions })
  const drones = useQuery({ queryKey: ['drones'], queryFn: () => get<Resource[]>('/api/v1/drones/'), ...queryOptions })
  const missions = useQuery({ queryKey: ['missions'], queryFn: () => get<Resource[]>('/api/v1/missions/'), ...queryOptions })
  const map = useQuery({ queryKey: ['world-map'], queryFn: () => get<Record<string, unknown>>('/api/v1/world/map'), retry: false })
  const localMap = useQuery({ queryKey: ['local-world-manifest'], queryFn: () => get<MapManifest>(worldManifestUrl), retry: false, staleTime: Infinity })
  const localGlb = useQuery({ queryKey: ['local-world-glb'], queryFn: async () => { const response = await fetch(worldGlbUrl, { method: 'HEAD' }); if (!response.ok) throw new Error('GLB asset unavailable'); return true }, retry: false, staleTime: Infinity })
  const spawnPads = useQuery({ queryKey: ['spawn-pads'], queryFn: () => get<SpawnPadCatalog>('/api/v1/world/spawn-pads'), ...queryOptions })

  const droneRows = useMemo(() => Array.isArray(drones.data) ? drones.data : [], [drones.data])
  const missionRows = useMemo(() => Array.isArray(missions.data) ? missions.data : [], [missions.data])
  const poseDroneKeys = droneRows.filter((drone) => !['creating', 'resetting', 'deleting', 'failed'].includes(drone.status ?? '')).map((drone) => [drone.id, drone.binding?.generation ?? null] as const)
  const poseDroneIds = JSON.stringify(poseDroneKeys)
  useEffect(() => {
    const entries = JSON.parse(poseDroneIds) as Array<[string, number | null]>
    const cleanups = entries.map(([id, generation]) => realtimeClient.subscribe(`drone.${id}.pose`, (envelope) => {
      if (generation !== null && envelope.runtime_generation !== undefined && envelope.runtime_generation !== generation) return
      if (envelope.type === 'invalidated' || envelope.type === 'error') {
        setPoses((current) => { const next = { ...current }; delete next[id]; return next })
        return
      }
      setPoses((current) => ({ ...current, [id]: envelope }))
    }))
    return () => cleanups.forEach((cleanup) => cleanup())
  }, [poseDroneIds])
  const telemetryDroneIds = JSON.stringify([...new Set(windows.filter((item) => item.kind === 'telemetry').map((item) => item.drone))].flatMap((id) => {
    const drone = droneRows.find((item) => item.id === id)
    return drone && !['creating', 'resetting', 'deleting', 'failed'].includes(drone.status ?? '') ? [[id, drone.binding?.generation ?? null] as const] : []
  }))
  useEffect(() => {
    const entries = JSON.parse(telemetryDroneIds) as Array<[string, number | null]>
    const cleanups = entries.map(([id, generation]) => realtimeClient.subscribe(`drone.${id}.telemetry`, (envelope) => {
      if (generation !== null && envelope.runtime_generation !== undefined && envelope.runtime_generation !== generation) return
      if (envelope.type === 'invalidated' || envelope.type === 'error') {
        setTelemetry((current) => { const next = { ...current }; delete next[id]; return next })
        return
      }
      const sampleType = String(envelope.data?.type ?? envelope.type)
      setTelemetry((current) => ({ ...current, [id]: { ...current[id], [sampleType]: envelope } }))
    }))
    return () => cleanups.forEach((cleanup) => cleanup())
  }, [telemetryDroneIds])
  const servicesOk = status.isSuccess && !status.isError
  const padRows = spawnPads.data?.pads ?? []
  const selectedPad = padRows.find((pad) => pad.id === selectedPadId) ?? null
  const availablePads = padRows.filter((pad) => pad.availability === 'available' && pad.supported_models.includes('x500_gimbal'))
  const invalidDroneName = droneName.trim() !== '' && !/^[A-Za-z][A-Za-z0-9_-]{0,62}$/.test(droneName.trim())
  const localMatchesBackend = Boolean(localMap.data && map.data?.available && localMap.data.package_id === map.data.package_id && localMap.data.version === map.data.version && localMap.data.world_name === map.data.world_name && localMap.data.coordinate_system === map.data.coordinate_system)
  const mapReady = localMatchesBackend && localGlb.isSuccess
  const focusedDrone = droneRows.find((drone) => drone.id === selectedDrone)
  const selectedPoseEnvelope = selectedDrone ? poses[selectedDrone] : undefined
  const selectedPoseCurrent = selectedPoseEnvelope && (focusedDrone?.binding?.generation === undefined || selectedPoseEnvelope.runtime_generation === focusedDrone.binding.generation)
  const selectedPose = selectedPoseCurrent ? selectedPoseEnvelope?.data?.pose as NonNullable<Resource['simulation']>['pose'] : focusedDrone?.simulation?.pose
  const mapDrones = useMemo(() => droneRows.map((drone) => {
    const realtimePose = poses[drone.id]
    const poseIsCurrent = realtimePose && (drone.binding?.generation === undefined || realtimePose.runtime_generation === drone.binding.generation)
    return { ...drone, simulation: { ...drone.simulation, pose: poseIsCurrent ? realtimePose.data?.pose as NonNullable<Resource['simulation']>['pose'] : drone.simulation?.pose } }
  }), [droneRows, poses])
  const droneFocus = focusedDrone ? mapDrones.find((drone) => drone.id === focusedDrone.id)?.simulation?.pose?.position : undefined
  const cameraFocus: [number, number, number] = !focusWorld && droneFocus ? [droneFocus.x, droneFocus.y, droneFocus.z] : [0, 0, 0]

  const createDrone = async () => {
    if (!selectedPad || selectedPad.availability !== 'available' || creatingDrone) return
    setCreatingDrone(true)
    setCreateDroneError(null)
    try {
      const response = await fetch('/api/v1/drones/', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: droneName.trim() || undefined, model: 'x500_gimbal', spawn_pad_id: selectedPad.id }) })
      const result = await response.json().catch(() => null) as { id?: string; error?: { code?: string; message?: string; details?: Record<string, unknown> } } | null
      if (!response.ok) {
        const code = result?.error?.code
        const message = code === 'spawn_pad_occupied' ? 'Площадку уже занял другой дрон. Выберите другую.' : code === 'spawn_pad_unavailable' ? 'Площадка сейчас недоступна. Выберите другую.' : code === 'spawn_pad_not_found' ? 'Площадка больше не существует в текущем мире.' : result?.error?.message ?? `Не удалось создать дрон (${response.status}).`
        setCreateDroneError(message)
        await Promise.all([spawnPads.refetch(), drones.refetch()])
        return
      }
      await Promise.all([queryClient.invalidateQueries({ queryKey: ['drones'] }), queryClient.invalidateQueries({ queryKey: ['spawn-pads'] })])
      if (result?.id) setSelectedDrone(result.id)
      droneModal.close()
      setSelectedPadId(null)
      notifications.show({ color: 'green', title: 'Дрон создан', message: `Площадка ${selectedPad.name} закреплена за дроном.` })
    } catch {
      setCreateDroneError('Backend недоступен. Состояние создания не подтверждено; обновите список перед повторной попыткой.')
      await Promise.all([spawnPads.refetch(), drones.refetch()])
    } finally { setCreatingDrone(false) }
  }

  const setSimulation = async (action: 'pause' | 'resume') => {
    try {
      const response = await fetch(`/api/v1/world/${action}`, { method: 'POST' })
      if (!response.ok) {
        notifications.show({ color: 'red', title: 'Операция не принята', message: `Не удалось изменить состояние симуляции (${response.status}).` })
        return
      }
      await world.refetch()
      notifications.show({ color: 'green', title: 'Команда принята', message: 'Проверьте состояние симуляции после обновления.' })
    } catch {
      notifications.show({ color: 'red', title: 'Backend недоступен', message: 'Состояние симуляции не подтверждено.' })
    }
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
              <Group justify="space-between" mb="xs"><Text size="xs" c="dimmed">РЕСУРСЫ МИРА</Text><ActionIcon size="sm" aria-label="Создать дрон" onClick={() => { setCreateDroneError(null); setSelectedPadId(null); droneModal.open(); void spawnPads.refetch() }}><IconCirclePlus size={17} /></ActionIcon></Group>
              {drones.isLoading && <Text size="sm" c="dimmed">Загрузка…</Text>}
              {drones.isError && <InlineError message="Backend недоступен" onRetry={() => void drones.refetch()} />}
              {droneRows.map((drone) => <div className={`resource-row ${selectedDrone === drone.id ? 'selected' : ''}`} key={drone.id} onClick={() => { setSelectedDrone(drone.id); setFocusWorld(false) }} onContextMenu={(event) => { event.preventDefault(); setSelectedDrone(drone.id); openWindow(drone.id, 'telemetry') }}>
                <span className="drone-dot" /><div className="resource-main"><Text size="sm" fw={600}>{drone.name || drone.id}</Text><Text size="xs" c="dimmed">{String(drone.status ?? 'Состояние неизвестно')} · площадка {drone.spawn_pad_id ?? 'не указана'}</Text></div><Badge size="xs" variant="outline" color="gray">PX4</Badge>
              </div>)}
              {!droneRows.length && !drones.isLoading && !drones.isError && <EmptyHint text="В мире пока нет дронов" />}
            </Accordion.Panel></Accordion.Item>
            <Accordion.Item value="missions"><Accordion.Control icon={<IconRoute size={17} />}>Миссии <span className="count">{missionRows.length}</span></Accordion.Control><Accordion.Panel>
              <Group grow mb="sm"><Button size="xs" disabled leftSection={<IconCirclePlus size={14} />} variant="light" title="Редактор миссий будет подключён после редактора карты">Новая миссия</Button><Button size="xs" variant="default" onClick={() => void missions.refetch()}><IconRefresh size={14} /></Button></Group>
              {missionRows.map((mission) => <div className="mission-row" key={mission.id}><IconRoute size={16} /><div className="resource-main"><Text size="sm" fw={500}>{mission.name || mission.id}</Text><Text size="xs" c="dimmed">{String(mission.revision ? `Редакция ${mission.revision}` : 'Сохранённая миссия')}</Text></div></div>)}
              {!missionRows.length && !missions.isError && <EmptyHint text="Выберите сохранённую миссию или создайте новую" />}
              {missions.isError && <InlineError message="Не удалось загрузить миссии" onRetry={() => void missions.refetch()} />}
              <Text size="xs" c="dimmed" mt="sm">Редактор маршрута появится после подключения пакета участка.</Text>
            </Accordion.Panel></Accordion.Item>
            <Accordion.Item value="world"><Accordion.Control icon={<IconSettings size={17} />}>Настройки мира</Accordion.Control><Accordion.Panel>
              <Text size="sm" fw={600}>{String(world.data?.name ?? 'Gazebo')}</Text><Text size="xs" c="dimmed" mb="sm">{world.data?.simulation && typeof world.data.simulation === 'object' && 'paused' in world.data.simulation ? (world.data.simulation.paused ? 'Симуляция на паузе' : 'Симуляция выполняется') : 'Состояние недоступно'}</Text>
              <Group grow><Button size="xs" variant="default" disabled={!servicesOk || Boolean((world.data?.simulation as { paused?: boolean } | undefined)?.paused)} leftSection={<IconPlayerPause size={14} />} onClick={() => void setSimulation('pause')}>Пауза</Button><Button size="xs" variant="default" disabled={!servicesOk || !(world.data?.simulation as { paused?: boolean } | undefined)?.paused} leftSection={<IconPlayerPlay size={14} />} onClick={() => void setSimulation('resume')}>Продолжить</Button></Group>
              <Text size="xs" c="dimmed" mt="sm">Сброс и перезапуск будут доступны с подтверждением.</Text>
            </Accordion.Panel></Accordion.Item>
          </Accordion>
        </div>
        <button className="service-status" onClick={() => void status.refetch()}><span className={`status-dot ${servicesOk ? 'online' : 'offline'}`} /><span>{status.isLoading ? 'Проверка сервисов…' : servicesOk ? 'Сервисы доступны' : 'Нет связи с Backend'}</span><IconAntennaBars5 size={16} /></button>
      </>}
    </aside>

    <main className="workspace">
      <section className="map-stage" onClick={() => setDroneMenu(null)}>
        <div className="map-toolbar map-toolbar-left">
          <Group gap={0} className="segmented"><Button size="xs" variant={view === '2d' ? 'filled' : 'subtle'} onClick={() => setView('2d')}>2D</Button><Button size="xs" variant={view === '3d' ? 'filled' : 'subtle'} onClick={() => setView('3d')}>3D</Button></Group>
          <ActionIcon variant="default" aria-label="Уменьшить масштаб" onClick={() => setMapZoom((zoom) => Math.max(0.12, zoom * 0.8))}><IconMinus size={15} /></ActionIcon><Badge variant="outline" color="gray">{Math.round(mapZoom * 100)}%</Badge><ActionIcon variant="default" aria-label="Увеличить масштаб" onClick={() => setMapZoom((zoom) => Math.min(8, zoom * 1.25))}>+</ActionIcon><ActionIcon variant="default" aria-label="Весь участок" onClick={() => { setMapZoom(0.13); setFocusWorld(true); setViewFollow(false) }}><IconMaximize size={15} /></ActionIcon>
          {selectedDrone && <Button size="xs" variant="default" onClick={() => { setFocusWorld(false); setViewFollow(false) }}>Выбранный дрон</Button>}<Button size="xs" disabled={!selectedDrone} variant={viewFollow ? 'light' : 'default'} onClick={() => { setFocusWorld(false); setViewFollow(!viewFollow) }}>Следовать</Button>
        </div>
        <div className="map-toolbar map-toolbar-right">
          {Object.entries({ drones: layers.drones, routes: layers.routes, pads: layers.pads, grid: layers.grid }).map(([key, enabled]) => <Button key={key} size="xs" variant={enabled ? 'light' : 'default'} onClick={() => setLayers({ ...layers, [key]: !enabled })}>{key === 'drones' ? 'Дроны' : key === 'routes' ? 'Маршруты' : key === 'pads' ? 'Площадки' : 'Сетка'}</Button>)}
        </div>
        {mapReady && localMap.data && <MapView view={view} zoom={mapZoom} focus={cameraFocus} follow={viewFollow} manifest={localMap.data} pads={padRows} drones={mapDrones} selectedDrone={selectedDrone} showPads={layers.pads} showDrones={layers.drones} showGrid={layers.grid} onSelectDrone={(drone) => { setSelectedDrone(drone.id); setFocusWorld(false); setDroneMenu(null) }} onSelectPad={(pad) => { setSelectedPadId(pad.id); setFocusWorld(false); setCreateDroneError(null); setPadPickerMode(false); droneModal.open() }} onDroneContext={(drone, x, y) => { setSelectedDrone(drone.id); setFocusWorld(false); setDroneMenu({ id: drone.id, x, y }) }} onPointerMissed={() => { if (padPickerMode) { setPadPickerMode(false); droneModal.open() } }} />}
        {!mapReady && <div className="map-prerequisite"><IconMap2 size={34} stroke={1.4} /><Text fw={600} mt="md">{map.isError || localMap.isError || localGlb.isError ? 'Не удалось загрузить пакет участка' : map.data?.available && !localMatchesBackend ? 'Пакет карты не совпадает с Backend' : map.data?.available ? 'Загрузка модели участка…' : 'Пакет участка не подключён'}</Text><Text size="sm" c="dimmed" ta="center" maw={440}>{map.isError || localMap.isError || localGlb.isError ? 'Проверьте локальную поставку world.glb/manifest и доступность Backend.' : map.data?.available && !localMatchesBackend ? `Backend: ${String(map.data.package_id)} ${String(map.data.version)}; браузер: ${localMap.data?.package_id ?? 'manifest недоступен'} ${localMap.data?.version ?? ''}.` : 'Карта появится после загрузки локального Blender-пакета и сверки его версии с Backend.'}</Text><Badge variant="outline" color="yellow" mt="sm">Карта и управление полётом заблокированы</Badge></div>}
        {selectedDrone && <div className="selected-drone-chip"><span className="drone-dot" /><div><Text size="sm" fw={600}>Выбранный дрон</Text><Text size="xs" c="dimmed">{selectedDrone} · {selectedPoseCurrent ? `обновлено ${String(selectedPoseEnvelope?.data?.age_s ?? '—')} с назад` : 'ожидание позы'}</Text></div><ActionIcon variant="subtle" aria-label="Открыть телеметрию" onClick={() => openWindow(selectedDrone, 'telemetry')}><IconActivity size={16} /></ActionIcon></div>}
        <div className="coordinate-readout">XYZ Gazebo · м <span>{formatPosition(selectedPose ?? null)}</span></div>
        {padPickerMode && <div className="map-pick-hint" onClick={(event) => event.stopPropagation()}>Выберите площадку на карте <Button size="compact-xs" variant="subtle" onClick={() => { setPadPickerMode(false); droneModal.open() }}>Отмена</Button></div>}
        {droneMenu && <div className="drone-context-menu" style={{ left: Math.min(droneMenu.x, window.innerWidth - 220), top: Math.min(droneMenu.y, window.innerHeight - 230) }} onClick={(event) => event.stopPropagation()}><Text size="xs" c="dimmed">Действия · {droneRows.find((drone) => drone.id === droneMenu.id)?.name ?? droneMenu.id}</Text>{(['telemetry', 'video', 'logs', 'manual'] as const).map((kind) => <Button key={kind} variant="subtle" size="xs" fullWidth onClick={() => { openWindow(droneMenu.id, kind); setDroneMenu(null) }}>{windowTitle[kind]}</Button>)}{(['RTL', 'Посадка', 'Настройки', 'Reset', 'Удалить'] as const).map((label) => <Button key={label} variant="subtle" size="xs" fullWidth disabled title="Действие ещё не подключено">{label}</Button>)}</div>}
        <div className="floating-windows">{windows.map((window) => <Rnd key={window.id} bounds="parent" minWidth={window.kind === 'manual' ? 400 : 300} minHeight={window.kind === 'manual' ? 380 : 220} size={{ width: window.width, height: window.minimized ? 42 : window.height }} position={{ x: window.x, y: window.y }} style={{ zIndex: window.z }} onDragStop={(_, data) => patchWindow(window.id, { x: data.x, y: data.y })} onResizeStop={(_, __, ref, ___, position) => patchWindow(window.id, { width: ref.offsetWidth, height: ref.offsetHeight, ...position })} onMouseDown={() => patchWindow(window.id, { z: Math.max(0, ...windows.map((item) => item.z)) + 1 })} dragHandleClassName="window-titlebar">
            <div className="floating-window"><header className="window-titlebar"><div><Text size="sm" fw={600}>{windowTitle[window.kind]}</Text><Text size="xs" c="dimmed">{window.drone}</Text></div><Group gap={4}><ActionIcon size="sm" variant="subtle" aria-label="Свернуть окно" onClick={() => patchWindow(window.id, { minimized: !window.minimized })}>{window.minimized ? <IconMaximize size={14} /> : <IconMinus size={14} />}</ActionIcon><ActionIcon size="sm" variant="subtle" aria-label="Закрыть окно" onClick={() => closeWindow(window.id)}><IconX size={14} /></ActionIcon></Group></header>{!window.minimized && <div className="window-content">{window.kind === 'video' ? <div className="video-placeholder"><IconVideo /><Text size="sm" c="dimmed">Видео подключается через WHEP</Text></div> : window.kind === 'logs' ? <Text size="sm" c="dimmed">Ожидание сообщений…</Text> : window.kind === 'manual' ? <Text size="sm" c="dimmed">Захват управления · WASD / RF / QE</Text> : <TelemetryPanel pose={poses[window.drone]?.runtime_generation === droneRows.find((drone) => drone.id === window.drone)?.binding?.generation ? poses[window.drone] : undefined} samples={Object.fromEntries(Object.entries(telemetry[window.drone] ?? {}).filter(([, sample]) => sample.runtime_generation === droneRows.find((drone) => drone.id === window.drone)?.binding?.generation))} />}</div>}</div>
          </Rnd>)}</div>
        <div className="window-dock">{windows.map((window) => <Button key={window.id} size="xs" variant="default" onClick={() => patchWindow(window.id, { minimized: false, z: Math.max(...windows.map((item) => item.z)) + 1 })}>{windowTitle[window.kind]} · {window.drone}</Button>)}</div>
      </section>
    </main>
    <Modal opened={createDroneOpen} onClose={() => { if (!creatingDrone) { droneModal.close(); setCreateDroneError(null) } }} title="Создать дрон" centered>
      <Stack>
        <TextInput label="Имя" value={droneName} onChange={(event) => setDroneName(event.currentTarget.value)} maxLength={63} error={invalidDroneName ? 'Начните с латинской буквы; далее допустимы буквы, цифры, _ и - (до 63 символов).' : undefined} />
        <Text size="sm" c="dimmed">Модель: x500_gimbal</Text>
        <Group justify="space-between"><Text size="sm" fw={600}>Площадка спауна</Text>{mapReady && <Button size="compact-xs" variant="default" onClick={() => { droneModal.close(); setPadPickerMode(true) }}>Выбрать на карте</Button>}</Group>
        {spawnPads.isLoading && <Text size="sm" c="dimmed">Загрузка площадок…</Text>}
        {spawnPads.isError && <InlineError message="Не удалось загрузить площадки" onRetry={() => void spawnPads.refetch()} />}
        {spawnPads.isSuccess && !padRows.length && <EmptyHint text="В конфигурации мира нет площадок для спауна" />}
        {padRows.map((pad) => {
          const canSelect = pad.availability === 'available' && pad.supported_models.includes('x500_gimbal')
          const position = pad.surface_pose.position
          const owner = pad.assigned_drone_id ? droneRows.find((drone) => drone.id === pad.assigned_drone_id)?.name ?? pad.assigned_drone_id : null
          const reason = pad.availability === 'occupied' ? `Закреплена за ${owner}` : pad.availability === 'unavailable' ? `Недоступна: ${pad.unavailable_reason ?? 'причина не указана'}` : !pad.supported_models.includes('x500_gimbal') ? 'Модель x500_gimbal не поддерживается' : 'Свободна'
          return <button type="button" key={pad.id} className={`spawn-pad-option ${selectedPadId === pad.id ? 'selected' : ''}`} disabled={!canSelect || creatingDrone} aria-pressed={selectedPadId === pad.id} onClick={() => { setSelectedPadId(pad.id); setCreateDroneError(null) }}>
            <span className={`pad-status ${canSelect ? 'available' : ''}`} /><span className="pad-option-main"><Text size="sm" fw={600}>{pad.name}</Text><Text size="xs" c="dimmed">{reason}</Text><Text size="xs" c="dimmed">Поверхность XYZ · м: {position.x.toFixed(2)}, {position.y.toFixed(2)}, {position.z.toFixed(2)}</Text></span>{selectedPadId === pad.id && <Badge color="blue">Выбрана</Badge>}
          </button>
        })}
        {selectedPad && <Text size="xs" c="dimmed">Начальная поза дрона задаётся миром: XYZ {selectedPad.spawn_pose.position.x.toFixed(3)}, {selectedPad.spawn_pose.position.y.toFixed(3)}, {selectedPad.spawn_pose.position.z.toFixed(3)} м.</Text>}
        {selectedPad?.assigned_drone_id && <Button variant="default" onClick={() => { setSelectedDrone(selectedPad.assigned_drone_id); setFocusWorld(false); droneModal.close() }}>Выбрать владельца {droneRows.find((drone) => drone.id === selectedPad.assigned_drone_id)?.name ?? selectedPad.assigned_drone_id}</Button>}
        {spawnPads.isSuccess && availablePads.length === 0 && <Text size="xs" c="yellow">Сейчас нет доступных площадок. После удаления дрона список обновится автоматически.</Text>}
        {createDroneError && <Text role="alert" size="sm" c="red">{createDroneError}</Text>}
        <Group justify="flex-end"><Button variant="default" disabled={creatingDrone} onClick={droneModal.close}>Отмена</Button><Button loading={creatingDrone} disabled={!selectedPad || selectedPad.availability !== 'available' || invalidDroneName || spawnPads.isFetching} onClick={() => void createDrone()}>Создать</Button></Group>
      </Stack>
    </Modal>
  </div>
}

function formatPosition(pose: { position?: { x: number; y: number; z: number } } | null) { const p = pose?.position; return p ? `${p.x.toFixed(2)}, ${p.y.toFixed(2)}, ${p.z.toFixed(2)}` : '—' }
function EmptyHint({ text }: { text: string }) { return <div className="empty-hint">{text}</div> }
function InlineError({ message, onRetry }: { message: string; onRetry: () => void }) { return <Group justify="space-between"><Text size="xs" c="red">{message}</Text><Button size="compact-xs" variant="subtle" onClick={onRetry}>Повторить</Button></Group> }

export default function App() { return <MantineProvider defaultColorScheme="dark"><Notifications position="top-right" /><Workbench /></MantineProvider> }
