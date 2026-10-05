import { useEffect, useMemo, useRef, useState } from 'react'
import { Accordion, ActionIcon, Badge, Button, Group, Modal, NumberInput, Stack, Text, TextInput, Tooltip, MantineProvider } from '@mantine/core'
import { useDisclosure } from '@mantine/hooks'
import { Notifications, notifications } from '@mantine/notifications'
import { IconActivity, IconAntennaBars5, IconChevronLeft, IconChevronRight, IconCirclePlus, IconDrone, IconMap2, IconMaximize, IconMinus, IconPlayerPause, IconPlayerPlay, IconRefresh, IconRoute, IconSettings, IconX } from '@tabler/icons-react'
import { Rnd } from 'react-rnd'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { create } from 'zustand'
import './App.css'
import MapView, { type MapManifest } from './MapView'
import { worldGlbUrl, worldManifestUrl } from './mapAssets'
import { realtimeClient, type RealtimeEnvelope } from './realtime'
import LogsPanel from './LogsPanel'
import TelemetryPanel from './TelemetryPanel'
import VideoPanel from './VideoPanel'
import ManualPanel from './ManualPanel'
import DroneSettingsModal from './DroneSettingsModal'
import WorldSettingsModal from './WorldSettingsModal'
import MissionFlightModal from './MissionFlightModal'

type Resource = { id: string; name: string; spawn_pad_id?: string; status?: string; binding?: { generation?: number }; autopilot?: { status?: string }; simulation?: { pose?: { position?: { x: number; y: number; z: number }; orientation?: { x: number; y: number; z: number; w: number } } }; [key: string]: unknown }
type SpawnPad = { id: string; name: string; model: string; availability: 'available' | 'occupied' | 'unavailable'; assigned_drone_id: string | null; unavailable_reason: string | null; surface_pose: { position: { x: number; y: number; z: number } }; spawn_pose: { position: { x: number; y: number; z: number }; orientation: { x: number; y: number; z: number; w: number } }; supported_models: string[] }
type SpawnPadCatalog = { world: string; world_generation: number; coordinate_system: string; units: string; pads: SpawnPad[] }
type FloatingWindow = { id: string; drone: string; kind: 'telemetry' | 'video' | 'logs' | 'manual'; minimized: boolean; x: number; y: number; width: number; height: number; z: number }
type Waypoint = { x: number; y: number; z: number }
type Mission = { id: string; name: string; world: string; revision: number; waypoints: Waypoint[]; cruise_speed_m_s: number; takeoff_height_m: number; return_height_m: number }
type MissionDraft = Omit<Mission, 'id' | 'revision'> & { id?: string; revision?: number }
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
const missionDraft = (mission: Mission): MissionDraft => ({ id: mission.id, revision: mission.revision, name: mission.name, world: mission.world, waypoints: mission.waypoints.map((point) => ({ ...point })), cruise_speed_m_s: mission.cruise_speed_m_s, takeoff_height_m: mission.takeoff_height_m, return_height_m: mission.return_height_m })
const queryOptions = { retry: 1, refetchInterval: 5000, staleTime: 2000 }

function Workbench() {
  const [collapsed, setCollapsed] = useState(false)
  const [windowLayoutRestored, setWindowLayoutRestored] = useState(false)
  const [active, setActive] = useState<string[]>(['drones', 'missions'])
  const [createDroneOpen, droneModal] = useDisclosure(false)
  const [padPickerMode, setPadPickerMode] = useState(false)
  const [droneMenu, setDroneMenu] = useState<{ id: string; x: number; y: number } | null>(null)
  const [droneName, setDroneName] = useState('Drone-01')
  const [selectedPadId, setSelectedPadId] = useState<string | null>(null)
  const [creatingDrone, setCreatingDrone] = useState(false)
  const [createDroneError, setCreateDroneError] = useState<string | null>(null)
  const [missionDraftState, setMissionDraftState] = useState<MissionDraft | null>(null)
  const [missionSaving, setMissionSaving] = useState(false)
  const [missionError, setMissionError] = useState<string | null>(null)
  const [missionConflict, setMissionConflict] = useState(false)
  const [routeEditing, setRouteEditing] = useState(false)
  const [missionFlightOpen, setMissionFlightOpen] = useState(false)
  const [droneSettingsId, setDroneSettingsId] = useState<string | null>(null)
  const [worldSettingsOpen, setWorldSettingsOpen] = useState(false)
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false)
  const [pendingAction, setPendingAction] = useState<{ droneId: string; action: 'reset' | 'delete' } | null>(null)
  const [pendingWindowClose, setPendingWindowClose] = useState<FloatingWindow | null>(null)
  const [actionBusy, setActionBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const [viewFollow, setViewFollow] = useState(false)
  const [mapZoom, setMapZoom] = useState(1)
  const [focusWorld, setFocusWorld] = useState(true)
  const [layers, setLayers] = useState({ drones: true, routes: true, pads: true, grid: true })
  const [poses, setPoses] = useState<Record<string, RealtimeEnvelope>>({})
  const [telemetry, setTelemetry] = useState<Record<string, Record<string, RealtimeEnvelope>>>({})
  const [logs, setLogs] = useState<Record<string, RealtimeEnvelope[]>>({})
  const pendingLogs = useRef<Record<string, RealtimeEnvelope[]>>({})
  const logFlushTimer = useRef<number | null>(null)
  const { selectedDrone, setSelectedDrone, view, setView, windows, openWindow, patchWindow, closeWindow } = useWorkspace()
  const status = useQuery({ queryKey: ['status'], queryFn: () => get<Record<string, unknown>>('/api/v1/system/status'), ...queryOptions })
  const world = useQuery({ queryKey: ['world'], queryFn: () => get<Record<string, unknown>>('/api/v1/world'), ...queryOptions })
  const drones = useQuery({ queryKey: ['drones'], queryFn: () => get<Resource[]>('/api/v1/drones/'), ...queryOptions })
  const missions = useQuery({ queryKey: ['missions'], queryFn: () => get<Mission[]>('/api/v1/missions/'), ...queryOptions })
  const map = useQuery({ queryKey: ['world-map'], queryFn: () => get<Record<string, unknown>>('/api/v1/world/map'), retry: false })
  const localMap = useQuery({ queryKey: ['local-world-manifest'], queryFn: () => get<MapManifest>(worldManifestUrl), retry: false, staleTime: Infinity })
  const localGlb = useQuery({ queryKey: ['local-world-glb'], queryFn: async () => { const response = await fetch(worldGlbUrl, { method: 'HEAD' }); if (!response.ok) throw new Error('GLB asset unavailable'); return true }, retry: false, staleTime: Infinity })
  const spawnPads = useQuery({ queryKey: ['spawn-pads'], queryFn: () => get<SpawnPadCatalog>('/api/v1/world/spawn-pads'), ...queryOptions })

  const droneRows = useMemo(() => Array.isArray(drones.data) ? drones.data : [], [drones.data])
  const missionRows = useMemo(() => Array.isArray(missions.data) ? missions.data : [], [missions.data])
  useEffect(() => {
    if (!drones.isSuccess || windowLayoutRestored) return
    const droneIds = new Set(droneRows.map((drone) => drone.id))
    const minimums = { telemetry: [300, 240], video: [320, 220], logs: [360, 220], manual: [400, 380] } as const
    try {
      const stored = JSON.parse(localStorage.getItem('simdrone.window-layout.v1') ?? '[]') as unknown
      const restored = Array.isArray(stored) ? stored.filter((item): item is FloatingWindow => {
        if (!item || typeof item !== 'object') return false
        const window = item as Partial<FloatingWindow>
        if (!window.drone || (!(window.drone === 'world' && window.kind === 'logs') && !droneIds.has(window.drone)) || !window.id || !['telemetry', 'video', 'logs', 'manual'].includes(window.kind ?? '')) return false
        return [window.x, window.y, window.width, window.height, window.z].every((value) => typeof value === 'number' && Number.isFinite(value))
      }).map((window) => {
        const [minWidth, minHeight] = minimums[window.kind]
        return { ...window, width: Math.max(minWidth, window.width), height: Math.max(minHeight, window.height), minimized: Boolean(window.minimized) }
      }) : []
      useWorkspace.setState({ windows: restored })
    } catch { useWorkspace.setState({ windows: [] }) }
    queueMicrotask(() => setWindowLayoutRestored(true))
  }, [drones.isSuccess, droneRows, windowLayoutRestored])
  useEffect(() => {
    if (windowLayoutRestored) localStorage.setItem('simdrone.window-layout.v1', JSON.stringify(windows))
  }, [windows, windowLayoutRestored])
  useEffect(() => {
    if (!windowLayoutRestored) return
    const clamp = () => {
      const bounds = document.querySelector('.map-stage')?.getBoundingClientRect()
      if (!bounds) return
      useWorkspace.setState((state) => ({ windows: state.windows.map((window) => ({ ...window, x: Math.max(0, Math.min(window.x, Math.max(0, bounds.width - 96))), y: Math.max(0, Math.min(window.y, Math.max(0, bounds.height - 42))) })) }))
    }
    window.addEventListener('resize', clamp)
    const frame = window.requestAnimationFrame(clamp)
    return () => { window.cancelAnimationFrame(frame); window.removeEventListener('resize', clamp) }
  }, [collapsed, windowLayoutRestored])
  const missionDirty = Boolean(missionDraftState && (missionDraftState.id ? JSON.stringify(missionDraftState) !== JSON.stringify(missionRows.find((row) => row.id === missionDraftState.id) ? missionDraft(missionRows.find((row) => row.id === missionDraftState.id)!) : null) : true))
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
  const logDroneKeys = JSON.stringify([...new Set(windows.filter((item) => item.kind === 'logs').map((item) => item.drone))].flatMap((id) => {
    if (id === 'world') return [[id, null] as const]
    const drone = droneRows.find((item) => item.id === id)
    return drone?.autopilot?.status === 'running' ? [[id, drone.binding?.generation ?? null] as const] : []
  }))
  useEffect(() => {
    const entries = JSON.parse(logDroneKeys) as Array<[string, number | null]>
    const cleanups = entries.map(([id, generation]) => realtimeClient.subscribe(id === 'world' ? 'world.logs' : `drone.${id}.logs`, (envelope) => {
      if (generation !== null && envelope.runtime_generation !== undefined && envelope.runtime_generation !== generation) return
      if (envelope.type === 'invalidated') {
        delete pendingLogs.current[id]
        setLogs((current) => { const next = { ...current }; delete next[id]; return next })
        return
      }
      pendingLogs.current[id] = [...(pendingLogs.current[id] ?? []), envelope].slice(-2000)
      if (logFlushTimer.current === null) logFlushTimer.current = window.setTimeout(() => {
        const additions = pendingLogs.current
        pendingLogs.current = {}
        logFlushTimer.current = null
        setLogs((current) => {
          const next = { ...current }
          for (const [droneId, messages] of Object.entries(additions)) next[droneId] = [...(current[droneId] ?? []), ...messages].slice(-2000)
          return next
        })
      }, 100)
    }))
    return () => cleanups.forEach((cleanup) => cleanup())
  }, [logDroneKeys])
  useEffect(() => () => { if (logFlushTimer.current !== null) window.clearTimeout(logFlushTimer.current) }, [])
  const statusBackend = status.data?.backend as { ready?: boolean; world_configuration?: { ready?: boolean; reason?: string } | null } | undefined
  const servicesOk = status.isSuccess && statusBackend?.ready === true && statusBackend.world_configuration?.ready === true &&
    (status.data?.gazebo as { available?: boolean } | undefined)?.available === true &&
    (status.data?.px4_hub as { available?: boolean } | undefined)?.available === true &&
    (status.data?.mediamtx as { available?: boolean } | undefined)?.available === true
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

  const beginMission = () => {
    setMissionError(null); setMissionConflict(false)
    setMissionDraftState({ name: `Миссия ${missionRows.length + 1}`, world: String(world.data?.name ?? 'empty'), waypoints: [{ x: 0, y: 0, z: 10 }], cruise_speed_m_s: 2, takeoff_height_m: 10, return_height_m: 10 })
    setRouteEditing(false)
  }
  const editMission = (mission: Mission) => {
    setMissionError(null); setMissionConflict(false); setMissionDraftState(missionDraft(mission)); setRouteEditing(false)
  }
  const patchMission = (patch: Partial<MissionDraft>) => setMissionDraftState((draft) => draft ? { ...draft, ...patch } : draft)
  const saveMission = async () => {
    const draft = missionDraftState
    if (!draft || !draft.name.trim() || draft.waypoints.length < 1 || missionSaving) return
    setMissionSaving(true); setMissionError(null); setMissionConflict(false)
    const body = { name: draft.name.trim(), world: draft.world, waypoints: draft.waypoints, cruise_speed_m_s: draft.cruise_speed_m_s, takeoff_height_m: draft.takeoff_height_m, return_height_m: draft.return_height_m, ...(draft.id ? { expected_revision: draft.revision } : {}) }
    try {
      const response = await fetch(draft.id ? `/api/v1/missions/${encodeURIComponent(draft.id)}/` : '/api/v1/missions/', { method: draft.id ? 'PUT' : 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
      const result = await response.json().catch(() => null) as (Mission & { error?: { code?: string; message?: string } }) | null
      if (!response.ok) {
        if (response.status === 409 || result?.error?.code === 'mission_revision_conflict') setMissionConflict(true)
        throw new Error(result?.error?.message ?? `Не удалось сохранить миссию (${response.status})`)
      }
      setMissionDraftState(missionDraft(result!)); await queryClient.invalidateQueries({ queryKey: ['missions'] })
      notifications.show({ message: 'Миссия сохранена', color: 'green' })
    } catch (error) { setMissionError(error instanceof Error ? error.message : 'Не удалось сохранить миссию') }
    finally { setMissionSaving(false) }
  }
  const reloadMission = async () => {
    if (!missionDraftState?.id) return
    try { const latest = await get<Mission>(`/api/v1/missions/${encodeURIComponent(missionDraftState.id)}/`); setMissionDraftState(missionDraft(latest)); setMissionConflict(false); setMissionError(null); await queryClient.invalidateQueries({ queryKey: ['missions'] }) }
    catch (error) { setMissionError(error instanceof Error ? error.message : 'Не удалось перечитать миссию') }
  }

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

  const runLifecycleAction = async () => {
    if (!pendingAction || actionBusy) return
    const { droneId, action } = pendingAction
    setActionBusy(true); setActionError(null)
    try {
      const response = await fetch(`/api/v1/drones/${encodeURIComponent(droneId)}${action === 'reset' ? '/reset' : '/'}`, {
        method: action === 'delete' ? 'DELETE' : 'POST',
      })
      const result = await response.json().catch(() => null) as { error?: { message?: string } } | null
      if (!response.ok) throw new Error(result?.error?.message ?? `HTTP ${response.status}`)
      if (action === 'delete') {
        for (const item of windows.filter((window) => window.drone === droneId)) closeWindow(item.id)
        if (selectedDrone === droneId) setSelectedDrone(null)
      }
      if (action === 'reset' || action === 'delete') await Promise.all([queryClient.invalidateQueries({ queryKey: ['drones'] }), queryClient.invalidateQueries({ queryKey: ['spawn-pads'] })])
      else await queryClient.invalidateQueries({ queryKey: ['drones'] })
      setPendingAction(null)
      notifications.show({ color: 'green', title: 'Команда принята', message: 'Backend принял запрос. Наблюдайте фактическое состояние в списке и телеметрии.' })
    } catch (reason) { setActionError(reason instanceof Error ? reason.message : 'Операция не подтверждена') }
    finally { setActionBusy(false) }
  }
  const sendFlightAction = async (droneId: string, action: 'return' | 'land') => {
    try {
      const response = await fetch(`/api/v1/drones/${encodeURIComponent(droneId)}/flight/${action}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ request_id: crypto.randomUUID() }) })
      const result = await response.json().catch(() => null) as { error?: { message?: string } } | null
      if (!response.ok) throw new Error(result?.error?.message ?? `HTTP ${response.status}`)
      await queryClient.invalidateQueries({ queryKey: ['drones'] })
      notifications.show({ color: 'green', title: 'Команда принята', message: 'Результат полёта отслеживается по телеметрии и состоянию PX4.' })
    } catch (reason) { notifications.show({ color: 'red', title: 'Команда не принята', message: reason instanceof Error ? reason.message : 'Backend недоступен' }) }
  }

  const refreshAfterWorldOperation = async () => {
    setWorldSettingsOpen(false); setSelectedDrone(null); useWorkspace.setState({ windows: [] })
    await queryClient.invalidateQueries()
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
              {droneRows.map((drone) => <div className={`resource-row ${selectedDrone === drone.id ? 'selected' : ''}`} key={drone.id} onClick={() => { setSelectedDrone(drone.id); setFocusWorld(false) }} onContextMenu={(event) => { event.preventDefault(); setSelectedDrone(drone.id); setDroneMenu({ id: drone.id, x: event.clientX, y: event.clientY }) }}>
                <span className="drone-dot" /><div className="resource-main"><Text size="sm" fw={600}>{drone.name || drone.id}</Text><Text size="xs" c="dimmed">{String(drone.status ?? 'Состояние неизвестно')} · площадка {drone.spawn_pad_id ?? 'не указана'}</Text></div><Badge size="xs" variant="outline" color="gray">PX4</Badge>
              </div>)}
              {!droneRows.length && !drones.isLoading && !drones.isError && <EmptyHint text="В мире пока нет дронов" />}
            </Accordion.Panel></Accordion.Item>
            <Accordion.Item value="missions"><Accordion.Control icon={<IconRoute size={17} />}>Миссии <span className="count">{missionRows.length}</span></Accordion.Control><Accordion.Panel>
              <Group grow mb="sm"><Button size="xs" leftSection={<IconCirclePlus size={14} />} variant="light" onClick={beginMission}>Новая миссия</Button><Button size="xs" variant="default" aria-label="Обновить миссии" onClick={() => void missions.refetch()}><IconRefresh size={14} /></Button></Group>
              {missionRows.map((mission) => <button type="button" className={`mission-row ${missionDraftState?.id === mission.id ? 'selected' : ''}`} key={mission.id} onClick={() => editMission(mission)}><IconRoute size={16} /><div className="resource-main"><Text size="sm" fw={500}>{mission.name || mission.id}</Text><Text size="xs" c="dimmed">Редакция {mission.revision} · {mission.waypoints.length} точек</Text></div></button>)}
              {!missionRows.length && !missions.isError && <EmptyHint text="Выберите сохранённую миссию или создайте новую" />}
              {missions.isError && <InlineError message="Не удалось загрузить миссии" onRetry={() => void missions.refetch()} />}
              {missionDraftState && <Stack gap="xs" mt="md" className="mission-editor">
                <Group justify="space-between"><Text size="sm" fw={600}>{missionDraftState.id ? `Редакция ${missionDraftState.revision}` : 'Черновик миссии'}</Text><Badge size="xs" color={missionDirty ? 'yellow' : 'gray'}>{missionDirty ? 'Есть изменения' : 'Сохранено'}</Badge></Group>
                <TextInput size="xs" label="Название" value={missionDraftState.name} onChange={(event) => patchMission({ name: event.currentTarget.value })} maxLength={100} />
                <Group grow><NumberInput size="xs" label="Скорость, м/с" min={0.1} max={3} step={0.1} decimalScale={1} value={missionDraftState.cruise_speed_m_s} onChange={(value) => patchMission({ cruise_speed_m_s: Number(value) })} /><NumberInput size="xs" label="Взлёт, м" min={0.1} max={100} value={missionDraftState.takeoff_height_m} onChange={(value) => patchMission({ takeoff_height_m: Number(value) })} /></Group>
                <NumberInput size="xs" label="Высота возврата, м" min={0.1} max={100} value={missionDraftState.return_height_m} onChange={(value) => patchMission({ return_height_m: Number(value) })} />
                <Group justify="space-between"><Text size="xs" fw={600}>Маршрут · {missionDraftState.waypoints.length} точек</Text><Button size="compact-xs" variant="default" disabled={missionDraftState.waypoints.length >= 500} onClick={() => patchMission({ waypoints: [...missionDraftState.waypoints, { ...missionDraftState.waypoints.at(-1)! }] })}>Добавить</Button></Group>
                <Button size="xs" variant={routeEditing ? 'light' : 'default'} disabled={!mapReady} onClick={() => setRouteEditing((enabled) => !enabled)}>{routeEditing ? 'Завершить добавление на карте' : 'Добавлять точки кликом по карте'}</Button>
                {missionDraftState.waypoints.map((point, index) => <div className="waypoint-row" key={index}><Text size="xs" c="dimmed">{index + 1}</Text>{(['x', 'y', 'z'] as const).map((axis) => <NumberInput key={axis} size="xs" aria-label={`Точка ${index + 1} ${axis.toUpperCase()}`} value={point[axis]} decimalScale={2} onChange={(value) => { const waypoints = missionDraftState.waypoints.map((item, itemIndex) => itemIndex === index ? { ...item, [axis]: Number(value) } : item); patchMission({ waypoints }) }} />)}<Group gap={2}><ActionIcon size="sm" variant="subtle" aria-label={`Точку ${index + 1} выше`} disabled={index === 0} onClick={() => { const waypoints = [...missionDraftState.waypoints]; [waypoints[index - 1], waypoints[index]] = [waypoints[index], waypoints[index - 1]]; patchMission({ waypoints }) }}>↑</ActionIcon><ActionIcon size="sm" variant="subtle" aria-label={`Удалить точку ${index + 1}`} disabled={missionDraftState.waypoints.length <= 1} onClick={() => patchMission({ waypoints: missionDraftState.waypoints.filter((_, itemIndex) => itemIndex !== index) })}><IconX size={14} /></ActionIcon></Group></div>)}
                {missionError && <Text role="alert" size="xs" c="red">{missionError}</Text>}
                {missionConflict && <Button size="compact-xs" variant="default" onClick={() => void reloadMission()}>Перечитать серверную редакцию</Button>}
                {missionDraftState.id && !missionDirty && <Button size="xs" variant="light" onClick={() => setMissionFlightOpen(true)}>Проверить / запустить…</Button>}
                <Group grow><Button size="xs" variant="default" onClick={() => { setMissionDraftState(null); setRouteEditing(false) }}>Закрыть</Button><Button size="xs" loading={missionSaving} disabled={!missionDirty || !missionDraftState.name.trim() || missionDraftState.waypoints.some((point) => ![point.x, point.y, point.z].every(Number.isFinite)) || missionDraftState.cruise_speed_m_s <= 0 || missionDraftState.cruise_speed_m_s > 3 || missionDraftState.takeoff_height_m <= 0 || missionDraftState.return_height_m <= 0} onClick={() => void saveMission()}>{missionDraftState.id ? 'Сохранить' : 'Создать'}</Button></Group>
              </Stack>}
            </Accordion.Panel></Accordion.Item>
            <Accordion.Item value="world"><Accordion.Control icon={<IconSettings size={17} />}>Настройки мира</Accordion.Control><Accordion.Panel>
              <Text size="sm" fw={600}>{String(world.data?.name ?? 'Gazebo')}</Text><Text size="xs" c="dimmed" mb="sm">{world.data?.simulation && typeof world.data.simulation === 'object' && 'paused' in world.data.simulation ? (world.data.simulation.paused ? 'Симуляция на паузе' : 'Симуляция выполняется') : 'Состояние недоступно'}</Text>
              <Group grow><Button size="xs" variant="default" disabled={!servicesOk || Boolean((world.data?.simulation as { paused?: boolean } | undefined)?.paused)} leftSection={<IconPlayerPause size={14} />} onClick={() => void setSimulation('pause')}>Пауза</Button><Button size="xs" variant="default" disabled={!servicesOk || !(world.data?.simulation as { paused?: boolean } | undefined)?.paused} leftSection={<IconPlayerPlay size={14} />} onClick={() => void setSimulation('resume')}>Продолжить</Button></Group>
              <Group grow mt="sm"><Button size="xs" variant="default" onClick={() => openWindow('world', 'logs')}>Логи мира</Button><Button size="xs" variant="default" onClick={() => setWorldSettingsOpen(true)}>Настройки, сброс и reboot…</Button></Group>
            </Accordion.Panel></Accordion.Item>
          </Accordion>
        </div>
        <button className="service-status" onClick={() => { setDiagnosticsOpen(true); void status.refetch() }}><span className={`status-dot ${servicesOk ? 'online' : 'offline'}`} /><span>{status.isLoading ? 'Проверка сервисов…' : servicesOk ? 'Сервисы доступны' : 'Нет связи с Backend'}</span><IconAntennaBars5 size={16} /></button>
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
        {mapReady && localMap.data && <MapView view={view} zoom={mapZoom} focus={cameraFocus} follow={viewFollow} manifest={localMap.data} pads={padRows} drones={mapDrones} route={layers.routes ? missionDraftState?.waypoints ?? [] : []} routeEditing={routeEditing} selectedDrone={selectedDrone} showPads={layers.pads} showDrones={layers.drones} showGrid={layers.grid} onAddRoutePoint={(point) => { if (!missionDraftState) return; const z = missionDraftState.waypoints.at(-1)?.z ?? 10; patchMission({ waypoints: [...missionDraftState.waypoints, { ...point, z }] }) }} onMoveRoutePoint={(index, x, y) => { if (missionDraftState) patchMission({ waypoints: missionDraftState.waypoints.map((point, itemIndex) => itemIndex === index ? { ...point, x, y } : point) }) }} onSelectDrone={(drone) => { setSelectedDrone(drone.id); setFocusWorld(false); setDroneMenu(null) }} onSelectPad={(pad) => { setSelectedPadId(pad.id); setFocusWorld(false); setCreateDroneError(null); setPadPickerMode(false); droneModal.open() }} onDroneContext={(drone, x, y) => { setSelectedDrone(drone.id); setFocusWorld(false); setDroneMenu({ id: drone.id, x, y }) }} onPointerMissed={() => { if (padPickerMode) { setPadPickerMode(false); droneModal.open() } }} />}
        {!mapReady && <div className="map-prerequisite"><IconMap2 size={34} stroke={1.4} /><Text fw={600} mt="md">{map.isError || localMap.isError || localGlb.isError ? 'Не удалось загрузить пакет участка' : map.data?.available && !localMatchesBackend ? 'Пакет карты не совпадает с Backend' : map.data?.available ? 'Загрузка модели участка…' : 'Пакет участка не подключён'}</Text><Text size="sm" c="dimmed" ta="center" maw={440}>{map.isError || localMap.isError || localGlb.isError ? 'Проверьте локальную поставку world.glb/manifest и доступность Backend.' : map.data?.available && !localMatchesBackend ? `Backend: ${String(map.data.package_id)} ${String(map.data.version)}; браузер: ${localMap.data?.package_id ?? 'manifest недоступен'} ${localMap.data?.version ?? ''}.` : 'Карта появится после загрузки локального Blender-пакета и сверки его версии с Backend.'}</Text><Badge variant="outline" color="yellow" mt="sm">Карта и управление полётом заблокированы</Badge></div>}
        {selectedDrone && <div className="selected-drone-chip"><span className="drone-dot" /><div><Text size="sm" fw={600}>Выбранный дрон</Text><Text size="xs" c="dimmed">{selectedDrone} · {selectedPoseCurrent ? `обновлено ${String(selectedPoseEnvelope?.data?.age_s ?? '—')} с назад` : 'ожидание позы'}</Text></div><ActionIcon variant="subtle" aria-label="Открыть телеметрию" onClick={() => openWindow(selectedDrone, 'telemetry')}><IconActivity size={16} /></ActionIcon></div>}
        <div className="coordinate-readout">XYZ Gazebo · м <span>{formatPosition(selectedPose ?? null)}</span></div>
        {padPickerMode && <div className="map-pick-hint" onClick={(event) => event.stopPropagation()}>Выберите площадку на карте <Button size="compact-xs" variant="subtle" onClick={() => { setPadPickerMode(false); droneModal.open() }}>Отмена</Button></div>}
        {droneMenu && <div className="drone-context-menu" style={{ left: Math.min(droneMenu.x, window.innerWidth - 220), top: Math.min(droneMenu.y, window.innerHeight - 330) }} onClick={(event) => event.stopPropagation()}><Text size="xs" c="dimmed">Действия · {droneRows.find((drone) => drone.id === droneMenu.id)?.name ?? droneMenu.id}</Text>{(['telemetry', 'video', 'logs', 'manual'] as const).map((kind) => <Button key={kind} variant="subtle" size="xs" fullWidth disabled={kind === 'manual' && !mapReady} title={kind === 'manual' && !mapReady ? 'Нужен согласованный пакет карты' : undefined} onClick={() => { openWindow(droneMenu.id, kind); setDroneMenu(null) }}>{windowTitle[kind]}</Button>)}<Button variant="subtle" size="xs" fullWidth onClick={() => { setDroneSettingsId(droneMenu.id); setDroneMenu(null) }}>Настройки / PX4 / сенсоры</Button><Button variant="subtle" size="xs" fullWidth onClick={() => { void sendFlightAction(droneMenu.id, 'return'); setDroneMenu(null) }}>Возврат (RTL)</Button><Button variant="subtle" size="xs" fullWidth onClick={() => { void sendFlightAction(droneMenu.id, 'land'); setDroneMenu(null) }}>Посадка</Button>{(['reset', 'delete'] as const).map((action) => <Button key={action} variant="subtle" color="red" size="xs" fullWidth onClick={() => { setPendingAction({ droneId: droneMenu.id, action }); setActionError(null); setDroneMenu(null) }}>{action === 'reset' ? 'Сбросить дрон' : 'Удалить дрон'}</Button>)}</div>}
        <div className="floating-windows">{windows.map((window) => <Rnd key={window.id} bounds="parent" minWidth={window.kind === 'manual' ? 400 : window.kind === 'video' ? 320 : window.kind === 'logs' ? 360 : 300} minHeight={window.kind === 'manual' ? 380 : window.kind === 'telemetry' ? 240 : 220} size={{ width: window.width, height: window.minimized ? 42 : window.height }} position={{ x: window.x, y: window.y }} style={{ zIndex: window.z }} onDragStop={(_, data) => patchWindow(window.id, { x: data.x, y: data.y })} onResizeStop={(_, __, ref, ___, position) => patchWindow(window.id, { width: ref.offsetWidth, height: ref.offsetHeight, ...position })} onMouseDown={() => patchWindow(window.id, { z: Math.max(0, ...windows.map((item) => item.z)) + 1 })} dragHandleClassName="window-titlebar">
            <div className="floating-window"><header className="window-titlebar"><div><Text size="sm" fw={600}>{windowTitle[window.kind]}</Text><Text size="xs" c="dimmed">{window.drone === 'world' ? 'Gazebo · мир' : droneRows.find((drone) => drone.id === window.drone)?.name ?? window.drone}</Text></div><Group gap={4}><ActionIcon size="sm" variant="subtle" aria-label="Свернуть окно" onClick={() => patchWindow(window.id, { minimized: !window.minimized })}>{window.minimized ? <IconMaximize size={14} /> : <IconMinus size={14} />}</ActionIcon><ActionIcon size="sm" variant="subtle" aria-label="Закрыть окно" onClick={() => window.kind === 'manual' ? setPendingWindowClose(window) : closeWindow(window.id)}><IconX size={14} /></ActionIcon></Group></header>{!window.minimized && <div className="window-content">{window.kind === 'video' ? <VideoPanel droneId={window.drone} /> : window.kind === 'logs' ? <LogsPanel entries={logs[window.drone] ?? []} available={window.drone === 'world' || droneRows.find((drone) => drone.id === window.drone)?.autopilot?.status === 'running'} scope={window.drone === 'world' ? 'Gazebo logs' : 'PX4 logs'} onClear={() => { delete pendingLogs.current[window.drone]; setLogs((current) => ({ ...current, [window.drone]: [] })) }} /> : window.kind === 'manual' ? <ManualPanel droneId={window.drone} enabled={mapReady} /> : <TelemetryPanel pose={poses[window.drone]?.runtime_generation === droneRows.find((drone) => drone.id === window.drone)?.binding?.generation ? poses[window.drone] : undefined} samples={Object.fromEntries(Object.entries(telemetry[window.drone] ?? {}).filter(([, sample]) => sample.runtime_generation === droneRows.find((drone) => drone.id === window.drone)?.binding?.generation))} />}</div>}</div>
          </Rnd>)}</div>
        <div className="window-dock">{windows.map((window) => <Button key={window.id} size="xs" variant="default" onClick={() => patchWindow(window.id, { minimized: false, z: Math.max(...windows.map((item) => item.z)) + 1 })}>{windowTitle[window.kind]} · {window.drone === 'world' ? 'Gazebo' : window.drone}</Button>)}</div>
      </section>
    </main>
    <DroneSettingsModal droneId={droneSettingsId} onClose={() => setDroneSettingsId(null)} onChanged={(closeWindows = false) => { if (closeWindows && droneSettingsId) for (const item of windows.filter((window) => window.drone === droneSettingsId)) closeWindow(item.id); void Promise.all([queryClient.invalidateQueries({ queryKey: ['drones'] }), queryClient.invalidateQueries({ queryKey: ['spawn-pads'] })]) }} />
    <WorldSettingsModal opened={worldSettingsOpen} droneCount={droneRows.length} onClose={() => setWorldSettingsOpen(false)} onChanged={() => void refreshAfterWorldOperation()} />
    <MissionFlightModal mission={missionDraftState?.id && missionDraftState.revision ? { id: missionDraftState.id, name: missionDraftState.name, revision: missionDraftState.revision, waypoints: missionDraftState.waypoints } : null} drones={droneRows} opened={missionFlightOpen} mapReady={mapReady} paused={Boolean((world.data?.simulation as { paused?: boolean } | undefined)?.paused)} onClose={() => setMissionFlightOpen(false)} />
    <Modal opened={diagnosticsOpen} onClose={() => setDiagnosticsOpen(false)} title="Диагностика сервисов" centered>
      <Stack>
        {(['backend', 'gazebo', 'px4_hub', 'mediamtx'] as const).map((service) => {
          const detail = status.data?.[service] as Record<string, unknown> | undefined
          const worldConfig = service === 'backend' ? detail?.world_configuration as { ready?: boolean; reason?: string } | null | undefined : undefined
          const available = service === 'backend' ? detail?.ready === true && worldConfig?.ready === true : detail?.available === true
          const unmanagedDrones = detail?.unmanaged_drones
          const unmanagedInstances = detail?.unmanaged_instances
          return <Group key={service} justify="space-between" align="flex-start"><div><Text size="sm" fw={600}>{service === 'px4_hub' ? 'PX4 Hub' : service === 'mediamtx' ? 'MediaMTX' : service === 'gazebo' ? 'Gazebo' : 'Backend'}</Text><Text size="xs" c="dimmed">{String(detail?.error ?? worldConfig?.reason ?? detail?.reason ?? (service === 'backend' ? detail?.operation ?? 'API отвечает' : ''))}</Text>{Array.isArray(unmanagedDrones) && unmanagedDrones.length > 0 && <Text size="xs" c="yellow">Неучтённые модели: {JSON.stringify(unmanagedDrones)}</Text>}{Array.isArray(unmanagedInstances) && unmanagedInstances.length > 0 && <Text size="xs" c="yellow">Неучтённые PX4: {JSON.stringify(unmanagedInstances)}</Text>}</div><Badge color={available ? 'green' : 'red'}>{available ? 'Доступен' : 'Недоступен'}</Badge></Group>
        })}
        <Text size="xs" c="dimmed">Backend отвечает отдельно от готовности каждого сервиса. Операция: {String((status.data?.backend as Record<string, unknown> | undefined)?.operation ?? 'нет')}.</Text>
        <Button size="xs" variant="default" onClick={() => void status.refetch()}>Обновить диагностику</Button>
      </Stack>
    </Modal>
    <Modal opened={Boolean(pendingAction)} onClose={() => { if (!actionBusy) { setPendingAction(null); setActionError(null) } }} title={pendingAction?.action === 'delete' ? 'Удалить дрон?' : 'Сбросить дрон?'} centered>
      <Stack><Text size="sm">{pendingAction?.action === 'delete' ? 'Gazebo-модель и PX4 будут удалены. После подтверждённого удаления площадка освободится.' : 'Модель и PX4 будут пересозданы. Дрон вернётся на исходную позу закреплённой площадки; параметры сенсоров и PX4 сбросятся.'}</Text>{actionError && <Text size="sm" c="red">{actionError}</Text>}<Group justify="flex-end"><Button variant="default" disabled={actionBusy} onClick={() => setPendingAction(null)}>Отмена</Button><Button color="red" loading={actionBusy} onClick={() => void runLifecycleAction()}>Подтвердить</Button></Group></Stack>
    </Modal>
    <Modal opened={Boolean(pendingWindowClose)} onClose={() => setPendingWindowClose(null)} title="Закрыть ручное управление?" centered>
      <Stack><Text size="sm">Offboard-сессия будет закрыта. Если дрон находится в воздухе, Backend может инициировать RTL. При потере связи действуют таймеры watchdog: остановка setpoint через 0,5 с, RTL через 5 с.</Text><Group justify="flex-end"><Button variant="default" onClick={() => setPendingWindowClose(null)}>Остаться</Button><Button color="red" onClick={() => { if (pendingWindowClose) closeWindow(pendingWindowClose.id); setPendingWindowClose(null) }}>Закрыть сессию</Button></Group></Stack>
    </Modal>
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
