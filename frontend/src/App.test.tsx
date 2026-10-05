import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

  it('creates a drone through a selected spawn pad and refreshes the catalog', async () => {
    const created = { id: 'drone-public-1', name: 'Drone-01', model: 'x500_gimbal', spawn_pad_id: 'landing_pad_01', status: 'ready' }
    const pads = {
      world: 'empty', world_generation: 1, coordinate_system: 'gazebo_xyz_z_up', units: 'meters',
      pads: [
        { id: 'landing_pad_01', name: 'landing_pad_01', model: 'drone_pad', availability: 'available', assigned_drone_id: null, unavailable_reason: null,
          surface_pose: { position: { x: -3, y: 0, z: 0.3 } }, spawn_pose: { position: { x: -3, y: 0, z: 0.292 }, orientation: { x: 0, y: 0, z: 0, w: 1 } }, supported_models: ['x500_gimbal'] },
        { id: 'landing_pad_02', name: 'landing_pad_02', model: 'drone_pad', availability: 'occupied', assigned_drone_id: 'drone-other', unavailable_reason: null,
          surface_pose: { position: { x: 3, y: 0, z: 0.3 } }, spawn_pose: { position: { x: 3, y: 0, z: 0.292 }, orientation: { x: 0, y: 0, z: 0, w: 1 } }, supported_models: ['x500_gimbal'] },
      ],
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.endsWith('/world/spawn-pads')) return new Response(JSON.stringify(pads), { status: 200 })
      if (path.endsWith('/drones/') && init?.method === 'POST') return new Response(JSON.stringify(created), { status: 201 })
      const value = path.endsWith('/world/map') ? { available: false, reason: 'world_model_missing' }
        : path.endsWith('/system/status') ? { backend: { ready: true } }
          : path.endsWith('/world') ? { name: 'empty' }
            : path.endsWith('/drones/') ? [created]
              : []
      return new Response(JSON.stringify(value), { status: 200 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={queryClient}><App /></QueryClientProvider>)

    fireEvent.click(await screen.findByRole('button', { name: 'Создать дрон' }))
    expect(await screen.findByText('Поверхность XYZ · м: -3.00, 0.00, 0.30')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /landing_pad_02/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /landing_pad_01/ }))
    const submit = screen.getByRole('button', { name: 'Создать' })
    await waitFor(() => expect(submit).toBeEnabled())
    fireEvent.click(submit)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/drones/', expect.objectContaining({ method: 'POST' })))
    const request = fetchMock.mock.calls.find(([path, init]) => String(path).endsWith('/drones/') && init?.method === 'POST')?.[1]
    expect(JSON.parse(String(request?.body))).toEqual({ name: 'Drone-01', model: 'x500_gimbal', spawn_pad_id: 'landing_pad_01' })
    expect(await screen.findByText(/Площадка landing_pad_01 закреплена за дроном/)).toBeInTheDocument()
    await waitFor(() => expect(fetchMock.mock.calls.filter(([path]) => String(path).endsWith('/world/spawn-pads')).length).toBeGreaterThan(1))
  })

})
