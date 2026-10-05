import { useEffect, useRef, useState } from 'react'
import { Badge, Button, Group, Text, TextInput } from '@mantine/core'
import type { RealtimeEnvelope } from './realtime'

type LogEntry = RealtimeEnvelope & { data?: Record<string, unknown> }

export default function LogsPanel({ entries, available, onClear, scope = 'PX4 logs' }: { entries: LogEntry[]; available: boolean; onClear: () => void; scope?: string }) {
  const [paused, setPaused] = useState(false)
  const [frozen, setFrozen] = useState<LogEntry[] | null>(null)
  const [search, setSearch] = useState('')
  const bottom = useRef<HTMLDivElement>(null)
  const visible = paused ? frozen ?? entries : entries
  const matches = visible.filter((entry) => String(entry.data?.message ?? entry.message ?? '').toLowerCase().includes(search.toLowerCase()))
  const filtered = search ? matches : matches.slice(-250)

  useEffect(() => {
    if (!paused) bottom.current?.scrollIntoView?.({ block: 'end' })
  }, [entries, paused, search])

  return <div className="logs-panel">
    <Group gap="xs" wrap="nowrap">
      <TextInput aria-label="Поиск по логам" placeholder="Поиск" size="xs" value={search} onChange={(event) => setSearch(event.currentTarget.value)} style={{ flex: 1 }} />
      <Button size="compact-xs" variant={paused ? 'light' : 'default'} onClick={() => { if (paused) { setPaused(false); setFrozen(null) } else { setFrozen(entries); setPaused(true) } }}>{paused ? 'Продолжить' : 'Пауза'}</Button>
      <Button size="compact-xs" variant="default" onClick={() => { onClear(); if (paused) setFrozen([]) }}>Очистить</Button>
    </Group>
    <div className="logs-status"><Badge size="xs" variant="outline" color={available ? 'green' : 'yellow'}>{available ? scope : 'Сервис недоступен'}</Badge><Text size="xs" c="dimmed">{entries.length}/2000 строк{paused ? ' · отображение приостановлено' : ''}</Text></div>
    {!available && <Text size="xs" c="dimmed">Поток логов сейчас недоступен.</Text>}
    <div className="logs-list" role="log" aria-label={scope === 'PX4 logs' ? 'Логи PX4' : 'Логи Gazebo'}>
      {filtered.map((entry, index) => {
        const message = String(entry.data?.message ?? entry.message ?? (entry.type === 'gap' ? `Пропущено сообщений: ${String(entry.data?.dropped ?? '—')}` : entry.type))
        const source = String(entry.data?.source ?? 'Backend')
        const time = String(entry.data?.received_at ?? entry.received_at ?? '')
        return <div className={`log-entry ${entry.type === 'gap' ? 'log-gap' : ''}`} key={`${time}-${index}`}><time>{time ? new Date(time).toLocaleTimeString() : '—'}</time><span>{source}</span><pre>{message}</pre></div>
      })}
      {filtered.length === 0 && <Text size="xs" c="dimmed">{entries.length ? 'Совпадений нет' : 'Ожидание новых строк…'}</Text>}
      <div ref={bottom} />
    </div>
  </div>
}
