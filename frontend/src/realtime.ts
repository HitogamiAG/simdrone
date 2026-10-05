export type RealtimeEnvelope = {
  type: string
  channel?: string
  drone_id?: string | null
  data?: Record<string, unknown>
  message?: string
  world_generation?: number
  runtime_generation?: number | null
  received_at?: string
}

type Listener = (message: RealtimeEnvelope) => void

/** Owns one public realtime socket and reference-counts channel consumers. */
export class RealtimeClient {
  private socket: WebSocket | null = null
  private readonly listeners = new Map<string, Set<Listener>>()
  private retryTimer: number | undefined
  private stopped = false

  subscribe(channel: string, listener: Listener) {
    const consumers = this.listeners.get(channel) ?? new Set<Listener>()
    const isFirst = consumers.size === 0
    consumers.add(listener)
    this.listeners.set(channel, consumers)
    this.stopped = false
    this.ensureConnected()
    if (isFirst && this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ action: 'subscribe', channels: [channel] }))
    }
    return () => {
      const current = this.listeners.get(channel)
      if (!current) return
      current.delete(listener)
      if (current.size) return
      this.listeners.delete(channel)
      if (this.socket?.readyState === WebSocket.OPEN) {
        this.socket.send(JSON.stringify({ action: 'unsubscribe', channels: [channel] }))
      }
      if (!this.listeners.size) this.disconnect()
    }
  }

  private ensureConnected() {
    if (this.socket || this.stopped || !this.listeners.size) return
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(`${protocol}//${location.host}/api/v1/realtime`)
    this.socket = socket
    socket.onopen = () => {
      if (this.socket !== socket) return
      const channels = [...this.listeners.keys()]
      if (channels.length) socket.send(JSON.stringify({ action: 'subscribe', channels }))
    }
    socket.onmessage = (event) => {
      let message: RealtimeEnvelope
      try { message = JSON.parse(event.data) as RealtimeEnvelope } catch { return }
      if (!message.channel) return
      for (const listener of this.listeners.get(message.channel) ?? []) listener(message)
    }
    socket.onclose = () => {
      if (this.socket !== socket) return
      this.socket = null
      if (!this.stopped && this.listeners.size) this.retryTimer = window.setTimeout(() => this.ensureConnected(), 1500)
    }
    socket.onerror = () => socket.close()
  }

  private disconnect() {
    this.stopped = true
    if (this.retryTimer !== undefined) window.clearTimeout(this.retryTimer)
    this.retryTimer = undefined
    const socket = this.socket
    this.socket = null
    socket?.close()
  }
}

export const realtimeClient = new RealtimeClient()
