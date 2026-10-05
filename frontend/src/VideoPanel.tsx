import { useEffect, useRef, useState } from 'react'
import { Badge, Button, Group, Select, Stack, Text } from '@mantine/core'
import { useQuery } from '@tanstack/react-query'

type Camera = { id: string; name?: string; type: string }
type SensorCatalog = Camera[]
type VideoDetails = { available: boolean; active: boolean | null; url: string; protocol: string; on_demand: boolean }
type VideoState = 'loading' | 'connecting' | 'waiting-frame' | 'playing' | 'error'

function waitForIceGathering(peer: RTCPeerConnection, signal: AbortSignal) {
  if (peer.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise<void>((resolve, reject) => {
    const finish = (error?: Error) => {
      window.clearTimeout(timeout)
      peer.removeEventListener('icegatheringstatechange', check)
      signal.removeEventListener('abort', cancel)
      if (error) reject(error)
      else resolve()
    }
    const check = () => { if (peer.iceGatheringState === 'complete') finish() }
    const cancel = () => finish(new DOMException('WHEP negotiation was cancelled.', 'AbortError'))
    const timeout = window.setTimeout(() => finish(new Error('Истекло время сбора ICE-кандидатов.')), 10_000)
    peer.addEventListener('icegatheringstatechange', check)
    signal.addEventListener('abort', cancel, { once: true })
    if (signal.aborted) cancel()
  })
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url)
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`)
  return response.json() as Promise<T>
}

export function WHEPVideo({ url }: { url: string }) {
  const video = useRef<HTMLVideoElement>(null)
  const [state, setState] = useState<VideoState>('connecting')
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let closed = false
    let peer: RTCPeerConnection | undefined
    let sessionUrl: string | null = null
    let sessionDeleted = false
    const abort = new AbortController()
    const target = video.current
    const deleteSession = () => {
      if (!sessionUrl || sessionDeleted) return
      sessionDeleted = true
      void fetch(sessionUrl, { method: 'DELETE', keepalive: true }).catch(() => undefined)
    }

    const connect = async () => {
      try {
        peer = new RTCPeerConnection({ iceServers: [] })
        peer.addTransceiver('video', { direction: 'recvonly' })
        peer.ontrack = (event) => {
          if (!target || closed) return
          target.srcObject = event.streams[0] ?? new MediaStream([event.track])
          void target.play().catch(() => undefined)
          setState('waiting-frame')
        }
        peer.onconnectionstatechange = () => {
          if (!closed && peer?.connectionState === 'failed') {
            setError('WebRTC соединение не установлено. Проверьте прямую UDP-доступность MediaMTX.')
            setState('error')
          }
        }
        const offer = await peer.createOffer()
        await peer.setLocalDescription(offer)
        await waitForIceGathering(peer, abort.signal)
        if (closed || !peer.localDescription?.sdp) return
        const response = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/sdp', accept: 'application/sdp' }, body: peer.localDescription.sdp, signal: abort.signal })
        if (!response.ok) throw new Error(`WHEP ответил ${response.status}: ${(await response.text()).slice(0, 300)}`)
        const locationHeader = response.headers.get('Location')
        if (locationHeader) sessionUrl = new URL(locationHeader, url).href
        const sdp = await response.text()
        if (closed) {
          deleteSession()
          return
        }
        await peer.setRemoteDescription({ type: 'answer', sdp })
        setState('waiting-frame')
      } catch (cause) {
        if (closed || (cause instanceof DOMException && cause.name === 'AbortError')) return
        setError(cause instanceof Error ? cause.message : 'Не удалось подключить видео.')
        setState('error')
      }
    }

    void connect()
    return () => {
      closed = true
      abort.abort()
      peer?.close()
      if (target) target.srcObject = null
      deleteSession()
    }
  }, [url, attempt])

  const onFrame = () => setState('playing')
  return <Stack gap="xs" className="video-panel">
    <video ref={video} autoPlay muted playsInline onLoadedData={onFrame} onPlaying={onFrame} />
    <Group justify="space-between"><Text size="xs" c="dimmed">{state === 'connecting' || state === 'loading' ? 'Подключение…' : state === 'waiting-frame' ? 'Ожидание кадра…' : state === 'playing' ? 'Видео воспроизводится' : 'Ошибка видео'}</Text><Badge size="xs" variant="outline" color={state === 'playing' ? 'green' : state === 'error' ? 'red' : 'yellow'}>WHEP</Badge></Group>
    {error && <Text size="xs" c="red">{error}</Text>}
    {state === 'error' && <Button size="compact-xs" variant="default" onClick={() => { setError(null); setState('connecting'); setAttempt((value) => value + 1) }}>Подключить снова</Button>}
  </Stack>
}

export default function VideoPanel({ droneId }: { droneId: string }) {
  const sensors = useQuery({ queryKey: ['drone-sensors', droneId], queryFn: () => getJson<SensorCatalog>(`/api/v1/drones/${encodeURIComponent(droneId)}/sensors/`), retry: false })
  const cameras = (sensors.data ?? []).filter((sensor) => sensor.type === 'camera')
  const [cameraId, setCameraId] = useState<string | null>(null)
  const selectedCamera = cameras.find((camera) => camera.id === cameraId) ?? cameras[0]
  const video = useQuery({ queryKey: ['drone-video', droneId, selectedCamera?.id], enabled: Boolean(selectedCamera), queryFn: () => getJson<VideoDetails>(`/api/v1/drones/${encodeURIComponent(droneId)}/sensors/${encodeURIComponent(selectedCamera!.id)}/video`), retry: false })

  return <Stack gap="sm" className="video-panel-shell">
    {sensors.isLoading && <Text size="xs" c="dimmed">Загрузка камер…</Text>}
    {sensors.isError && <Group justify="space-between"><Text size="xs" c="red">Не удалось получить список камер.</Text><Button size="compact-xs" variant="subtle" onClick={() => void sensors.refetch()}>Повторить</Button></Group>}
    {sensors.isSuccess && cameras.length === 0 && <Text size="xs" c="dimmed">У этого дрона нет доступных камер.</Text>}
    {cameras.length > 0 && <Select label="Камера" size="xs" data={cameras.map((camera) => ({ value: camera.id, label: camera.name ?? camera.id }))} value={selectedCamera?.id ?? null} onChange={setCameraId} />}
    {video.isFetching && <Text size="xs" c="dimmed">Получение адреса видеопотока…</Text>}
    {video.isError && <Group justify="space-between"><Text size="xs" c="red">Не удалось получить WHEP URL.</Text><Button size="compact-xs" variant="subtle" onClick={() => void video.refetch()}>Повторить</Button></Group>}
    {video.data?.available && selectedCamera && <WHEPVideo key={`${droneId}:${selectedCamera.id}`} url={video.data.url} />}
  </Stack>
}
