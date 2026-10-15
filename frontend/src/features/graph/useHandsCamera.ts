import { useCallback } from 'react'
import type { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { orbited, tilted, zoomed, type Vec3 } from './cameraMoves'

/** What hands mode needs from the 3D graph's handle (react-force-graph-3d's ForceGraphMethods has all of it). */
export type GraphHandle = {
  camera(): { position: Vec3 }
  controls(): object
  cameraPosition(position: Vec3, lookAt: Vec3, transitionMs: number): unknown
  graph2ScreenCoords(x: number, y: number, z: number): { x: number; y: number }
}

/** The orbit controls behind a graph handle; undefined until the canvas has mounted. */
export const orbitOf = (graph: { controls(): object } | undefined) => graph?.controls() as OrbitControls | undefined

/** Hands mode picks the paper drawn nearest its pointer, within this many pixels. */
const PICK_RADIUS = 48

type Placed = { id: string; title: string; x?: number; y?: number; z?: number }

/**
 * Hands mode's camera. Turns, tilts and zooms set the camera straight to each frame's pose (frames come ~30 times a
 * second, too often to glide between); `onMove` runs before each. `pickAt` finds the paper under the pointer.
 */
export function useHandsCamera(
  graph: () => GraphHandle | undefined,
  papers: readonly Placed[],
  size: { width: number; height: number },
  onMove: () => void,
) {
  const nudge = useCallback(
    (step: (camera: Vec3, target: Vec3) => Vec3) => {
      const handle = graph()
      const target = orbitOf(handle)?.target
      if (!handle || !target) return
      onMove()
      const { x, y, z } = handle.camera().position
      const lookAt: Vec3 = { x: target.x, y: target.y, z: target.z }
      handle.cameraPosition(step({ x, y, z }, lookAt), lookAt, 0)
    },
    [graph, onMove],
  )
  const turnBy = useCallback(
    (azimuth: number, polar: number) => nudge((camera, target) => tilted(orbited(camera, target, azimuth), target, polar)),
    [nudge],
  )
  const zoomBy = useCallback((factor: number) => nudge((camera, target) => zoomed(camera, target, factor)), [nudge])

  // The paper drawn nearest a point of the view (x/y 0–1 across and down), if one is within PICK_RADIUS pixels.
  const pickAt = useCallback(
    (x: number, y: number) => {
      const handle = graph()
      const target = orbitOf(handle)?.target
      if (!handle || !target) return null
      const cam = handle.camera().position
      let best: { id: string; title: string } | null = null
      let nearestDepth = Infinity
      for (const paper of papers) {
        if (paper.x === undefined) continue
        // A paper behind the camera still projects onto the screen, so only papers in front of it count, and of those
        // under the pointer the nearest wins: it hides the ones behind.
        const ahead =
          (paper.x - cam.x) * (target.x - cam.x) +
          ((paper.y ?? 0) - cam.y) * (target.y - cam.y) +
          ((paper.z ?? 0) - cam.z) * (target.z - cam.z)
        if (ahead <= 0) continue
        const at = handle.graph2ScreenCoords(paper.x, paper.y ?? 0, paper.z ?? 0)
        const gap = Math.hypot(at.x - x * size.width, at.y - y * size.height)
        if (gap < PICK_RADIUS && ahead < nearestDepth) {
          nearestDepth = ahead
          best = { id: paper.id, title: paper.title }
        }
      }
      return best
    },
    [graph, papers, size],
  )

  return { turnBy, zoomBy, pickAt }
}
