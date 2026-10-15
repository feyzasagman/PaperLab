import { expect, it } from 'vitest'
import { palmCentre, type Hand } from './gestures'
import { dwellStep, EDGE, handStep, initialDwell, initialHandState, STABLE_FRAMES, TURN_GAIN, type HandMoves, type HandState } from './handControl'
import fixtures from './handFixtures.json'

const f = fixtures as unknown as Record<'point' | 'fist' | 'palm', Hand> & { twoPalms: Hand[] }
const run = (frames: Hand[][], start: HandState = initialHandState()) => {
  let state = start
  const moves: HandMoves[] = []
  for (const hands of frames) {
    const [next, out] = handStep(state, hands)
    state = next
    moves.push(out)
  }
  return moves
}
const repeat = (hands: Hand[], n: number) => Array.from({ length: n }, () => hands)
const shifted = (hand: Hand, dx: number, dy = 0): Hand => hand.map((p) => ({ x: p.x + dx, y: p.y + dy }))
/** Both hands moved away from (k > 1) or towards (k < 1) the point between them. */
const spread = (hands: Hand[], k: number): Hand[] => {
  const [a, b] = hands.map(palmCentre)
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
  return hands.map((hand) => hand.map((p) => ({ x: mid.x + (p.x - mid.x) * k, y: mid.y + (p.y - mid.y) * k })))
}

it('acts on a gesture only once it has held for STABLE_FRAMES frames', () => {
  expect(run(repeat([f.point], STABLE_FRAMES - 1)).at(-1)!.mode).toBe('none')
  expect(run(repeat([f.point], STABLE_FRAMES)).at(-1)!.mode).toBe('point')
  expect(run(repeat([], 4)).at(-1)).toEqual({ mode: 'none', pointer: null, turn: null, zoom: null })
})

it('ignores one misread frame in the middle of a gesture, keeping the pointer where it was', () => {
  const moves = run([...repeat([f.point], 3), [f.fist], [f.point]])
  expect(moves.slice(2).map((m) => m.mode)).toEqual(['point', 'point', 'point'])
  expect(moves[3].pointer).toEqual(moves[2].pointer)
})

it('mirrors the fingertip like a selfie, the middle of the picture spanning the whole view', () => {
  const pointer = run(repeat([f.point], STABLE_FRAMES)).at(-1)!.pointer!
  expect(pointer.x).toBeCloseTo((1 - f.point[8].x - EDGE) / (1 - 2 * EDGE), 9)
  expect(pointer.y).toBeCloseTo((f.point[8].y - EDGE) / (1 - 2 * EDGE), 9)
})

it('smooths the pointer towards the fingertip instead of jumping', () => {
  const moves = run([...repeat([f.point], 3), [shifted(f.point, -0.1)]])
  const [was, now] = [moves[2].pointer!, moves[3].pointer!]
  expect(now.x).toBeGreaterThan(was.x)
  expect(now.x - was.x).toBeLessThan(0.1 / (1 - 2 * EDGE)) // part of the way, not all of it
})

it('turns the view like dragging it with an open hand, and ignores jitter and jumps', () => {
  const moves = run([0, 0, 0, -0.02, -0.0201, -0.6].map((dx) => [shifted(f.palm, dx)]))
  expect(moves[2].turn).toBeNull() // the first steady frame only sets where the hand started
  // Raw x down is the hand moving to the owner's right in a mirrored picture: the papers follow it right.
  expect(moves[3].turn!.azimuth).toBeCloseTo(-0.02 * TURN_GAIN, 9)
  expect(moves[3].turn!.polar).toBe(0)
  expect(moves[4].turn).toBeNull() // under the dead zone
  expect(moves[5].turn).toBeNull() // a jump, not a move
  const tilt = run([0, 0, 0, 0.02].map((dy) => [shifted(f.palm, 0, dy)])).at(-1)!
  expect(tilt.turn!.polar).toBeGreaterThan(0) // hand down: look from higher up
})

it('zooms in as two open hands move apart, out as they come together, and ignores a jump', () => {
  expect(run([1, 1, 1, 1.05].map((k) => spread(f.twoPalms, k))).at(-1)!.zoom).toBeCloseTo(1 / 1.05, 9)
  expect(run([1, 1, 1, 0.95].map((k) => spread(f.twoPalms, k))).at(-1)!.zoom).toBeCloseTo(1 / 0.95, 9)
  expect(run([1, 1, 1, 1.5].map((k) => spread(f.twoPalms, k))).at(-1)!.zoom).toBeNull()
})

it('switching gesture starts afresh, so the first palm frame after pointing never turns the view', () => {
  const moves = run([...repeat([f.point], 3), ...repeat([shifted(f.palm, -0.05)], 3), [shifted(f.palm, -0.07)]])
  expect(moves[5].mode).toBe('palm')
  expect(moves[5].turn).toBeNull()
  expect(moves[6].turn!.azimuth).toBeCloseTo(-0.02 * TURN_GAIN, 9)
})

it('selects the paper held under the pointer for DWELL_MS, once', () => {
  let step = dwellStep(initialDwell(), 'a', 0)
  expect(step.progress).toBe(0)
  step = dwellStep(step.dwell, 'a', 500)
  expect([step.progress, step.selected]).toEqual([0.5, null])
  step = dwellStep(step.dwell, 'a', 1000)
  expect(step.selected).toBe('a')
  step = dwellStep(step.dwell, 'a', 1500)
  expect([step.progress, step.selected]).toEqual([1, null])
  step = dwellStep(step.dwell, 'b', 1600)
  expect(step.progress).toBe(0)
  step = dwellStep(dwellStep(step.dwell, null, 1700).dwell, null, 5000)
  expect([step.progress, step.selected]).toEqual([0, null])
})
