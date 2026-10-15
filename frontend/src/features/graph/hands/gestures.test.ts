import { expect, it } from 'vitest'
import { fingertip, gestureOf, palmCentre, straightFingers, type Hand } from './gestures'
import fixtures from './handFixtures.json'

const f = fixtures as unknown as Record<'point' | 'fist' | 'palm' | 'victory' | 'thumbUp', Hand> & { twoPalms: Hand[] }

it('reads real hands from MediaPipe test photos', () => {
  expect(gestureOf(f.point)).toBe('point')
  expect(gestureOf(f.fist)).toBe('fist')
  expect(gestureOf(f.palm)).toBe('palm')
  // One of these two palms is upside down: straightness is measured from the wrist, whichever way the hand faces.
  expect(f.twoPalms.map(gestureOf)).toEqual(['palm', 'palm'])
  expect(gestureOf(f.victory)).toBe('other')
  expect(gestureOf(f.thumbUp)).toBe('fist') // the thumb never decides
})

it('lists which fingers are straight, index first', () => {
  expect(straightFingers(f.point)).toEqual([true, false, false, false])
  expect(straightFingers(f.victory)).toEqual([true, true, false, false])
})

it('finds the index fingertip and the middle of the palm', () => {
  expect(fingertip(f.point)).toEqual(f.point[8])
  const centre = palmCentre(f.palm)
  const knuckles = [0, 5, 9, 13, 17].map((i) => f.palm[i])
  expect(centre.x).toBeCloseTo(knuckles.reduce((s, p) => s + p.x, 0) / 5, 12)
  expect(centre.y).toBeCloseTo(knuckles.reduce((s, p) => s + p.y, 0) / 5, 12)
})
