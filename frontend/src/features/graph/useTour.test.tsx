// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { TOUR_STOP_MS, useTour } from './useTour'

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

/** The hook wired like the view wires it: selecting a paper sets the focus the hook watches. */
function setup() {
  const selected: string[] = []
  const hook = renderHook(({ focusId }) => useTour((id) => {
    selected.push(id)
    hook.rerender({ focusId: id })
  }, focusId), { initialProps: { focusId: null as string | null } })
  return { hook, selected }
}

it('opens each paper in turn, TOUR_STOP_MS apart, and stops after the last', () => {
  const { hook, selected } = setup()
  act(() => hook.result.current.start(['a', 'b', 'c']))
  expect(selected).toEqual(['a'])
  expect(hook.result.current.running).toBe(true)
  act(() => vi.advanceTimersByTime(TOUR_STOP_MS))
  expect(selected).toEqual(['a', 'b'])
  act(() => vi.advanceTimersByTime(TOUR_STOP_MS))
  expect(selected).toEqual(['a', 'b', 'c'])
  expect(hook.result.current.running).toBe(false)
  act(() => vi.advanceTimersByTime(TOUR_STOP_MS * 3))
  expect(selected).toEqual(['a', 'b', 'c'])
})

it('stops when told to, and when the owner opens a paper the tour did not', () => {
  const { hook, selected } = setup()
  act(() => hook.result.current.start(['a', 'b', 'c']))
  act(() => hook.result.current.stop())
  act(() => vi.advanceTimersByTime(TOUR_STOP_MS * 3))
  expect(selected).toEqual(['a'])
  act(() => hook.result.current.start(['a', 'b', 'c']))
  hook.rerender({ focusId: 'z' }) // the owner picked another paper
  expect(hook.result.current.running).toBe(false)
  act(() => vi.advanceTimersByTime(TOUR_STOP_MS * 3))
  expect(selected).toEqual(['a', 'a'])
})

it('does nothing for an empty tour', () => {
  const { hook, selected } = setup()
  act(() => hook.result.current.start([]))
  expect(hook.result.current.running).toBe(false)
  expect(selected).toEqual([])
})
