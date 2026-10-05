import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MantineProvider } from '@mantine/core'
import ManualPanel from './ManualPanel'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('manual offboard control', () => {
  it('creates a session, authenticates the control socket and requires explicit capture before arm', async () => {
    const socketInstances: Array<{ sent: string[]; onopen?: () => void }> = []
    class MockSocket {
      static OPEN = 1
      readyState = 1
      sent: string[] = []
      onopen?: () => void
      onmessage?: (event: { data: string }) => void
      onclose?: () => void
      constructor(_url: string) { socketInstances.push(this); queueMicrotask(() => this.onopen?.()) }
      send(value: string) { this.sent.push(value) }
      close() { this.readyState = 3; this.onclose?.() }
    }
    vi.stubGlobal('WebSocket', MockSocket)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.endsWith('/sessions') && init?.method === 'POST') return new Response(JSON.stringify({ session_id: 'session-1', token: 'secret', status: 'ready', armed: false, limits: { horizontal_m_s: 3, up_m_s: 2, down_m_s: 1 } }), { status: 201 })
      if (path.endsWith('/flight') && !path.includes('/sessions/')) return new Response(JSON.stringify({ armed: false, landed_state: { landed_state: 'ON_GROUND' } }))
      return new Response(JSON.stringify({ status: 'ready', armed: false }), { status: 202 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<MantineProvider><QueryClientProvider client={queryClient}><ManualPanel droneId="drone-1" /></QueryClientProvider></MantineProvider>)

    fireEvent.click(screen.getByRole('button', { name: 'Открыть сессию' }))
    await screen.findByRole('button', { name: 'Захватить управление' })
    await waitFor(() => expect(socketInstances[0]?.sent).toEqual([JSON.stringify({ token: 'secret' })]))
    const arm = screen.getByRole('button', { name: 'Arm' })
    expect(arm).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Захватить управление' }))
    expect(arm).toBeEnabled()
    fireEvent.click(arm)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/drones/drone-1/flight/offboard/sessions/session-1/arm', expect.objectContaining({ method: 'POST' })))
    await screen.findByText(/Команда arm принята/)
    const body = JSON.parse(String(fetchMock.mock.calls.at(-1)?.[1]?.body)) as { request_id: string }
    expect(body.request_id).toMatch(/^[0-9a-f-]{36}$/)
    fireEvent.keyDown(document.body, { code: 'Space', key: ' ' })
    fireEvent.keyDown(document.body, { code: 'KeyR', key: 'r' })
    await waitFor(() => expect(socketInstances[0]?.sent.some((frame) => JSON.parse(frame).up === 1)).toBe(true))
    fireEvent.keyUp(document.body, { code: 'KeyR', key: 'r' })
    fireEvent.keyUp(document.body, { code: 'Space', key: ' ' })
    await waitFor(() => expect(JSON.parse(socketInstances[0]?.sent.at(-1) ?? '{}').up).toBe(0))
  })
})
