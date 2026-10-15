// @vitest-environment jsdom
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { TourControls } from './TourControls'

it('starts and pauses the tour, and picks its order only while it is stopped', () => {
  const onToggle = vi.fn()
  const onOrder = vi.fn()
  const { rerender } = render(<TourControls running={false} order="links" onOrder={onOrder} onToggle={onToggle} />)
  const group = screen.getByRole('group', { name: 'Guided tour' })
  expect(group).toBeInTheDocument()
  fireEvent.change(screen.getByRole('combobox', { name: 'Tour order' }), { target: { value: 'years' } })
  expect(onOrder).toHaveBeenCalledWith('years')
  const toggle = screen.getByRole('button', { name: 'Tour' })
  expect(toggle).toHaveAttribute('aria-pressed', 'false')
  fireEvent.click(toggle)
  expect(onToggle).toHaveBeenCalledTimes(1)
  rerender(<TourControls running order="links" onOrder={onOrder} onToggle={onToggle} />)
  expect(screen.getByRole('button', { name: 'Tour' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByRole('combobox', { name: 'Tour order' })).toBeDisabled()
  expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['Follow links', 'Oldest first'])
})
