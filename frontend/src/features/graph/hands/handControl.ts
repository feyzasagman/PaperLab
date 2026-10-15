import { fingertip, gestureOf, palmCentre, type Hand, type Point } from './gestures'

/** What the hands are doing, once it has held for a few frames. */
export type HandMode = 'none' | 'point' | 'palm' | 'two-palms' | 'fist' | 'other'

/** What one camera frame asks of the view. Pointer x/y run 0–1 across and down the 3D view, mirrored like a selfie. */
export type HandMoves = {
  mode: HandMode
  pointer: Point | null
  turn: { azimuth: number; polar: number } | null
  zoom: number | null
}

export type HandState = {
  mode: HandMode
  candidate: HandMode
  streak: number
  pointer: Point | null
  anchor: Point | null
  spread: number | null
}

// ponytail: every constant below is a calibration knob, tuned on MediaPipe's test photos and a laptop webcam.
/** Frames a new gesture must hold before it counts, so one misread frame never moves the view. */
export const STABLE_FRAMES = 3
/** The outer band of the picture the pointer never needs: the middle 70% spans the whole view. */
export const EDGE = 0.15
/** How far the pointer moves towards the fingertip each frame (1 = no smoothing). */
export const SMOOTHING = 0.35
/** A palm move across the whole picture turns the view one full turn; top to bottom tips it half a turn. */
export const TURN_GAIN = 2 * Math.PI
export const TILT_GAIN = Math.PI
/** Palm moves smaller than this are tracker jitter; larger than JUMP is a lost-and-found hand, not a move. */
export const DEAD_ZONE = 0.003
export const JUMP = 0.15
/** One frame never turns or tips the view more than this many radians, however far the hand moved since the last steady frame. */
export const TURN_STEP_LIMIT = 0.3
/** Two-hand spread changes below 0.5% are jitter; a change past 25% in one frame is a jump, not a zoom. */
export const ZOOM_JITTER = 0.005
export const ZOOM_JUMP = 1.25
/** One frame never zooms more than 15% either way. */
export const ZOOM_STEP_LIMIT = 1.15

export const initialHandState = (): HandState => ({
  mode: 'none',
  candidate: 'none',
  streak: 0,
  pointer: null,
  anchor: null,
  spread: null,
})

const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value))
const toView = (value: number) => clamp((value - EDGE) / (1 - 2 * EDGE), 0, 1)

function modeOf(hands: readonly Hand[]): HandMode {
  if (hands.length === 0) return 'none'
  const gestures = hands.map(gestureOf)
  if (gestures.length >= 2 && gestures[0] === 'palm' && gestures[1] === 'palm') return 'two-palms'
  return gestures[0]
}

/**
 * One camera frame in, the view's moves out. A new gesture counts only after STABLE_FRAMES frames, and starting one
 * forgets the last gesture's reference point, so switching gestures never makes the view jump.
 */
export function handStep(state: HandState, hands: readonly Hand[]): [HandState, HandMoves] {
  const seen = modeOf(hands)
  const streak = seen === state.candidate ? state.streak + 1 : 1
  const switched = streak >= STABLE_FRAMES && seen !== state.mode
  const mode = streak >= STABLE_FRAMES ? seen : state.mode
  const base: HandState = switched
    ? { mode, candidate: seen, streak, pointer: null, anchor: null, spread: null }
    : { ...state, candidate: seen, streak }
  const still: HandMoves = { mode, pointer: null, turn: null, zoom: null }
  // The view acts only while the frame shows the gesture that is in force.
  if (seen !== mode) return [base, { ...still, pointer: mode === 'point' ? base.pointer : null }]

  if (mode === 'point') {
    const tip = fingertip(hands[0])
    const goal = { x: toView(1 - tip.x), y: toView(tip.y) }
    const was = base.pointer
    const pointer = was ? { x: was.x + (goal.x - was.x) * SMOOTHING, y: was.y + (goal.y - was.y) * SMOOTHING } : goal
    return [{ ...base, pointer }, { ...still, pointer }]
  }

  if (mode === 'palm') {
    const centre = palmCentre(hands[0])
    const here = { x: 1 - centre.x, y: centre.y }
    const was = base.anchor
    if (!was) return [{ ...base, anchor: here }, still]
    const dx = here.x - was.x
    const dy = here.y - was.y
    const step = Math.hypot(dx, dy)
    if (step > JUMP) return [{ ...base, anchor: here }, still]
    if (step < DEAD_ZONE) return [base, still]
    // Like dragging the scene with the hand: move right and the papers follow to the right.
    const turn = {
      azimuth: clamp(-dx * TURN_GAIN, -TURN_STEP_LIMIT, TURN_STEP_LIMIT),
      polar: clamp(dy * TILT_GAIN, -TURN_STEP_LIMIT, TURN_STEP_LIMIT),
    }
    return [{ ...base, anchor: here }, { ...still, turn }]
  }

  if (mode === 'two-palms') {
    const a = palmCentre(hands[0])
    const b = palmCentre(hands[1])
    const spread = Math.hypot(a.x - b.x, a.y - b.y)
    const was = base.spread
    if (was === null || spread === 0) return [{ ...base, spread }, still]
    const ratio = spread / was
    if (ratio > ZOOM_JUMP || ratio < 1 / ZOOM_JUMP) return [{ ...base, spread }, still]
    if (Math.abs(ratio - 1) < ZOOM_JITTER) return [base, still]
    // Hands apart brings the camera closer: the zoom factor is the inverse of the spread's growth.
    const zoom = clamp(1 / ratio, 1 / ZOOM_STEP_LIMIT, ZOOM_STEP_LIMIT)
    return [{ ...base, spread }, { ...still, zoom }]
  }

  return [base, still]
}

/** Holding the pointer on one paper this long opens it. */
export const DWELL_MS = 1000

export type Dwell = { id: string | null; since: number; done: boolean }
export const initialDwell = (): Dwell => ({ id: null, since: 0, done: false })

/** Holding on one paper for DWELL_MS selects it once; moving to another paper, or off, starts the count again. */
export function dwellStep(
  dwell: Dwell,
  hovered: string | null,
  now: number,
): { dwell: Dwell; progress: number; selected: string | null } {
  if (hovered !== dwell.id) return { dwell: { id: hovered, since: now, done: false }, progress: 0, selected: null }
  if (hovered === null || dwell.done) return { dwell, progress: dwell.done ? 1 : 0, selected: null }
  const progress = Math.min(1, (now - dwell.since) / DWELL_MS)
  if (progress < 1) return { dwell, progress, selected: null }
  return { dwell: { ...dwell, done: true }, progress: 1, selected: hovered }
}
