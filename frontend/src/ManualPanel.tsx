import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { Button, Group, Modal, Stack, Text } from '@mantine/core'
import { useQuery } from '@tanstack/react-query'

type Session = { session_id: string; token: string; status: string; armed: boolean; input_timeout_s?: number; rtl_timeout_s?: number; limits?: Record<string, number> }
type Axes = { forward: number; right: number; up: number; yaw: number }
const emptyAxes: Axes = { forward: 0, right: 0, up: 0, yaw: 0 }

export default function ManualPanel({ droneId, enabled = true }: { droneId: string; enabled?: boolean }) {
  const [session, setSession] = useState<Session | null>(null)
  const [captured, setCaptured] = useState(false)
  const [armed, setArmed] = useState(false)
  const [status, setStatus] = useState('Управление не захвачено')
  const [confirmClose, setConfirmClose] = useState(false)
  const axes = useRef<Axes>(emptyAxes)
  const spaceHeld = useRef(false)
  const socket = useRef<WebSocket | null>(null)
  const focusTarget = useRef<HTMLDivElement>(null)
  const seq = useRef(0)
  const sessionRef = useRef<Session | null>(null)
  const capturedRef = useRef(false)
  const flight = useQuery({ queryKey: ['flight-state', droneId], queryFn: async () => { const response = await fetch(`/api/v1/drones/${encodeURIComponent(droneId)}/flight`); if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json() as Promise<{ armed?: boolean; landed_state?: unknown; offboard?: { status?: string } }> }, refetchInterval: session ? 1000 : false, retry: false })
  const stateArmed = typeof flight.data?.armed === 'boolean' ? flight.data.armed : armed
  const landed = JSON.stringify(flight.data?.landed_state ?? '').toLowerCase()
  const landedConfirmed = landed.includes('on_ground') || landed === '"2"' || landed === '2'

  const send = useCallback((value = axes.current) => {
    const ws = socket.current
    const current = sessionRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN || !current || !capturedRef.current || !armed) return
    ws.send(JSON.stringify({ seq: ++seq.current, ...value }))
  }, [armed])
  const release = () => { axes.current = emptyAxes; spaceHeld.current = false; send(emptyAxes); setCaptured(false); capturedRef.current = false }
  const disconnect = () => { release(); socket.current?.close(); socket.current = null }
  const keyboardDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!captured || !armed) return
    const target = event.target as HTMLElement | null
    if (target?.closest('input,textarea,select,[contenteditable=true]')) return
    const keys: Record<string, keyof Axes> = { w: 'forward', s: 'forward', a: 'right', d: 'right', r: 'up', f: 'up', q: 'yaw', e: 'yaw' }
    if (event.code !== 'Space' && !keys[event.key.toLowerCase()]) return
    event.preventDefault()
    if (event.code === 'Space') { spaceHeld.current = true; setStatus('Space удерживается; движение разрешено'); return }
    if (!spaceHeld.current) { setStatus('Удерживайте Space вместе с клавишей направления'); return }
    const key = event.key.toLowerCase(), axis = keys[key]
    const sign = key === 's' || key === 'a' || key === 'f' || key === 'q' ? -1 : 1
    axes.current = { ...axes.current, [axis]: sign }
    setStatus(`Передаётся команда ${key.toUpperCase()}`)
  }
  const keyboardUp = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!captured || !armed) return
    const keys: Record<string, keyof Axes> = { w: 'forward', s: 'forward', a: 'right', d: 'right', r: 'up', f: 'up', q: 'yaw', e: 'yaw' }
    if (event.code === 'Space') { spaceHeld.current = false; axes.current = emptyAxes; send(emptyAxes); setStatus('Space отпущен; переданы нулевые скорости'); return }
    const axis = keys[event.key.toLowerCase()]
    if (axis) { axes.current = { ...axes.current, [axis]: 0 }; setStatus('Клавиша отпущена; передана нулевая скорость') }
  }

  useEffect(() => { if (!enabled && capturedRef.current) { axes.current = emptyAxes; spaceHeld.current = false; send(emptyAxes); setCaptured(false); capturedRef.current = false; setStatus('Пакет карты изменился; управление остановлено') } }, [enabled, send])

  useEffect(() => {
    if (!captured) return
    const timer = window.setInterval(() => send(), 50)
    const blur = () => { axes.current = emptyAxes; spaceHeld.current = false; send(emptyAxes); setCaptured(false); capturedRef.current = false; setStatus('Фокус потерян; нажмите «Захватить управление» снова') }
    const visibility = () => { if (document.visibilityState === 'hidden') blur() }
    window.addEventListener('blur', blur)
    document.addEventListener('visibilitychange', visibility)
    return () => { window.clearInterval(timer); window.removeEventListener('blur', blur); document.removeEventListener('visibilitychange', visibility) }
  }, [captured, armed, send])

  useEffect(() => {
    if (!captured || !armed) return
    const keys: Record<string, keyof Axes> = { w: 'forward', s: 'forward', a: 'right', d: 'right', r: 'up', f: 'up', q: 'yaw', e: 'yaw' }
    const down = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target?.closest('input,textarea,select,[contenteditable=true]')) return
      if (event.code !== 'Space' && !keys[event.key.toLowerCase()]) return
      event.preventDefault()
      if (event.code === 'Space') { spaceHeld.current = true; return }
      if (!spaceHeld.current) return
      const key = event.key.toLowerCase(), axis = keys[key]
      const sign = key === 's' || key === 'a' || key === 'f' || key === 'q' ? -1 : 1
      axes.current = { ...axes.current, [axis]: sign }
    }
    const up = (event: KeyboardEvent) => {
      if (event.code === 'Space') { spaceHeld.current = false; axes.current = emptyAxes; send(emptyAxes); return }
      const key = event.key.toLowerCase(), axis = keys[key]
      if (axis) axes.current = { ...axes.current, [axis]: 0 }
    }
    document.addEventListener('keydown', down, true); document.addEventListener('keyup', up, true)
    return () => { document.removeEventListener('keydown', down, true); document.removeEventListener('keyup', up, true) }
  }, [captured, armed, send])

  useEffect(() => () => {
    socket.current?.close(); socket.current = null
    const current = sessionRef.current
    if (current) void fetch(`/api/v1/drones/${encodeURIComponent(droneId)}/flight/offboard/sessions/${encodeURIComponent(current.session_id)}`, { method: 'DELETE', keepalive: true })
  }, [droneId])

  const create = async () => {
    setStatus('Создание сессии…')
    try {
      const response = await fetch(`/api/v1/drones/${encodeURIComponent(droneId)}/flight/offboard/sessions`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ request_id: crypto.randomUUID() }) })
      const value = await response.json() as Session & { error?: { message?: string } }
      if (!response.ok) throw new Error(value.error?.message ?? `HTTP ${response.status}`)
      sessionRef.current = value; setSession(value)
      const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
      const ws = new WebSocket(`${protocol}//${location.host}/api/v1/drones/${encodeURIComponent(droneId)}/flight/offboard/sessions/${encodeURIComponent(value.session_id)}/control`)
      socket.current = ws
      ws.onopen = () => { ws.send(JSON.stringify({ token: value.token })); setStatus('Контроллер подключён; нажмите «Захватить управление»') }
      ws.onmessage = (event) => {
        const message = JSON.parse(event.data) as { type?: string; message?: string }
        if (message.type === 'error') setStatus(message.message ?? 'Ошибка потока управления')
      }
      ws.onclose = () => { setCaptured(false); capturedRef.current = false; if (sessionRef.current) setStatus('Связь управления потеряна') }
      setStatus('Сессия открыта; подключение контроллера…')
    } catch (error) { setStatus(error instanceof Error ? error.message : 'Не удалось открыть сессию') }
  }
  const action = async (name: 'arm' | 'disarm') => {
    if (!session) return
    const response = await fetch(`/api/v1/drones/${encodeURIComponent(droneId)}/flight/offboard/sessions/${encodeURIComponent(session.session_id)}/${name}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ request_id: crypto.randomUUID() }) })
    const result = await response.json() as { error?: { message?: string }; status?: string; armed?: boolean }
    if (!response.ok) { setStatus(result.error?.message ?? `HTTP ${response.status}`); return }
    setArmed(name === 'arm'); if (name === 'arm') focusTarget.current?.focus({ preventScroll: true }); setStatus(name === 'arm' ? 'Команда arm принята; подтверждайте фактическое состояние телеметрией' : 'Команда disarm принята')
  }
  const claim = () => {
    if (!socket.current || socket.current.readyState !== WebSocket.OPEN) { setStatus('WebSocket контроллера ещё не подключён'); return }
    capturedRef.current = true; setCaptured(true); focusTarget.current?.focus(); setStatus('Управление захвачено. Для движения удерживайте Space и WASD/RF/QE.')
  }
  const closeSession = async () => {
    if (!session) return
    disconnect()
    try {
      const response = await fetch(`/api/v1/drones/${encodeURIComponent(droneId)}/flight/offboard/sessions/${encodeURIComponent(session.session_id)}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      sessionRef.current = null; setSession(null); setArmed(false); setStatus('Сессия закрыта')
      setConfirmClose(false)
    } catch (error) { setStatus(error instanceof Error ? `Не удалось закрыть сессию: ${error.message}` : 'Не удалось закрыть сессию') }
  }
  const button = (label: string, axis: keyof Axes, value: number) => <Button size="xs" variant="default" disabled={!captured || !armed} onPointerDown={(event) => { event.preventDefault(); axes.current = { ...axes.current, [axis]: value } }} onPointerUp={() => { axes.current = { ...axes.current, [axis]: 0 } }} onPointerLeave={() => { axes.current = { ...axes.current, [axis]: 0 } }}>{label}</Button>

  return <Stack ref={focusTarget} tabIndex={-1} onKeyDownCapture={keyboardDown} onKeyUpCapture={keyboardUp} gap="sm" className="manual-panel">
    {!enabled && <Text size="sm" c="red">Ручное управление заблокировано: карта не согласована с пакетом Backend.</Text>}
    <Text size="xs" c="dimmed">{status}</Text>
    <Text size="xs" c="dimmed">Лимиты: XY {session?.limits?.horizontal_m_s ?? '—'} м/с · вверх {session?.limits?.up_m_s ?? '—'} · вниз {session?.limits?.down_m_s ?? '—'} · watchdog: ноль через 0,5 с, RTL через 5 с.</Text>
    {!session ? <Button size="xs" disabled={!enabled} onClick={() => void create()}>Открыть сессию</Button> : <>
      <Group grow><Button size="xs" variant={captured ? 'light' : 'default'} onClick={claim}>{captured ? 'Управление захвачено' : 'Захватить управление'}</Button><Button size="xs" variant="default" onClick={() => void action('arm')} disabled={!captured || stateArmed}>Arm</Button><Button size="xs" variant="default" onClick={() => void action('disarm')} disabled={!stateArmed || !landedConfirmed}>Disarm</Button></Group>
      <Text size="xs" c={flight.isError ? 'red' : 'dimmed'}>Наблюдаемое состояние PX4: armed={String(stateArmed)} · landed={landedConfirmed ? 'подтверждена' : 'не подтверждена'} · {flight.data?.offboard?.status ?? 'offboard —'}</Text>
      <Group justify="center">{button('↑ вперёд', 'forward', 1)}</Group><Group justify="center">{button('←', 'right', -1)}{button('↓', 'forward', -1)}{button('→', 'right', 1)}</Group><Group justify="center">{button('R вверх', 'up', 1)}{button('F вниз', 'up', -1)}{button('Q поворот', 'yaw', -1)}{button('E поворот', 'yaw', 1)}</Group>
      <Button size="xs" color="red" variant="default" onClick={() => setConfirmClose(true)}>Закрыть сессию</Button>
    </>}
    <Modal opened={confirmClose} onClose={() => setConfirmClose(false)} title="Закрыть Offboard-сессию?" centered>
      <Stack><Text size="sm">Backend может инициировать RTL, если дрон находится в воздухе. При потере связи setpoint прекращаются через 0,5 с, RTL запускается через 5 с.</Text><Group justify="flex-end"><Button variant="default" onClick={() => setConfirmClose(false)}>Остаться</Button><Button color="red" onClick={() => void closeSession()}>Закрыть сессию</Button></Group></Stack>
    </Modal>
  </Stack>
}
