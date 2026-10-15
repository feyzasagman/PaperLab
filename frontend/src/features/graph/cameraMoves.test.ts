import { expect, it } from 'vitest'
import { cameraTarget, keyAction, MAX_DISTANCE, MIN_DISTANCE, orbited, zoomed, type Vec3 } from './cameraMoves'

const origin: Vec3 = { x: 0, y: 0, z: 0 }
const distance = (a: Vec3, b: Vec3) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z)
const expectNear = (actual: Vec3, expected: Vec3) => expect(distance(actual, expected)).toBeLessThan(1e-9)
const key = (k: string, mods: Partial<KeyboardEvent> = {}) =>
  keyAction({ key: k, ctrlKey: false, metaKey: false, altKey: false, ...mods })

it('zooms along the line of sight towards the target, whatever the target', () => {
  expectNear(zoomed({ x: 0, y: 0, z: 100 }, origin, 0.8), { x: 0, y: 0, z: 80 })
  expectNear(zoomed({ x: 105, y: 5, z: 5 }, { x: 5, y: 5, z: 5 }, 0.5), { x: 55, y: 5, z: 5 })
})

it('never zooms closer than MIN_DISTANCE or farther than MAX_DISTANCE', () => {
  expect(distance(zoomed({ x: 0, y: 0, z: 40 }, origin, 0.1), origin)).toBe(MIN_DISTANCE)
  expect(distance(zoomed({ x: 0, y: 0, z: 9000 }, origin, 10), origin)).toBe(MAX_DISTANCE)
})

it('backs off along +z when the camera sits exactly on its target', () => {
  expectNear(zoomed(origin, origin, 0.8), { x: 0, y: 0, z: MIN_DISTANCE })
})

it('orbits round the vertical axis through the target, keeping distance and height', () => {
  expectNear(orbited({ x: 0, y: 7, z: 100 }, origin, Math.PI / 2), { x: 100, y: 7, z: 0 })
  const target = { x: 5, y: 5, z: 5 }
  const start = { x: 3, y: 2, z: 9 }
  const moved = orbited(start, target, 1.1)
  expect(Math.abs(distance(moved, target) - distance(start, target))).toBeLessThan(1e-9)
  expect(moved.y).toBe(2)
})

it('undoes each move with its opposite', () => {
  const target = { x: 5, y: 5, z: 5 }
  const camera = { x: 105, y: 45, z: -20 }
  expectNear(cameraTarget('zoom-out', cameraTarget('zoom-in', camera, target), target), camera)
  expectNear(cameraTarget('rotate-right', cameraTarget('rotate-left', camera, target), target), camera)
})

it('maps the view keys to moves, and ignores other keys and shortcut chords', () => {
  expect(key('ArrowLeft')).toBe('rotate-left')
  expect(key('ArrowRight')).toBe('rotate-right')
  expect(key('ArrowUp')).toBe('zoom-in')
  expect(key('ArrowDown')).toBe('zoom-out')
  expect(key('+')).toBe('zoom-in')
  expect(key('=')).toBe('zoom-in')
  expect(key('-')).toBe('zoom-out')
  expect(key('_')).toBe('zoom-out')
  expect(key('0')).toBe('reset')
  expect(key('a')).toBeNull()
  expect(key('toString')).toBeNull() // an inherited object key is not a view key
  expect(key('-', { metaKey: true })).toBeNull() // Cmd − zooms the page, not the graph
  expect(key('0', { ctrlKey: true })).toBeNull()
  expect(key('ArrowLeft', { altKey: true })).toBeNull()
})
