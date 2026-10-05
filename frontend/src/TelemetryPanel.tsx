import { Badge, Stack, Text } from '@mantine/core'
import type { RealtimeEnvelope } from './realtime'

type SampleMap = Record<string, RealtimeEnvelope>

const labels: Record<string, string> = {
  position: 'Позиция PX4', velocity: 'Скорость PX4', attitude: 'Ориентация',
  flight_mode: 'Режим полёта', armed: 'Armed', landed_state: 'Состояние посадки',
  gps: 'GPS', health: 'Health', battery: 'Батарея', status_text: 'Сообщение PX4', home: 'Домашняя позиция',
}

function compact(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.map(compact).join(', ')
  if (typeof value === 'object') return Object.entries(value).map(([key, item]) => `${key}: ${compact(item)}`).join(' · ')
  return String(value)
}

export default function TelemetryPanel({ pose, samples }: { pose?: RealtimeEnvelope; samples?: SampleMap }) {
  const poseData = pose?.data?.pose as { position?: { x?: number; y?: number; z?: number } } | undefined
  const position = poseData?.position
  const rows = Object.entries(samples ?? {}).map(([type, envelope]) => {
    const payload = envelope.data?.data ?? envelope.data
    return [labels[type] ?? type, compact(payload)] as const
  })
  const poseReceived = pose?.type === 'pose'

  return <Stack gap="xs" className="telemetry-panel">
    <div className="telemetry-entry">
      <div className="telemetry-entry-heading"><Text size="xs" c="dimmed">Позиция Gazebo · XYZ, м</Text><Badge size="xs" variant="outline" color={poseReceived ? 'green' : 'gray'}>{poseReceived ? `свежесть ${compact(pose.data?.age_s)} с` : 'нет свежих данных'}</Badge></div>
      <Text size="sm">{position ? `${compact(position.x)}, ${compact(position.y)}, ${compact(position.z)}` : 'Ожидание позы'}</Text>
    </div>
    {rows.map(([label, value]) => <div className="telemetry-entry" key={label}><Text size="xs" c="dimmed">{label}</Text><Text size="sm" className="telemetry-value">{value}</Text></div>)}
    {!rows.length && <Text size="xs" c="dimmed">Ожидание телеметрии PX4. Поток открывается для этого дрона.</Text>}
  </Stack>
}
