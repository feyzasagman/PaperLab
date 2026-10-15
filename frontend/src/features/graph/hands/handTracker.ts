import type { HandLandmarker } from '@mediapipe/tasks-vision'
import wasmLoaderPath from '@mediapipe/tasks-vision/vision_wasm_internal.js?url'
import wasmBinaryPath from '@mediapipe/tasks-vision/vision_wasm_internal.wasm?url'
import type { Hand } from './gestures'

/** Why the camera or the tracker couldn't start. */
export type TrackerProblem = 'denied' | 'no-camera' | 'busy' | 'failed'

export class TrackerError extends Error {
  readonly problem: TrackerProblem
  constructor(problem: TrackerProblem, cause?: unknown) {
    super(problem, { cause })
    this.problem = problem
  }
}

/** Frames in a row the tracker may fail to read before hands mode gives up and says so. */
const MAX_FAILED_FRAMES = 30

// Bundled with the app (public/models), so hands mode works offline and fetches nothing from anywhere else.
const MODEL = `${import.meta.env.BASE_URL}models/hand_landmarker.task`

/** What a camera failure means for the owner. */
export function problemOf(error: unknown): TrackerProblem {
  const name = error instanceof Error || error instanceof DOMException ? error.name : ''
  if (name === 'NotAllowedError' || name === 'SecurityError') return 'denied'
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return 'no-camera'
  if (name === 'NotReadableError' || name === 'AbortError') return 'busy'
  return 'failed'
}

async function openCamera(): Promise<MediaStream> {
  if (!navigator.mediaDevices?.getUserMedia) throw new TrackerError('no-camera')
  try {
    return await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480, facingMode: 'user' }, audio: false })
  } catch (error) {
    throw new TrackerError(problemOf(error), error)
  }
}

async function loadLandmarker(): Promise<HandLandmarker> {
  try {
    const { HandLandmarker } = await import('@mediapipe/tasks-vision')
    const files = { wasmLoaderPath, wasmBinaryPath }
    const options = (delegate: 'GPU' | 'CPU') => ({
      baseOptions: { modelAssetPath: MODEL, delegate },
      runningMode: 'VIDEO' as const,
      numHands: 2,
    })
    // The GPU is faster where it works; some browsers, headless ones among them, only manage the CPU.
    return await HandLandmarker.createFromOptions(files, options('GPU')).catch(() =>
      HandLandmarker.createFromOptions(files, options('CPU')),
    )
  } catch (error) {
    throw new TrackerError('failed', error)
  }
}

/**
 * Opens the camera into `video` and calls `onFrame` with the hands seen on every new camera frame. Resolves to a stop
 * function that turns the camera off (its light too) and frees the tracker; rejects with a TrackerError.
 */
export async function startTracker(
  video: HTMLVideoElement,
  onFrame: (hands: Hand[], now: number) => void,
  onFail?: (problem: TrackerProblem) => void,
): Promise<() => void> {
  const stream = await openCamera()
  const cameraOff = () => {
    stream.getTracks().forEach((track) => track.stop())
    // Only if it still holds this stream: two starts on one element (StrictMode, or a quick off and on) must not
    // wipe the later one's picture.
    if (video.srcObject === stream) video.srcObject = null
  }
  let landmarker: HandLandmarker
  try {
    video.srcObject = stream
    await video.play()
    landmarker = await loadLandmarker()
  } catch (error) {
    cameraOff()
    throw error instanceof TrackerError ? error : new TrackerError('failed', error)
  }
  let frame = 0
  let lastFrameTime = -1
  let failedFrames = 0
  let stopped = false
  const stop = () => {
    if (stopped) return
    stopped = true
    cancelAnimationFrame(frame)
    cameraOff()
    landmarker.close()
  }
  const tick = () => {
    if (stopped) return
    // Scheduled first, so a throw from onFrame can't end tracking.
    frame = requestAnimationFrame(tick)
    // Only a new camera frame is worth reading: the screen redraws faster than most webcams film.
    if (video.currentTime !== lastFrameTime) {
      lastFrameTime = video.currentTime
      const now = performance.now()
      let hands: Hand[] | null = null
      try {
        hands = landmarker.detectForVideo(video, now).landmarks
        failedFrames = 0
      } catch {
        // A frame the tracker can't read is skipped, but a tracker that keeps failing is reported.
        failedFrames += 1
        if (failedFrames >= MAX_FAILED_FRAMES) {
          stop()
          onFail?.('failed')
          return
        }
      }
      if (hands) onFrame(hands, now)
    }
  }
  frame = requestAnimationFrame(tick)
  return stop
}
