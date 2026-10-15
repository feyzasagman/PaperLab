import { useEffect, useRef, useState } from 'react'
import type { Hand, Point } from './gestures'
import { dwellStep, handStep, initialDwell, initialHandState, REARM, type HandMode } from './handControl'
import { startTracker, TrackerError, type TrackerProblem } from './handTracker'

export type HandsModeProps = {
  onTurn: (azimuth: number, polar: number) => void
  onZoom: (factor: number) => void
  /** The paper drawn under a point of the view (x/y 0–1 across and down), or null. */
  pickAt: (x: number, y: number) => { id: string; title: string } | null
  onSelect: (id: string) => void
  /** A two-finger flick: 1 steps to the next linked paper, -1 to the previous. */
  onHop?: (step: 1 | -1) => void
}

export const MODE_TEXT: Record<HandMode | 'starting', string> = {
  starting: 'Starting the camera…',
  none: 'Show your hand to the camera',
  point: 'Pointing: hold still on a paper to open it',
  two: 'Two fingers: flick left or right to go to the previous or next linked paper',
  palm: 'Open hand: move it to turn the view',
  'two-palms': 'Two open hands: move them apart to zoom in, together to zoom out',
  fist: 'Fist: paused',
  other: 'Point, open your hand, or make a fist',
}

export const PROBLEM_TEXT: Record<TrackerProblem, string> = {
  denied: 'Camera access is blocked. Allow PaperLab to use the camera in your system settings, then turn Hands mode on again.',
  'no-camera': 'No camera found.',
  busy: 'The camera is in use by another app.',
  failed: "Hand tracking couldn't start in this browser.",
}

export const GESTURES =
  'Point to aim · Hold on a paper to open it · Two fingers and flick to hop between linked papers · Open hand to turn · Two open hands to zoom · Fist to pause. The camera picture stays on this computer.'

type Pointer = { at: Point; title: string | null; progress: number }

/**
 * Hands mode, over the 3D view: the camera picture, what the hands are doing, and a pointer that opens the paper it
 * rests on. The camera runs while this is mounted and stops when it goes.
 */
export default function HandsMode(props: HandsModeProps) {
  const video = useRef<HTMLVideoElement>(null)
  // The tracker starts once per mount; it reads the latest callbacks through this ref instead of restarting.
  const latest = useRef(props)
  useEffect(() => {
    latest.current = props
  })
  const [mode, setMode] = useState<HandMode | 'starting'>('starting')
  const [problem, setProblem] = useState<TrackerProblem | null>(null)
  const [pointer, setPointer] = useState<Pointer | null>(null)

  useEffect(() => {
    let hands = initialHandState()
    let dwell = initialDwell()
    let opened: Point | null = null
    let stop: (() => void) | null = null
    let gone = false
    const onFrame = (seen: Hand[], now: number) => {
      const [next, moves] = handStep(hands, seen)
      hands = next
      setMode(moves.mode)
      const { onTurn, onZoom, pickAt, onSelect, onHop } = latest.current
      if (moves.turn) onTurn(moves.turn.azimuth, moves.turn.polar)
      if (moves.zoom !== null) onZoom(moves.zoom)
      if (moves.hop) onHop?.(moves.hop)
      if (!moves.pointer) {
        dwell = initialDwell()
        opened = null
        setPointer(null)
        return
      }
      const paper = pickAt(moves.pointer.x, moves.pointer.y)
      if (opened && Math.hypot(moves.pointer.x - opened.x, moves.pointer.y - opened.y) > REARM) opened = null
      if (opened) {
        dwell = initialDwell()
        setPointer({ at: moves.pointer, title: paper?.title ?? null, progress: 0 })
        return
      }
      const held = dwellStep(dwell, paper?.id ?? null, now)
      dwell = held.dwell
      setPointer({ at: moves.pointer, title: paper?.title ?? null, progress: held.progress })
      if (held.selected) {
        opened = moves.pointer
        onSelect(held.selected)
      }
    }
    startTracker(video.current!, onFrame, (problem) => {
      if (!gone) setProblem(problem)
    })
      .then((stopTracker) => {
        if (gone) stopTracker()
        else stop = stopTracker
      })
      .catch((error: unknown) => {
        if (!gone) setProblem(error instanceof TrackerError ? error.problem : 'failed')
      })
    return () => {
      gone = true
      stop?.()
    }
  }, [])

  return (
    <>
      <div className="pointer-events-none absolute top-3 left-3 flex max-w-[calc(100%-4.5rem)] flex-col items-start gap-1">
        {/* Mirrored like a selfie, so moving a hand right moves it right on screen too. */}
        <video ref={video} aria-hidden muted playsInline className="w-40 -scale-x-100 rounded-md border bg-black/40" />
        <p role="status" className="rounded-md bg-background/85 px-2 py-1 text-xs">
          {problem ? PROBLEM_TEXT[problem] : MODE_TEXT[mode]}
        </p>
        {!problem && <p className="rounded-md bg-background/85 px-2 py-1 text-xs text-muted-foreground">{GESTURES}</p>}
      </div>
      {pointer && (
        <div
          aria-hidden
          data-hand-pointer
          className="pointer-events-none absolute"
          style={{ left: `${pointer.at.x * 100}%`, top: `${pointer.at.y * 100}%` }}
        >
          {/* The ring fills while the pointer holds on a paper; full, the paper opens. */}
          <div
            className="size-6 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-primary"
            style={{ background: `conic-gradient(var(--color-primary) ${pointer.progress * 360}deg, transparent 0deg)` }}
          />
          {pointer.title && (
            <span className="absolute top-3 left-4 rounded bg-background/90 px-1.5 py-0.5 text-xs whitespace-nowrap shadow">
              {pointer.title}
            </span>
          )}
        </div>
      )}
    </>
  )
}
