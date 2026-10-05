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
  private readonly retryTimers = new Map<string, number>()
  private readonly retryCounts = new Map<string, number>()
  private readonly subscribed = new Set<string>()
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
      const timer = this.retryTimers.get(channel)
      if (timer !== undefined) window.clearTimeout(timer)
      this.retryTimers.delete(channel)
      this.retryCounts.delete(channel)
      this.subscribed.delete(channel)
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
      this.subscribed.clear()
      const channels = [...this.listeners.keys()]
      if (channels.length) socket.send(JSON.stringify({ action: 'subscribe', channels }))
    }
    socket.onmessage = (event) => {
      let message: RealtimeEnvelope
      try { message = JSON.parse(event.data) as RealtimeEnvelope } catch { return }
      if (!message.channel) return
      if (message.type === 'subscribed') {
        this.subscribed.add(message.channel)
        this.retryCounts.delete(message.channel)
        const timer = this.retryTimers.get(message.channel)
        if (timer !== undefined) window.clearTimeout(timer)
        this.retryTimers.delete(message.channel)
        return
      }
      for (const listener of this.listeners.get(message.channel) ?? []) listener(message)
      if (message.type === 'invalidated' || message.type === 'error') this.retryChannel(message.channel)
    }
    socket.onclose = () => {
      if (this.socket !== socket) return
      this.socket = null
      this.subscribed.clear()
      if (!this.stopped && this.listeners.size) this.retryTimer = window.setTimeout(() => this.ensureConnected(), 1500)
    }
    socket.onerror = () => socket.close()
  }

  private retryChannel(channel: string) {
    if (!this.listeners.has(channel) || this.retryTimers.has(channel)) return
    this.subscribed.delete(channel)
    const attempt = (this.retryCounts.get(channel) ?? 0) + 1
    this.retryCounts.set(channel, attempt)
    const delay = Math.min(750 * 2 ** Math.min(attempt - 1, 4), 12_000)
    const timer = window.setTimeout(() => {
      this.retryTimers.delete(channel)
      if (!this.listeners.has(channel)) return
      if (this.socket?.readyState === WebSocket.OPEN) {
        this.socket.send(JSON.stringify({ action: 'subscribe', channels: [channel] }))
      } else {
        this.ensureConnected()
      }
    }, delay)
    this.retryTimers.set(channel, timer)
  }

  private disconnect() {
    this.stopped = true
    if (this.retryTimer !== undefined) window.clearTimeout(this.retryTimer)
    this.retryTimer = undefined
    for (const timer of this.retryTimers.values()) window.clearTimeout(timer)
    this.retryTimers.clear()
    this.retryCounts.clear()
    this.subscribed.clear()
    const socket = this.socket
    this.socket = null
    socket?.close()
  }
}

export const realtimeClient = new RealtimeClient()
