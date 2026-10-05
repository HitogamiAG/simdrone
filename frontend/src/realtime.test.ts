import { afterEach, describe, expect, it, vi } from 'vitest'
import { RealtimeClient } from './realtime'

class MockSocket {
  static OPEN = 1
  static instances: MockSocket[] = []
  readonly url: string
  readyState = 0
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(url: string) { this.url = url; MockSocket.instances.push(this) }
  send(message: string) { this.sent.push(message) }
  close() { this.readyState = 3; this.onclose?.() }
  open() { this.readyState = MockSocket.OPEN; this.onopen?.() }
  emit(message: unknown) { this.onmessage?.({ data: JSON.stringify(message) }) }
}

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); MockSocket.instances = [] })

describe('RealtimeClient', () => {
  it('shares one socket, reference-counts a channel and routes envelopes by channel', () => {
    vi.stubGlobal('WebSocket', MockSocket)
    const client = new RealtimeClient()
    const first = vi.fn()
    const second = vi.fn()
    const other = vi.fn()
    vi.useFakeTimers()
    const removeFirst = client.subscribe('drone.a.pose', first)
    const socket = MockSocket.instances[0]
    socket.open()
    expect(JSON.parse(socket.sent[0])).toEqual({ action: 'subscribe', channels: ['drone.a.pose'] })

    const removeSecond = client.subscribe('drone.a.pose', second)
    const removeOther = client.subscribe('drone.b.pose', other)
    expect(MockSocket.instances).toHaveLength(1)
    expect(JSON.parse(socket.sent[1])).toEqual({ action: 'subscribe', channels: ['drone.b.pose'] })
    socket.emit({ type: 'subscribed', channel: 'drone.a.pose' })
    expect(first).not.toHaveBeenCalled()
    socket.emit({ type: 'pose', channel: 'drone.a.pose', data: { pose: {} } })
    expect(first).toHaveBeenCalledTimes(1)
    expect(second).toHaveBeenCalledTimes(1)
    expect(other).not.toHaveBeenCalled()
    socket.emit({ type: 'invalidated', channel: 'drone.a.pose' })
    expect(first.mock.lastCall?.[0]).toMatchObject({ type: 'invalidated' })
    vi.advanceTimersByTime(750)
    expect(JSON.parse(socket.sent[2])).toEqual({ action: 'subscribe', channels: ['drone.a.pose'] })

    removeFirst()
    removeSecond()
    expect(JSON.parse(socket.sent[3])).toEqual({ action: 'unsubscribe', channels: ['drone.a.pose'] })
    removeOther()
    expect(socket.readyState).toBe(3)
  })
})
