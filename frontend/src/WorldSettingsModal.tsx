import { useEffect, useState } from 'react'
import { Button, Group, Modal, NumberInput, Stack, Text } from '@mantine/core'

type WorldData = { physics?: { max_step_size?: number; real_time_factor?: number }; gravity?: { x: number; y: number; z: number }; spherical_coordinates?: { latitude_deg?: number; longitude_deg?: number; elevation?: number; heading_deg?: number; surface_model?: string } }
type WorldResponse = Omit<WorldData, 'gravity'> & { gravity?: WorldData['gravity'] | number[] }
type Intent = { title: string; path: string; method: string; body?: unknown; consequence: string } | null

export default function WorldSettingsModal({ opened, droneCount, onClose, onChanged }: { opened: boolean; droneCount: number; onClose: () => void; onChanged: () => void }) {
  const [values, setValues] = useState<WorldData>({})
  const [intent, setIntent] = useState<Intent>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    if (!opened) return
    let live = true
    fetch('/api/v1/world').then(async (response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json() as Promise<WorldResponse> }).then((data) => { if (live) { const gravity = Array.isArray(data.gravity) ? { x: data.gravity[0] ?? 0, y: data.gravity[1] ?? 0, z: data.gravity[2] ?? 0 } : data.gravity; setValues({ ...data, gravity }); setLoaded(true) } }).catch((reason) => { if (live) setError(reason instanceof Error ? reason.message : 'Не удалось загрузить мир') })
    return () => { live = false }
  }, [opened])
  const set = (path: 'physics.max_step_size' | 'physics.real_time_factor' | 'gravity.0' | 'gravity.1' | 'gravity.2' | 'spherical_coordinates.latitude_deg' | 'spherical_coordinates.longitude_deg' | 'spherical_coordinates.elevation' | 'spherical_coordinates.heading_deg', value: number) => {
    const [section, field] = path.split('.')
    setValues((current) => section === 'gravity' ? { ...current, gravity: { ...(current.gravity ?? { x: 0, y: 0, z: 0 }), [(['x', 'y', 'z'][Number(field)])]: value } } : { ...current, [section]: { ...current[section as 'physics' | 'spherical_coordinates'], [field]: value } })
  }
  const perform = async () => {
    if (!intent || busy) return
    setBusy(true); setError(null)
    try {
      const response = await fetch(intent.path, { method: intent.method, headers: intent.body === undefined ? undefined : { 'content-type': 'application/json' }, body: intent.body === undefined ? undefined : JSON.stringify(intent.body) })
      const result = await response.json().catch(() => null) as { error?: { message?: string } } | null
      if (!response.ok) throw new Error(result?.error?.message ?? `HTTP ${response.status}`)
      setIntent(null); setLoaded(false); onChanged()
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Операция завершилась ошибкой') }
    finally { setBusy(false) }
  }
  const patchIntent: Intent = { title: 'Применить настройки мира', path: '/api/v1/world', method: 'PATCH', body: { physics: values.physics, gravity: values.gravity, spherical_coordinates: values.spherical_coordinates }, consequence: `Backend сначала сбросит мир, удалит ${droneCount} дрон(ов) и затем применит объединённые настройки. Площадки освободятся; продолжить полёты после этого нельзя без нового создания дронов.` }
  return <>
    <Modal opened={opened} onClose={() => { setLoaded(false); setError(null); onClose() }} title="Настройки мира" size="lg" centered>
      <Stack>
        <Text size="xs" c="dimmed">Изменяются только поля, поддерживаемые Backend: physics step size, real-time factor, gravity и геопривязка WGS84.</Text>
        <Group grow><NumberInput label="Шаг physics, с" min={0.0001} step={0.001} decimalScale={5} value={values.physics?.max_step_size ?? 0} onChange={(v) => set('physics.max_step_size', Number(v))} /><NumberInput label="Real-time factor" min={0.01} step={0.1} value={values.physics?.real_time_factor ?? 0} onChange={(v) => set('physics.real_time_factor', Number(v))} /></Group>
        <Text size="sm" fw={600}>Гравитация · м/с²</Text><Group grow>{(['x', 'y', 'z'] as const).map((axis, index) => <NumberInput key={axis} label={axis.toUpperCase()} value={values.gravity?.[axis] ?? 0} onChange={(v) => set((['gravity.0', 'gravity.1', 'gravity.2'] as const)[index], Number(v))} />)}</Group>
        <Text size="sm" fw={600}>Геопривязка</Text><Group grow>{(['latitude_deg', 'longitude_deg', 'elevation', 'heading_deg'] as const).map((key) => <NumberInput key={key} label={key} value={values.spherical_coordinates?.[key] ?? 0} onChange={(v) => set(`spherical_coordinates.${key}`, Number(v))} />)}</Group>
        {error && <Text size="sm" c="red">{error}</Text>}
        <Group justify="space-between"><Button color="red" variant="default" onClick={() => setIntent({ title: 'Сбросить мир', path: '/api/v1/world/reset', method: 'POST', consequence: `Будут остановлены PX4, удалены ${droneCount} дрон(ов) и восстановлен исходный SDF мира.` })}>Сбросить мир</Button><Button color="red" variant="default" onClick={() => setIntent({ title: 'Перезапустить Gazebo', path: '/api/v1/system/gazebo/reboot', method: 'POST', consequence: `Gazebo будет перезапущен, ${droneCount} дрон(ов) и их PX4 будут удалены.` })}>Reboot Gazebo</Button><Button disabled={!loaded} onClick={() => setIntent(patchIntent)}>Применить настройки</Button></Group>
      </Stack>
    </Modal>
    <Modal opened={Boolean(intent)} onClose={() => setIntent(null)} title={intent?.title} centered>
      <Stack><Text size="sm">{intent?.consequence}</Text>{error && <Text size="sm" c="red">{error}</Text>}<Group justify="flex-end"><Button variant="default" onClick={() => setIntent(null)}>Отмена</Button><Button color="orange" loading={busy} onClick={() => void perform()}>Подтвердить</Button></Group></Stack>
    </Modal>
  </>
}
