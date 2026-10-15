// @vitest-environment jsdom
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { CameraControls, HINT } from './CameraControls'

it('sends each button its own move, in a labelled group', () => {
  const onAction = vi.fn()
  render(<CameraControls onAction={onAction} showHint={false} />)
  const group = screen.getByRole('group', { name: 'Move the 3D view' })
  const pressed = (['Zoom in', 'Zoom out', 'Rotate left', 'Rotate right', 'Reset view'] as const).map((label) => {
    fireEvent.click(group.querySelector(`[aria-label="${label}"]`)!)
    return onAction.mock.lastCall?.[0]
  })
  expect(pressed).toEqual(['zoom-in', 'zoom-out', 'rotate-left', 'rotate-right', 'reset'])
  expect(screen.getAllByRole('button')).toHaveLength(5)
})

it('shows the how-to line only while asked to', () => {
  const { rerender } = render(<CameraControls onAction={() => {}} showHint />)
  expect(screen.getByText(HINT)).toBeInTheDocument()
  rerender(<CameraControls onAction={() => {}} showHint={false} />)
  expect(screen.queryByText(HINT)).toBeNull()
})

it('offers a Hands mode toggle that says whether it is on', () => {
  const onToggle = vi.fn()
  const { rerender } = render(<CameraControls onAction={() => {}} showHint={false} hands={{ on: false, onToggle }} />)
  const toggle = screen.getByRole('button', { name: 'Hands mode' })
  expect(toggle).toHaveAttribute('aria-pressed', 'false')
  fireEvent.click(toggle)
  expect(onToggle).toHaveBeenCalledTimes(1)
  rerender(<CameraControls onAction={() => {}} showHint={false} hands={{ on: true, onToggle }} />)
  expect(screen.getByRole('button', { name: 'Hands mode' })).toHaveAttribute('aria-pressed', 'true')
})
