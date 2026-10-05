import { useRef, useState } from 'react'
import { Button, Group, Modal, Select, Stack, Text } from '@mantine/core'
import { useQuery } from '@tanstack/react-query'

type Mission = { id: string; name: string; revision: number; waypoints: { x: number; y: number; z: number }[] }
type Drone = { id: string; name: string; autopilot?: { status?: string } }
type Result = { error?: { message?: string }; execution_id?: string; valid?: boolean; [key: string]: unknown }

export default function MissionFlightModal({ mission, drones, opened, mapReady, paused, onClose }: { mission: Mission | null; drones: Drone[]; opened: boolean; mapReady: boolean; paused: boolean; onClose: () => void }) {
  const [droneId, setDroneId] = useState<string | null>(null)
  const [validation, setValidation] = useState<Result | null>(null)
  const [executionId, setExecutionId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [cancelConfirm, setCancelConfirm] = useState(false)
  const [startConfirm, setStartConfirm] = useState(false)
  const startRequestId = useRef<string | null>(null)
  const cancelRequestId = useRef<string | null>(null)
  const execution = useQuery({ queryKey: ['flight-execution', droneId, executionId], enabled: Boolean(droneId && executionId), queryFn: async () => { const response = await fetch(`/api/v1/drones/${encodeURIComponent(droneId!)}/flight/executions/${encodeURIComponent(executionId!)}`); const result = await response.json() as Result; if (!response.ok) throw new Error(result.error?.message ?? `HTTP ${response.status}`); return result }, refetchInterval: (query) => ['completed', 'cancelled', 'failed', 'interrupted'].includes(String(query.state.data?.status)) ? false : 1000, retry: false })
  const request = async (path: string, body: unknown) => {
    const response = await fetch(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
    const result = await response.json() as Result
    if (!response.ok) throw new Error(result.error?.message ?? `HTTP ${response.status}`)
    return result
  }
  const validate = async () => {
    if (!mission || !droneId) return
    setBusy(true); setError(null); setValidation(null)
    try { setValidation(await request(`/api/v1/missions/${encodeURIComponent(mission.id)}/validate?drone_id=${encodeURIComponent(droneId)}&revision=${mission.revision}`, {})) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Проверка не выполнена') }
    finally { setBusy(false) }
  }
  const start = async () => {
    if (!mission || !droneId || !validation) return
    setBusy(true); setError(null)
    try { startRequestId.current ??= crypto.randomUUID(); const result = await request(`/api/v1/drones/${encodeURIComponent(droneId)}/flight/missions`, { mission_id: mission.id, revision: mission.revision, request_id: startRequestId.current }); setExecutionId(String(result.execution_id)); setStartConfirm(false) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Запуск не принят') }
    finally { setBusy(false) }
  }
  const cancel = async () => {
    if (!droneId || !executionId) return
    setBusy(true); setError(null)
    try { cancelRequestId.current ??= crypto.randomUUID(); await request(`/api/v1/drones/${encodeURIComponent(droneId)}/flight/executions/${encodeURIComponent(executionId)}/cancel`, { request_id: cancelRequestId.current }); setCancelConfirm(false) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Отмена не принята') }
    finally { setBusy(false) }
  }
  return <>
    <Modal opened={opened} onClose={onClose} title="Проверка и запуск миссии" centered>
      <Stack>
        {mission && <Text size="sm">{mission.name} · редакция {mission.revision} · {mission.waypoints.length} точек</Text>}
        {!executionId && <Select label="Дрон с работающим PX4" placeholder="Выберите дрон" value={droneId} onChange={(value) => { setDroneId(value); setValidation(null) }} data={drones.map((drone) => ({ value: drone.id, label: `${drone.name} · ${drone.autopilot?.status ?? 'PX4 неизвестен'}`, disabled: drone.autopilot?.status !== 'running' }))} />}
        {paused && <Text size="sm" c="yellow">Симуляция на паузе: запуск недоступен.</Text>}
        {!mapReady && <Text size="sm" c="red">Пакет карты не согласован с Backend: полёт заблокирован.</Text>}
        {validation && <Stack gap={4}><Text size="sm" fw={600}>Результат проверки Backend</Text><Text size="xs" c="dimmed">{JSON.stringify(validation)}</Text><Text size="xs" c="yellow">Это проверка условий PX4 и домашней позиции, она не проверяет отсутствие столкновений.</Text></Stack>}
        {execution.data && <Stack gap={4}><Text size="sm" fw={600}>Исполнение: {String(execution.data.status ?? 'запущено')}</Text><Text size="xs">Фаза: {String(execution.data.phase ?? '—')} · точка {String(execution.data.current_waypoint ?? '—')} / {String(execution.data.total_waypoints ?? mission?.waypoints.length ?? '—')}</Text><Text size="xs" c="dimmed">{JSON.stringify(execution.data)}</Text></Stack>}
        {execution.isError && <Text size="sm" c="red">Не удалось получить статус исполнения</Text>}
        {error && <Text role="alert" size="sm" c="red">{error}</Text>}
        <Group justify="flex-end">{executionId ? <><Button variant="default" onClick={() => setCancelConfirm(true)} disabled={['completed', 'cancelled', 'failed', 'interrupted'].includes(String(execution.data?.status))}>Отменить · RTL</Button><Button variant="default" onClick={onClose}>Закрыть</Button></> : <><Button variant="default" onClick={onClose}>Отмена</Button><Button variant="default" disabled={!droneId || !mapReady || paused} loading={busy} onClick={() => void validate()}>Проверить</Button><Button disabled={!validation || !droneId || !mapReady || paused} loading={busy} onClick={() => setStartConfirm(true)}>Запустить миссию</Button></>}</Group>
      </Stack>
    </Modal>
    <Modal opened={cancelConfirm} onClose={() => setCancelConfirm(false)} title="Отменить миссию?" centered><Stack><Text size="sm">Backend отменит выполнение и инициирует возврат дрона к площадке. Наблюдайте состояние до подтверждённой посадки.</Text><Group justify="flex-end"><Button variant="default" onClick={() => setCancelConfirm(false)}>Остаться</Button><Button color="orange" loading={busy} onClick={() => void cancel()}>Подтвердить возврат</Button></Group></Stack></Modal>
    <Modal opened={startConfirm} onClose={() => setStartConfirm(false)} title="Подтвердить запуск" centered><Stack><Text size="sm">Запустить «{mission?.name}», редакция {mission?.revision}, на {drones.find((drone) => drone.id === droneId)?.name ?? droneId}?</Text><Text size="xs" c="yellow">Дрон должен быть готов к полёту; маршрут не проверен на столкновения.</Text><Group justify="flex-end"><Button variant="default" onClick={() => setStartConfirm(false)}>Отмена</Button><Button loading={busy} onClick={() => void start()}>Подтвердить запуск</Button></Group></Stack></Modal>
  </>
}
