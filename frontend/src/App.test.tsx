import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'

afterEach(() => cleanup())

describe('workspace shell', () => {
  it('shows the three control sections and blocks the map without a Blender package', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      const value = path.endsWith('/world/map')
        ? { available: false, reason: 'world_model_missing' }
        : path.endsWith('/system/status') ? { backend: 'ok' }
          : path.endsWith('/world') ? { name: 'empty' }
            : []
      return new Response(JSON.stringify(value), { status: 200, headers: { 'content-type': 'application/json' } })
    }))
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={queryClient}><App /></QueryClientProvider>)

    expect(screen.getByText('SIMDRONE')).toBeInTheDocument()
    expect(screen.getAllByText('Дроны').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Миссии').length).toBeGreaterThan(0)
    expect(screen.getByText('Настройки мира')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Пакет участка не подключён')).toBeInTheDocument())
    expect(screen.getByText('Карта и управление полётом заблокированы')).toBeInTheDocument()
  })
})
