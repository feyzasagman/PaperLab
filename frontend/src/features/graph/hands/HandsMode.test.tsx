// @vitest-environment jsdom
import { act, render, screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { Hand } from './gestures'
import fixtures from './handFixtures.json'

const tracker = vi.hoisted(() => ({
  onFrame: null as null | ((hands: Hand[], now: number) => void),
  stop: vi.fn(),
  start: null as null | (() => Promise<() => void>),
}))
vi.mock('./handTracker', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./handTracker')>()
  return {
    ...actual,
    startTracker: vi.fn((_video: HTMLVideoElement, onFrame: (hands: Hand[], now: number) => void) => {
      tracker.onFrame = onFrame
      return tracker.start ? tracker.start() : Promise.resolve(tracker.stop)
    }),
  }
})

import { TrackerError } from './handTracker'
import HandsMode, { MODE_TEXT, PROBLEM_TEXT } from './HandsMode'

const f = fixtures as unknown as Record<'point' | 'palm', Hand> & { twoPalms: Hand[] }
const props = () => ({ onTurn: vi.fn(), onZoom: vi.fn(), onSelect: vi.fn(), pickAt: vi.fn(() => ({ id: 'p1', title: 'Alpha' })) })
const feed = (frames: Hand[][], from = 0) =>
  act(() => frames.forEach((hands, i) => tracker.onFrame!(hands, from + i * 100)))
const repeat = (hands: Hand[], n: number) => Array.from({ length: n }, () => hands)

beforeEach(() => {
  vi.clearAllMocks()
  tracker.onFrame = null
  tracker.start = null
})

it('opens the paper the pointer rests on, once, names it, and turns the camera off when it goes', async () => {
  const p = props()
  const { unmount } = render(<HandsMode {...p} />)
  expect(screen.getByRole('status')).toHaveTextContent(MODE_TEXT.starting)
  await vi.waitFor(() => expect(tracker.onFrame).not.toBeNull())
  feed(repeat([f.point], 16)) // 1.5 s of pointing at one paper
  expect(screen.getByRole('status')).toHaveTextContent(MODE_TEXT.point)
  expect(screen.getByText('Alpha')).toBeInTheDocument()
  expect(p.onSelect).toHaveBeenCalledTimes(1)
  expect(p.onSelect).toHaveBeenCalledWith('p1')
  unmount()
  expect(tracker.stop).toHaveBeenCalled()
})

it('turns the view with an open hand and zooms with two', async () => {
  const p = props()
  render(<HandsMode {...p} />)
  await vi.waitFor(() => expect(tracker.onFrame).not.toBeNull())
  const moved = (hand: Hand, dx: number): Hand => hand.map((q) => ({ x: q.x + dx, y: q.y }))
  feed([0, 0, 0, -0.02].map((dx) => [moved(f.palm, dx)]))
  expect(screen.getByRole('status')).toHaveTextContent(MODE_TEXT.palm)
  expect(p.onTurn).toHaveBeenCalledTimes(1)
  expect(p.onTurn.mock.calls[0][0]).toBeLessThan(0)
  const apart = (k: number) => f.twoPalms.map((hand) => hand.map((q) => ({ x: 0.5 + (q.x - 0.5) * k, y: q.y })))
  feed([1, 1, 1, 1.05].map(apart), 1000)
  expect(screen.getByRole('status')).toHaveTextContent(MODE_TEXT['two-palms'])
  expect(p.onZoom).toHaveBeenCalledTimes(1)
  expect(p.onZoom.mock.calls[0][0]).toBeLessThan(1)
})

it('says why the camera could not start', async () => {
  tracker.start = () => Promise.reject(new TrackerError('denied'))
  render(<HandsMode {...props()} />)
  expect(await screen.findByText(PROBLEM_TEXT.denied)).toBeInTheDocument()
})

it('stops a camera that finishes opening after the overlay has gone', async () => {
  let open: (stop: () => void) => void = () => {}
  tracker.start = () => new Promise((resolve) => (open = resolve))
  const { unmount } = render(<HandsMode {...props()} />)
  unmount()
  await act(async () => open(tracker.stop))
  expect(tracker.stop).toHaveBeenCalled()
})

it('says the camera failed when the start fails for an unknown reason', async () => {
  tracker.start = () => Promise.reject(new Error('x'))
  render(<HandsMode {...props()} />)
  expect(await screen.findByText(PROBLEM_TEXT.failed)).toBeInTheDocument()
})
