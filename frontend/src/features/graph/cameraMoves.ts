/** A point in the 3D view's space: the camera's position, or the point it looks at. */
export type Vec3 = { x: number; y: number; z: number }

/**
 * Every way the owner can move the 3D camera besides dragging. The on-screen buttons and the keyboard both send
 * these; a later input (hand tracking) sends the same ones, so the moves live in one place.
 */
export type CameraAction = 'zoom-in' | 'zoom-out' | 'rotate-left' | 'rotate-right' | 'reset'

/** One zoom step: in brings the camera 20% closer, out undoes it exactly. */
export const ZOOM_IN = 0.8
export const ZOOM_OUT = 1 / ZOOM_IN
/** One rotate step, a sixteenth of a turn. */
export const ORBIT_STEP = Math.PI / 8
/** How long a button or key move takes, in ms. */
export const MOVE_MS = 400
// ponytail: fixed limits tuned on a ~200-paper library; scale them by the fitted distance if huge libraries clip.
/** The camera never comes closer to what it looks at than this (inside a paper's sphere), or goes farther. */
export const MIN_DISTANCE = 30
export const MAX_DISTANCE = 10_000

const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value))

/** The camera moved along its line of sight by `factor` (< 1 is closer), kept between the distance limits. */
export function zoomed(camera: Vec3, target: Vec3, factor: number): Vec3 {
  const dx = camera.x - target.x
  const dy = camera.y - target.y
  const dz = camera.z - target.z
  const distance = Math.hypot(dx, dy, dz)
  // Sitting on the target there's no line of sight to move along: back off along +z, where the view starts.
  if (distance === 0) return { x: target.x, y: target.y, z: target.z + MIN_DISTANCE }
  const scale = clamp(distance * factor, MIN_DISTANCE, MAX_DISTANCE) / distance
  return { x: target.x + dx * scale, y: target.y + dy * scale, z: target.z + dz * scale }
}

/** The camera carried round the vertical axis through the target by `radians`, at the same distance and height. */
export function orbited(camera: Vec3, target: Vec3, radians: number): Vec3 {
  const dx = camera.x - target.x
  const dz = camera.z - target.z
  const cos = Math.cos(radians)
  const sin = Math.sin(radians)
  return { x: target.x + dx * cos + dz * sin, y: camera.y, z: target.z - dx * sin + dz * cos }
}

/** The camera never tips closer than this to straight above or below what it looks at (OrbitControls flips there). */
export const MIN_POLAR = 0.15

/** The camera tipped round the target by `radians` (positive looks from higher up), at the same distance. */
export function tilted(camera: Vec3, target: Vec3, radians: number): Vec3 {
  const dx = camera.x - target.x
  const dy = camera.y - target.y
  const dz = camera.z - target.z
  const distance = Math.hypot(dx, dy, dz)
  if (distance === 0) return { ...camera }
  const flat = Math.hypot(dx, dz)
  const polar = clamp(Math.atan2(flat, dy) - radians, MIN_POLAR, Math.PI - MIN_POLAR)
  // Straight above or below there's no direction to lean towards: lean towards +z, where the view starts.
  const [ux, uz] = flat === 0 ? [0, 1] : [dx / flat, dz / flat]
  const across = distance * Math.sin(polar)
  return { x: target.x + ux * across, y: target.y + distance * Math.cos(polar), z: target.z + uz * across }
}

/** Where a move other than reset puts the camera; reset is the view's own fit, so it has no position here. */
export function cameraTarget(action: Exclude<CameraAction, 'reset'>, camera: Vec3, target: Vec3): Vec3 {
  switch (action) {
    case 'zoom-in':
      return zoomed(camera, target, ZOOM_IN)
    case 'zoom-out':
      return zoomed(camera, target, ZOOM_OUT)
    case 'rotate-left':
      return orbited(camera, target, ORBIT_STEP)
    case 'rotate-right':
      return orbited(camera, target, -ORBIT_STEP)
  }
}

const KEYS: Record<string, CameraAction> = {
  ArrowLeft: 'rotate-left',
  ArrowRight: 'rotate-right',
  ArrowUp: 'zoom-in',
  ArrowDown: 'zoom-out',
  '+': 'zoom-in',
  '=': 'zoom-in',
  '-': 'zoom-out',
  _: 'zoom-out',
  '0': 'reset',
}

/** The move a key asks for, or null for any other key (and for any key held with Ctrl, Cmd or Alt). */
export function keyAction(event: Pick<KeyboardEvent, 'key' | 'ctrlKey' | 'metaKey' | 'altKey'>): CameraAction | null {
  if (event.ctrlKey || event.metaKey || event.altKey) return null
  return Object.hasOwn(KEYS, event.key) ? KEYS[event.key] : null
}
