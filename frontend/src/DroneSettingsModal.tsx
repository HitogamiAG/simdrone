import { useEffect, useState } from 'react'
import { Button, Group, Modal, NumberInput, Stack, Text } from '@mantine/core'

type Sensor = { id: string; name?: string; type?: string; update_rate?: number; capabilities?: string[] }
type Intent = { title: string; path: string; method?: string; body?: unknown; consequence: string } | null

export default function DroneSettingsModal({ droneId, onClose, onChanged }: { droneId: string | null; onClose: () => void; onChanged: (closeWindows?: boolean) => void }) {
  const [parameters, setParameters] = useState<Record<string, number>>({})
  const [sensors, setSensors] = useState<Sensor[]>([])
  const [rates, setRates] = useState<Record<string, number>>({})
  const [intent, setIntent] = useState<Intent>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [flightState, setFlightState] = useState<{ armed?: boolean; landed_state?: unknown; active?: { status?: string } | null; offboard?: { status?: string } | null } | null>(null)
  const base = `/api/v1/drones/${encodeURIComponent(droneId ?? '')}`

  useEffect(() => {
    if (!droneId) return
    let active = true
    Promise.all([fetch(`${base}/autopilot/parameters`).then(async (r) => r.ok ? r.json() : {}), fetch(`${base}/sensors/`).then(async (r) => r.ok ? r.json() : []), fetch(`${base}/flight`).then(async (r) => r.ok ? r.json() : null).catch(() => null)])
      .then(([px4, list, flight]) => { if (!active) return; const data = (px4.parameters ?? px4) as Record<string, number>; setParameters(data); setSensors(Array.isArray(list) ? list : []); setRates(Object.fromEntries((Array.isArray(list) ? list : []).map((sensor: Sensor) => [sensor.id, sensor.update_rate ?? 0]))); setFlightState(flight) })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : 'Не удалось загрузить настройки') })
    return () => { active = false }
  }, [droneId, base])

  const request = async (path: string, method = 'POST', body?: unknown) => {
    const response = await fetch(path, { method, headers: body === undefined ? undefined : { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
    const value = await response.json().catch(() => null) as { error?: { message?: string } } | null
    if (!response.ok) throw new Error(value?.error?.message ?? `HTTP ${response.status}`)
    return value
  }
  const executeIntent = async () => {
    if (!intent || busy) return
    setBusy(true); setError(null)
    try { await request(intent.path, intent.method, intent.body); const closeWindows = intent.path.endsWith('/reset') || intent.path.includes('/sensors/') || intent.path.endsWith('/autopilot/restart') || intent.path.endsWith('/autopilot/stop'); setIntent(null); onChanged(closeWindows) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Операция завершилась ошибкой') }
    finally { setBusy(false) }
  }
  const saveParameters = async () => {
    setBusy(true); setError(null)
    const allowed = ['MPC_XY_VEL_MAX', 'MPC_Z_VEL_MAX_UP', 'MPC_Z_VEL_MAX_DN'] as const
    const patch = Object.fromEntries(allowed.filter((key) => Number.isFinite(parameters[key]) && parameters[key] > 0).map((key) => [key, parameters[key]]))
    try { await request(`${base}/autopilot/parameters`, 'PATCH', patch); setError('Параметры приняты и подтверждены PX4; физический reset не выполнялся.') }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Не удалось изменить параметры') }
    finally { setBusy(false) }
  }
  const saveRate = async (sensor: Sensor) => {
    setIntent({ title: `Изменить частоту ${sensor.name ?? sensor.id}`, path: `${base}/sensors/${encodeURIComponent(sensor.id)}/`, method: 'PATCH', body: { update_rate: rates[sensor.id] }, consequence: 'Backend выполнит полный drone-reset, сохранит площадку и начальную позу, применит частоту и восстановит PX4.' })
  }
  const landed = JSON.stringify(flightState?.landed_state ?? '').toLowerCase()
  const airborneOrUnknown = flightState?.armed === true && !(landed.includes('on_ground') || landed === '"2"' || landed === '2')
  const flightBusy = !flightState || airborneOrUnknown || Boolean(flightState.active && !['completed', 'cancelled', 'failed', 'interrupted'].includes(flightState.active.status ?? '')) || Boolean(flightState.offboard && !['completed', 'cancelled', 'failed', 'interrupted'].includes(flightState.offboard.status ?? ''))
  return <>
    <Modal opened={Boolean(droneId)} onClose={onClose} title={`Настройки дрона · ${droneId ?? ''}`} size="lg" centered>
      <Stack>
        {error && <Text size="sm" c={error.startsWith('Параметры приняты') ? 'green' : 'red'}>{error}</Text>}
        <Text fw={600} size="sm">PX4 параметры (без физического reset)</Text>
        {(['MPC_XY_VEL_MAX', 'MPC_Z_VEL_MAX_UP', 'MPC_Z_VEL_MAX_DN'] as const).map((key) => <NumberInput key={key} label={key} min={0.1} decimalScale={2} value={parameters[key] ?? 0} onChange={(value) => setParameters((current) => ({ ...current, [key]: Number(value) }))} />)}
        <Button size="xs" loading={busy} disabled={!['MPC_XY_VEL_MAX', 'MPC_Z_VEL_MAX_UP', 'MPC_Z_VEL_MAX_DN'].some((key) => Number.isFinite(parameters[key]) && parameters[key] > 0)} onClick={() => void saveParameters()}>Применить параметры PX4</Button>
        <Text fw={600} size="sm" mt="sm">Сенсоры</Text>
        {sensors.map((sensor) => <Group key={sensor.id} align="end"><div style={{ flex: 1 }}><Text size="xs">{sensor.name ?? sensor.id} · {sensor.type ?? 'сенсор'}</Text><NumberInput size="xs" label="Частота, Гц" min={0.1} step={1} disabled={!sensor.capabilities?.includes('update_rate')} value={rates[sensor.id] ?? 0} onChange={(value) => setRates((current) => ({ ...current, [sensor.id]: Number(value) }))} /></div><Button size="xs" disabled={flightBusy || !sensor.capabilities?.includes('update_rate')} onClick={() => saveRate(sensor)}>Сохранить с reset</Button><Button size="xs" variant="default" disabled={flightBusy} onClick={() => setIntent({ title: `Сбросить сенсор ${sensor.name ?? sensor.id}`, path: `${base}/sensors/${encodeURIComponent(sensor.id)}/reset`, consequence: 'Выполнится полный drone-reset: поза, параметры сенсора и PX4 вернутся к сохранённому состоянию.' })}>Сброс сенсора</Button></Group>)}
        <Text fw={600} size="sm" mt="sm">PX4 lifecycle</Text>
        {flightBusy && <Text size="xs" c="yellow">Lifecycle блокирован: полёт/Offboard активен или подтверждённого состояния посадки нет. Используйте RTL/посадку из меню карты.</Text>}
        <Group grow>{(['start', 'stop', 'restart'] as const).map((action) => <Button key={action} size="xs" variant="default" disabled={action !== 'start' && flightBusy} onClick={() => setIntent({ title: `${action === 'start' ? 'Запустить' : action === 'stop' ? 'Остановить' : 'Перезапустить'} PX4`, path: `${base}/autopilot/${action}`, consequence: 'Операция затрагивает процессы PX4/MAVSDK. Текущее положение Gazebo не сбрасывается.' })}>{action === 'start' ? 'Запустить PX4' : action === 'stop' ? 'Остановить PX4' : 'Перезапустить PX4'}</Button>)}</Group>
        <Button color="red" variant="default" disabled={flightBusy} onClick={() => setIntent({ title: 'Сбросить дрон', path: `${base}/reset`, consequence: 'Модель будет пересоздана на закреплённой площадке; начальная поза, настройки сенсоров и параметры PX4 вернутся к сохранённым значениям.' })}>Сбросить дрон</Button>
      </Stack>
    </Modal>
    <Modal opened={Boolean(intent)} onClose={() => setIntent(null)} title={intent?.title} centered>
      <Stack><Text size="sm">{intent?.consequence}</Text>{error && <Text size="sm" c="red">{error}</Text>}<Group justify="flex-end"><Button variant="default" onClick={() => setIntent(null)}>Отмена</Button><Button color="orange" loading={busy} onClick={() => void executeIntent()}>Подтвердить</Button></Group></Stack>
    </Modal>
  </>
}
