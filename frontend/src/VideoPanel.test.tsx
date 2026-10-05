import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MantineProvider } from '@mantine/core'
import { WHEPVideo } from './VideoPanel'

class MockPeerConnection extends EventTarget {
  static instances: MockPeerConnection[] = []
  iceGatheringState = 'complete'
  connectionState = 'new'
  localDescription: RTCSessionDescriptionInit | null = null
  addTransceiver = vi.fn()
  setRemoteDescription = vi.fn(async () => undefined)
  close = vi.fn()
  constructor() { super(); MockPeerConnection.instances.push(this) }
  async createOffer() { return { type: 'offer' as const, sdp: 'offer-sdp' } }
  async setLocalDescription(description: RTCSessionDescriptionInit) { this.localDescription = description }
}

afterEach(() => { vi.unstubAllGlobals(); MockPeerConnection.instances = [] })

describe('WHEPVideo', () => {
  it('posts an SDP offer, waits for a decoded frame and closes the WHEP session', async () => {
    vi.stubGlobal('RTCPeerConnection', MockPeerConnection)
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method === 'DELETE'
      ? new Response(null, { status: 200 })
      : new Response('answer-sdp', { status: 201, headers: { location: '/sessions/one', 'content-type': 'application/sdp' } }))
    vi.stubGlobal('fetch', fetchMock)
    const { unmount, container } = render(<MantineProvider><WHEPVideo url="https://media.test/camera/whep" /></MantineProvider>)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('https://media.test/camera/whep', expect.objectContaining({ method: 'POST', body: 'offer-sdp' })))
    await waitFor(() => expect(MockPeerConnection.instances[0].setRemoteDescription).toHaveBeenCalledWith({ type: 'answer', sdp: 'answer-sdp' }))
    fireEvent.loadedData(container.querySelector('video')!)
    expect(screen.getByText('Видео воспроизводится')).toBeInTheDocument()

    unmount()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('https://media.test/sessions/one', expect.objectContaining({ method: 'DELETE' })))
    expect(MockPeerConnection.instances[0].close).toHaveBeenCalledOnce()
  })
})
