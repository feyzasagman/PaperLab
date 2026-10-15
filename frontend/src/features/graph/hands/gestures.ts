/** One landmark from the hand tracker: x and y run 0–1 across and down the camera picture (not mirrored). */
export type Point = { x: number; y: number }
/** The tracker's 21 landmarks for one hand: 0 is the wrist, 8 the index fingertip (MediaPipe's hand model). */
export type Hand = readonly Point[]
export type Gesture = 'point' | 'palm' | 'fist' | 'other'

const WRIST = 0
const INDEX_TIP = 8
/** [middle joint, tip] of the index, middle, ring and little fingers. The thumb never decides a gesture. */
const FINGERS = [
  [6, 8],
  [10, 12],
  [14, 16],
  [18, 20],
] as const
/** The wrist and the four knuckles: their average is the palm's middle, steadier than any fingertip. */
const PALM = [0, 5, 9, 13, 17] as const
/**
 * A finger is straight when its tip is farther from the wrist than its middle joint is, by this ratio. Measured on
 * real hands: straight fingers give 1.3 or more, curled ones 0.8 or less, whichever way the hand faces.
 */
export const STRAIGHT = 1

const distance = (a: Point, b: Point) => Math.hypot(a.x - b.x, a.y - b.y)

/** Which of the index, middle, ring and little fingers are straight, in that order. */
export function straightFingers(hand: Hand): boolean[] {
  return FINGERS.map(([joint, tip]) => distance(hand[WRIST], hand[tip]) > distance(hand[WRIST], hand[joint]) * STRAIGHT)
}

/** Only the index straight is pointing; all four straight is an open hand; none is a fist (a thumbs-up counts). */
export function gestureOf(hand: Hand): Gesture {
  const [index, ...others] = straightFingers(hand)
  if (index && others.every((straight) => !straight)) return 'point'
  if (index && others.every(Boolean)) return 'palm'
  if (!index && others.every((straight) => !straight)) return 'fist'
  return 'other'
}

export const fingertip = (hand: Hand): Point => hand[INDEX_TIP]

export function palmCentre(hand: Hand): Point {
  const sum = PALM.reduce((total, i) => ({ x: total.x + hand[i].x, y: total.y + hand[i].y }), { x: 0, y: 0 })
  return { x: sum.x / PALM.length, y: sum.y / PALM.length }
}
