import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MantineProvider } from '@mantine/core'
import WorldSettingsModal from './WorldSettingsModal'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('world settings', () => {
  it('shows consequences before applying the supported patch and posts vector gravity as XYZ', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method === 'PATCH'
      ? new Response(JSON.stringify({ applied: ['physics', 'gravity', 'spherical_coordinates'] }))
      : new Response(JSON.stringify({ physics: { max_step_size: 0.004, real_time_factor: 1 }, gravity: [0, 0, -9.80665], spherical_coordinates: { latitude_deg: 43.2, longitude_deg: 76.8, elevation: 800, heading_deg: 0, surface_model: 'EARTH_WGS84' } })))
    vi.stubGlobal('fetch', fetchMock)
    const onChanged = vi.fn()
    render(<MantineProvider><WorldSettingsModal opened droneCount={2} onClose={() => {}} onChanged={onChanged} /></MantineProvider>)

    fireEvent.click(await screen.findByRole('button', { name: 'Применить настройки' }))
    expect(await screen.findByText(/удалит 2 дрон/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/world', expect.objectContaining({ method: 'PATCH' })))
    const init = fetchMock.mock.calls.find(([path, options]) => String(path) === '/api/v1/world' && options?.method === 'PATCH')?.[1]
    const body = JSON.parse(String(init?.body))
    expect(body.gravity).toEqual({ x: 0, y: 0, z: -9.80665 })
    expect(onChanged).toHaveBeenCalledOnce()
  })
})
