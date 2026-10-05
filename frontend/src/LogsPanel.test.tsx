import { MantineProvider } from '@mantine/core'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import LogsPanel from './LogsPanel'

describe('LogsPanel', () => {
  it('searches, pauses display, resumes and clears the bounded log view', () => {
    const onClear = vi.fn()
    const first = { type: 'log', data: { source: 'px4', message: 'arming accepted', received_at: '2026-10-05T12:00:00Z' } }
    const { rerender } = render(<MantineProvider><LogsPanel entries={[first]} available onClear={onClear} /></MantineProvider>)
    expect(screen.getByText('arming accepted')).toBeInTheDocument()

    fireEvent.change(screen.getByRole('textbox', { name: 'Поиск по логам' }), { target: { value: 'accepted' } })
    expect(screen.getByText('arming accepted')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Пауза' }))
    rerender(<MantineProvider><LogsPanel entries={[first, { type: 'log', data: { source: 'px4', message: 'new line' } }]} available onClear={onClear} /></MantineProvider>)
    expect(screen.queryByText('new line')).not.toBeInTheDocument()

    fireEvent.change(screen.getByRole('textbox', { name: 'Поиск по логам' }), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Продолжить' }))
    expect(screen.getByText('new line')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Очистить' }))
    expect(onClear).toHaveBeenCalledOnce()
  })
})
