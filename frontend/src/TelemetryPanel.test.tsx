import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MantineProvider } from '@mantine/core'
import TelemetryPanel from './TelemetryPanel'

describe('TelemetryPanel', () => {
  it('renders this drone pose and available PX4 samples as readable fields', () => {
    render(<MantineProvider><TelemetryPanel
      pose={{ type: 'pose', data: { age_s: 0.04, pose: { position: { x: 1, y: 2, z: 3 } } } }}
      samples={{
        armed: { type: 'armed', data: { type: 'armed', data: true } },
        battery: { type: 'battery', data: { type: 'battery', data: { remaining_percent: 0.8 } } },
      }}
    /></MantineProvider>)

    expect(screen.getByText('1, 2, 3')).toBeInTheDocument()
    expect(screen.getByText('Armed')).toBeInTheDocument()
    expect(screen.getByText('true')).toBeInTheDocument()
    expect(screen.getByText('Батарея')).toBeInTheDocument()
    expect(screen.getByText('remaining_percent: 0.8')).toBeInTheDocument()
  })
})
