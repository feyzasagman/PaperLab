import { Component, lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { ForceGraphMethods, LinkObject, NodeObject } from 'react-force-graph-3d'
import { CHART_INK } from '@/features/charts/palette'
import { GraphLoadError } from './GraphCanvas'
import { entranceScale, logoStart } from './logoStart'
import { carryPositions, degrees, FADED, nodeLabel, sizedNodes, tooltipFor, withAlpha, type SizedNode } from './graphModel'
import { useBoxSize } from './useBoxSize'
import { emphasis, labelledIds, linkState, shortTitle } from './paperModel'
import { hasWebGL, WEBGL_FAILED, WEBGL_OFF, type ViewProps } from './viewModel'
import type { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { CameraControls } from './CameraControls'
import {
  cameraTarget,
  keyAction,
  MAX_DISTANCE,
  MIN_DISTANCE,
  MOVE_MS,
  orbited,
  tilted,
  zoomed,
  type CameraAction,
  type Vec3,
} from './cameraMoves'

// Loaded the first time 3D is picked, like 2D: react-force-graph-3d brings three.js, which no other view needs. This
// file is the only one that imports it. A failed dynamic import is cached for the page's lifetime, so the cache is
// cleared on failure and the error offers a reload.
// The glow and names (paperNodes, three.js) come in the same load, so three.js still stays out of every other view.
type Loaded = [typeof import('react-force-graph-3d'), typeof import('./paperNodes')]
let forceGraph3d: Promise<Loaded> | null = null
function loadForceGraph3d() {
  if (!forceGraph3d) {
    forceGraph3d = Promise.all([import('react-force-graph-3d'), import('./paperNodes')] as const).catch((error: unknown) => {
      forceGraph3d = null
      throw error
    })
  }
  return forceGraph3d
}

// Asked once per page load, not once per mount: the probe releases its context (viewModel's hasWebGL), but there's
// still no reason to ask again on every view switch. Reused for the rest of the page's life.
let webgl: boolean | undefined

type BoundaryState = { failed: boolean }

/**
 * Catches a 3D start-up failure the WebGL probe in `hasWebGL` can't predict: a lost context, a driver crash, or the
 * browser's cap on live WebGL contexts. three-forcegraph's `WebGLRenderer` throws inside a `useLayoutEffect` on
 * mount, and the app has no error boundary elsewhere, so this one wraps only the box that mounts `<Graph>` — the
 * rest of the page, including the tabs above it, is untouched. No logging: the failure has nowhere useful to go.
 */
class CanvasBoundary extends Component<{ children: ReactNode }, BoundaryState> {
  state: BoundaryState = { failed: false }

  static getDerivedStateFromError(): BoundaryState {
    return { failed: true }
  }

  render() {
    // Outside any aria-hidden wrapper, exactly like the WebGL-off message: a screen reader must still reach it.
    if (this.state.failed) {
      return (
        <p className="grid min-h-[28rem] flex-1 place-items-center px-6 text-center text-muted-foreground">
          {WEBGL_FAILED}
        </p>
      )
    }
    return this.props.children
  }
}

/** The gold of the selected paper's glow, for its own links. */
const ACTIVE_LINK = '#eab308'

/** A slow spin once the layout settles, so the map reads as 3D: about one turn in 100 s. */
const AUTO_ROTATE_SPEED = 0.6

/** The orbit controls behind a graph handle; undefined until the canvas has mounted. */
const orbitOf = (graph: { controls(): object } | undefined) => graph?.controls() as OrbitControls | undefined

// Loaded the first time Hands mode is turned on, never before: the hand tracker is a large download most owners never
// need, and a static import would break the 3D view wherever it isn't installed yet. A failed load says so in place.
const HandsMode = lazy(() =>
  import('./hands/HandsMode').catch(() => ({
    default: () => (
      <p role="status" className="absolute top-3 left-3 rounded-md bg-background/85 px-2 py-1 text-xs">
        Hand tracking couldn't start in this browser.
      </p>
    ),
  })),
)

/** Hands mode picks the paper drawn nearest its pointer, within this many pixels. */
const PICK_RADIUS = 48

/** react-force-graph-3d writes x/y/z and velocities onto the objects it is given, so it gets its own copies. */
type Node3D = SizedNode & { x?: number; y?: number; z?: number }
type Link3D = {
  source: string | { id: string }
  target: string | { id: string }
  kind: ViewProps['links'][number]['kind']
  label: string | null
}

/** Which clusters overlap in 2D (spec §5.2): free physics in three dimensions, orbit by dragging, zoom by scrolling. */
export function Graph3DView({ nodes, links, theme, colors, focusId, inFocus, onSelect }: ViewProps) {
  const [Graph, setGraph] = useState<typeof import('react-force-graph-3d').default | null>(null)
  const [papers, setPapers] = useState<typeof import('./paperNodes') | null>(null)
  const [failed, setFailed] = useState(false)
  // Asked once, before the canvas mounts: without WebGL three.js would throw inside React.
  const [webglSupported] = useState(() => (webgl ??= hasWebGL()))
  const [box, size] = useBoxSize<HTMLDivElement>()
  const previous = useRef<Node3D[]>([])
  const graphRef = useRef<ForceGraphMethods<NodeObject<Node3D>, LinkObject<Node3D, Link3D>> | undefined>(undefined)
  // Fit once per mount, when the layout first settles: a later rebuild (a layer toggle, a theme change) must never
  // yank a camera the owner has moved. A view switch remounts this component (Radix drops the unpicked tab), so
  // returning to 3D fits again.
  const fitted = useRef(false)
  // The group around the canvas: it takes the keys, and carries the camera's state for tests (data-camera-*).
  const view = useRef<HTMLDivElement>(null)
  // The owner has moved the view at least once: the how-to line goes and the spin never starts again.
  const [touched, setTouched] = useState(false)
  // Where the last button or key press is sending the camera, until its glide ends: a second press during the glide
  // builds on that goal, not on the camera's half-way pose (which orbits through odd distances).
  const aim = useRef<{ until: number; position: Vec3; lookAt: Vec3 } | null>(null)
  const [handsOn, setHandsOn] = useState(false)

  useEffect(() => {
    let cancelled = false
    loadForceGraph3d()
      .then(([module, paperNodes]) => {
        if (cancelled) return
        setGraph(() => module.default)
        setPapers(paperNodes)
      })
      .catch(() => !cancelled && setFailed(true))
    return () => {
      cancelled = true
    }
  }, [])

  const data = useMemo(() => {
    // oxlint-disable-next-line react/refs -- read once per rebuild, for where the simulation left each paper
    const carried: Node3D[] = carryPositions(sizedNodes(nodes, links, colors, theme), previous.current)
    // The first layout only, with motion allowed: papers start on the logo and spring out (logoStart).
    // oxlint-disable-next-line react/refs -- same read as above
    const first = previous.current.length === 0 && !matchMedia('(prefers-reduced-motion: reduce)').matches
    const graphNodes = first ? logoStart(carried, entranceScale(carried.length)) : carried
    const graphLinks = links.map((link) => ({
      source: link.source,
      target: link.target,
      kind: link.kind,
      label: link.label ?? null,
    }))
    return { nodes: graphNodes, links: graphLinks }
  }, [nodes, links, colors, theme])

  useEffect(() => {
    previous.current = data.nodes
  }, [data])

  // The selected paper glows and is named, the papers it links to directly glow softer and are named too, so the
  // owner always knows which paper they are looking at. The spheres stay the library's own (extended, not replaced).
  const named = useMemo(() => labelledIds(links, focusId), [links, focusId])
  const emphasisObject = useCallback(
    (node: Node3D) =>
      papers!.emphasisObject(emphasis(node.id, focusId, inFocus), named.has(node.id) ? shortTitle(node.title) : null, node.radius, theme),
    [papers, focusId, inFocus, named, theme],
  )

  // The first fit frames the linked papers: one with no links at all, far off on its own, would shrink the rest.
  const linked = useMemo(() => new Set([...degrees(links)].filter(([, n]) => n > 0).map(([id]) => id)), [links])

  const fit = useCallback(
    () => graphRef.current?.zoomToFit(MOVE_MS, 40, (node) => linked.size === 0 || linked.has(String(node.id))),
    [linked],
  )

  const stopSpin = useCallback(() => {
    const controls = orbitOf(graphRef.current)
    if (controls) controls.autoRotate = false
    if (view.current) view.current.dataset.autoRotate = 'false'
  }, [])

  // Every move the owner makes ends the spin for good and hides the how-to line. Buttons and keys send `move`; a
  // drag, scroll or click reaches the same `stopSpin` through OrbitControls' `start` event.
  const move = useCallback(
    (action: CameraAction) => {
      stopSpin()
      setTouched(true)
      const graph = graphRef.current
      if (!graph) return
      if (action === 'reset') {
        aim.current = null
        fit()
        return
      }
      // The typings lack the zero-argument getter, so the camera and its look-at point are read from three directly.
      const { x, y, z } = graph.camera().position
      const target = orbitOf(graph)?.target
      if (!target) return
      const goal = aim.current !== null && performance.now() < aim.current.until ? aim.current : null
      const from = goal ? goal.position : { x, y, z }
      const lookAt: Vec3 = goal ? goal.lookAt : { x: target.x, y: target.y, z: target.z }
      const position = cameraTarget(action, from, lookAt)
      aim.current = { until: performance.now() + MOVE_MS, position, lookAt }
      graph.cameraPosition(position, lookAt, MOVE_MS)
    },
    [fit, stopSpin, setTouched],
  )

  // Hands mode moves the camera straight to each frame's pose: frames come about thirty times a second, too often to
  // glide between, and each step is small.
  const nudge = useCallback(
    (step: (camera: Vec3, target: Vec3) => Vec3) => {
      const graph = graphRef.current
      const target = orbitOf(graph)?.target
      if (!graph || !target) return
      aim.current = null
      const { x, y, z } = graph.camera().position
      const lookAt: Vec3 = { x: target.x, y: target.y, z: target.z }
      graph.cameraPosition(step({ x, y, z }, lookAt), lookAt, 0)
    },
    [],
  )
  const turnBy = useCallback(
    (azimuth: number, polar: number) => nudge((camera, target) => tilted(orbited(camera, target, azimuth), target, polar)),
    [nudge],
  )
  const zoomBy = useCallback((factor: number) => nudge((camera, target) => zoomed(camera, target, factor)), [nudge])

  // The paper drawn nearest a point of the view (x/y 0–1 across and down), if one is within PICK_RADIUS pixels.
  const pickAt = useCallback(
    (x: number, y: number) => {
      const graph = graphRef.current
      if (!graph) return null
      let best: { id: string; title: string } | null = null
      let nearest = PICK_RADIUS
      for (const node of data.nodes) {
        if (node.x === undefined) continue
        const at = graph.graph2ScreenCoords(node.x, node.y ?? 0, node.z ?? 0)
        const gap = Math.hypot(at.x - x * size.width, at.y - y * size.height)
        if (gap < nearest) {
          nearest = gap
          best = { id: node.id, title: node.title }
        }
      }
      return best
    },
    [data, size],
  )

  // Turning Hands mode on counts as the owner taking over: the spin stops and the how-to line goes.
  const toggleHands = useCallback(() => {
    stopSpin()
    setTouched(true)
    setHandsOn((on) => !on)
  }, [stopSpin, setTouched, setHandsOn])

  // OrbitControls is ready once the canvas has mounted. The scroll wheel gets the same limits as the buttons, and
  // every camera change (a drag, a glide, the spin) is written onto the group for the E2E tests to read.
  const canvasReady = Graph !== null && size.width > 0
  useEffect(() => {
    const controls = canvasReady ? orbitOf(graphRef.current) : undefined
    const group = view.current
    if (!controls || !group) return
    controls.minDistance = MIN_DISTANCE
    controls.maxDistance = MAX_DISTANCE
    controls.autoRotateSpeed = AUTO_ROTATE_SPEED
    const show = () => {
      group.dataset.cameraDistance = controls.getDistance().toFixed(1)
      group.dataset.cameraAzimuth = ((controls.getAzimuthalAngle() * 180) / Math.PI).toFixed(1)
    }
    const takeOver = () => {
      aim.current = null
      stopSpin()
      setTouched(true)
    }
    controls.addEventListener('change', show)
    controls.addEventListener('start', takeOver)
    return () => {
      controls.removeEventListener('change', show)
      controls.removeEventListener('start', takeOver)
    }
  }, [canvasReady, stopSpin])

  // Selecting a paper flies the camera to it, so it sits in the middle of the view, close enough to read its name.
  useEffect(() => {
    const graph = graphRef.current
    const node = focusId === null ? undefined : data.nodes.find((n) => n.id === focusId)
    if (!graph || !node || node.x === undefined) return
    const { x = 0, y = 0, z = 0 } = node
    const away = 1 + 190 / Math.max(Math.hypot(x, y, z), 1)
    stopSpin()
    aim.current = null
    graph.cameraPosition({ x: x * away, y: y * away, z: z * away }, { x, y, z }, 900)
  }, [focusId, data, stopSpin])

  if (failed) return <GraphLoadError />

  const ink = CHART_INK[theme]
  // The selected paper's own links are drawn thick and gold, like its glow; links leaving the focus fade.
  const state = (link: Link3D) => linkState(link, focusId, inFocus)

  return (
    <div data-view="3d" data-ready={Graph !== null} className="flex min-h-0 flex-1 flex-col">
      {!webglSupported ? (
        <p className="grid min-h-[28rem] flex-1 place-items-center px-6 text-center text-muted-foreground">{WEBGL_OFF}</p>
      ) : (
        <CanvasBoundary>
          <div
            ref={view}
            role="group"
            aria-label="3D graph"
            aria-keyshortcuts="ArrowLeft ArrowRight ArrowUp ArrowDown Plus - 0"
            tabIndex={0}
            data-auto-rotate="false"
            onKeyDown={(event) => {
              const action = keyAction(event)
              if (!action) return
              event.preventDefault() // arrows turn the view, not the page
              move(action)
            }}
            className="relative flex min-h-0 flex-1 flex-col rounded-xl outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            <div ref={box} aria-hidden className="relative min-h-[28rem] flex-1 overflow-hidden rounded-xl">
              {Graph === null || size.width === 0 ? (
                <div className="h-full w-full animate-pulse rounded-xl bg-muted" />
              ) : (
                <Graph
                  ref={graphRef}
                  width={size.width}
                  height={size.height}
                  graphData={data}
                  backgroundColor="rgba(0,0,0,0)"
                  showNavInfo={false}
                  controlType="orbit"
                  nodeId="id"
                  // As in 2D: float-tooltip sets a string label as innerHTML, so a title only ever goes in as an element.
                  nodeLabel={(node: Node3D) => tooltipFor(nodeLabel(node)) as unknown as string}
                  nodeRelSize={1}
                  nodeVal={(node: Node3D) => node.radius ** 3}
                  nodeOpacity={1}
                  nodeColor={(node: Node3D) =>
                    inFocus !== null && !inFocus.has(node.id) ? withAlpha(node.color, FADED) : node.color
                  }
                  linkOpacity={1}
                  linkColor={(link: Link3D) =>
                    state(link) === 'active'
                      ? ACTIVE_LINK
                      : withAlpha(link.kind === 'manual' ? ink.text : ink.muted, state(link) === 'faded' ? FADED : 0.55)
                  }
                  // 0 draws a one-pixel line, the 2D view's 1; text along a 3D link would need another dependency, so
                  // a `manual` link's label is its hover label instead.
                  linkWidth={(link: Link3D) => (state(link) === 'active' ? 1.8 : link.kind === 'manual' ? 2.5 : 0)}
                  linkLabel={(link: Link3D) => (link.label ? tooltipFor(link.label) : null) as unknown as string}
                  linkDirectionalArrowLength={(link: Link3D) => (link.kind === 'cites' || link.kind === 'manual' ? 4 : 0)}
                  linkDirectionalArrowRelPos={1}
                  nodeThreeObject={papers ? emphasisObject : undefined}
                  nodeThreeObjectExtend
                  onNodeClick={(node: Node3D) => onSelect(node.id)}
                  onEngineStop={() => {
                    if (fitted.current) return
                    fitted.current = true
                    fit()
                    if (view.current) view.current.dataset.fitted = 'true'
                    // The spin starts only on an untouched view with no paper chosen, and never under reduced motion.
                    const controls = orbitOf(graphRef.current)
                    if (!controls || touched || focusId !== null) return
                    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return
                    controls.autoRotate = true
                    if (view.current) view.current.dataset.autoRotate = 'true'
                  }}
                  cooldownTicks={120}
                />
              )}
            </div>
            {canvasReady && (
              <CameraControls onAction={move} showHint={!touched} hands={{ on: handsOn, onToggle: toggleHands }} />
            )}
            {canvasReady && handsOn && (
              <Suspense fallback={null}>
                <HandsMode onTurn={turnBy} onZoom={zoomBy} pickAt={pickAt} onSelect={onSelect} />
              </Suspense>
            )}
          </div>
        </CanvasBoundary>
      )}
    </div>
  )
}
