// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  createFromOptions: vi.fn(),
  detectForVideo: vi.fn(() => ({ landmarks: [[{ x: 0.5, y: 0.5 }]] })),
  close: vi.fn(),
  getUserMedia: vi.fn(),
  stopTrack: vi.fn(),
}))
vi.mock('@mediapipe/tasks-vision', () => ({ HandLandmarker: { createFromOptions: mocks.createFromOptions } }))

// By path: the package's exports map has no subpath for the bundle text.
import bundle from '../../../../node_modules/@mediapipe/tasks-vision/vision_bundle.mjs?raw'
import { problemOf, startTracker, TrackerError } from './handTracker'

const stream = { getTracks: () => [{ stop: mocks.stopTrack }, { stop: mocks.stopTrack }] } as unknown as MediaStream
const video = () => document.createElement('video')

beforeEach(() => {
  vi.clearAllMocks()
  mocks.createFromOptions.mockResolvedValue({ detectForVideo: mocks.detectForVideo, close: mocks.close })
  mocks.getUserMedia.mockResolvedValue(stream)
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia: mocks.getUserMedia }, configurable: true })
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
  vi.stubGlobal('requestAnimationFrame', (run: FrameRequestCallback) => setTimeout(() => run(0), 0) as unknown as number)
  vi.stubGlobal('cancelAnimationFrame', (id: number) => clearTimeout(id))
})

it('says what a camera failure means for the owner', () => {
  const failure = (name: string) => new DOMException('x', name)
  expect(problemOf(failure('NotAllowedError'))).toBe('denied')
  expect(problemOf(failure('SecurityError'))).toBe('denied')
  expect(problemOf(failure('NotFoundError'))).toBe('no-camera')
  expect(problemOf(failure('OverconstrainedError'))).toBe('no-camera')
  expect(problemOf(failure('NotReadableError'))).toBe('busy')
  expect(problemOf(new Error('odd'))).toBe('failed')
})

it('reads the hands on a new camera frame, then stops the camera and the tracker', async () => {
  const onFrame = vi.fn()
  const element = video()
  const stop = await startTracker(element, onFrame)
  await vi.waitFor(() => expect(onFrame).toHaveBeenCalledWith([[{ x: 0.5, y: 0.5 }]], expect.any(Number)))
  expect(element.srcObject).toBe(stream)
  // The tracker runs in the page, in video mode, on the bundled model, looking for up to two hands.
  expect(mocks.createFromOptions.mock.calls[0][1]).toMatchObject({
    baseOptions: { modelAssetPath: '/models/hand_landmarker.task', delegate: 'GPU' },
    runningMode: 'VIDEO',
    numHands: 2,
  })
  stop()
  expect(mocks.stopTrack).toHaveBeenCalledTimes(2) // every track
  expect(mocks.close).toHaveBeenCalled()
  expect(element.srcObject).toBeNull()
  const calls = onFrame.mock.calls.length
  await new Promise((resolve) => setTimeout(resolve, 20))
  expect(onFrame.mock.calls.length).toBe(calls) // no frames after stop
})

it('falls back to the CPU when the GPU tracker cannot start', async () => {
  mocks.createFromOptions.mockRejectedValueOnce(new Error('no GPU'))
  const stop = await startTracker(video(), vi.fn())
  expect(mocks.createFromOptions).toHaveBeenCalledTimes(2)
  expect(mocks.createFromOptions.mock.calls[1][1]).toMatchObject({ baseOptions: { delegate: 'CPU' } })
  stop()
})

it('rejects with the reason when the camera is blocked, and never loads the tracker', async () => {
  mocks.getUserMedia.mockRejectedValueOnce(new DOMException('blocked', 'NotAllowedError'))
  const failure = await startTracker(video(), vi.fn()).catch((error: unknown) => error)
  expect(failure).toBeInstanceOf(TrackerError)
  expect((failure as TrackerError).problem).toBe('denied')
  expect(mocks.createFromOptions).not.toHaveBeenCalled()
})

it('turns the camera off again when the tracker cannot load', async () => {
  mocks.createFromOptions.mockRejectedValue(new Error('broken'))
  const element = video()
  const failure = await startTracker(element, vi.fn()).catch((error: unknown) => error)
  expect((failure as TrackerError).problem).toBe('failed')
  expect(mocks.stopTrack).toHaveBeenCalledTimes(2)
  expect(element.srcObject).toBeNull()
})

it('says there is no camera when the browser offers none', async () => {
  Object.defineProperty(navigator, 'mediaDevices', { value: undefined, configurable: true })
  const failure = await startTracker(video(), vi.fn()).catch((error: unknown) => error)
  expect((failure as TrackerError).problem).toBe('no-camera')
})

it('uses a hand tracker build that sends nothing to any server', () => {
  expect(bundle).not.toMatch(/https?:\/\/(?!www\.w3\.org)/)
})

it('turns the camera off and fails when the video will not play', async () => {
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockRejectedValue(new Error('no play'))
  const element = video()
  const failure = await startTracker(element, vi.fn()).catch((error: unknown) => error)
  expect((failure as TrackerError).problem).toBe('failed')
  expect(mocks.stopTrack).toHaveBeenCalledTimes(2)
  expect(element.srcObject).toBeNull()
})

it('loads the wasm from the bundled files', async () => {
  const stop = await startTracker(video(), vi.fn())
  const files = mocks.createFromOptions.mock.calls[0][0]
  expect(files.wasmLoaderPath).toMatch(/vision_wasm_internal\.js/)
  expect(files.wasmBinaryPath).toMatch(/vision_wasm_internal\.wasm/)
  stop()
})

it('reads a frame once per new camera frame', async () => {
  let t = 0
  const element = video()
  Object.defineProperty(element, 'currentTime', { get: () => t, configurable: true })
  const onFrame = vi.fn()
  const stop = await startTracker(element, onFrame)
  await vi.waitFor(() => expect(onFrame).toHaveBeenCalledTimes(1))
  await new Promise((resolve) => setTimeout(resolve, 20))
  expect(onFrame).toHaveBeenCalledTimes(1) // same camera frame
  t = 1
  await vi.waitFor(() => expect(onFrame).toHaveBeenCalledTimes(2))
  stop()
})

it('gives up and says so after 30 unreadable frames in a row', async () => {
  mocks.detectForVideo.mockImplementation(() => {
    throw new Error('bad frame')
  })
  let t = 0
  const element = video()
  Object.defineProperty(element, 'currentTime', { get: () => ++t, configurable: true })
  const onFail = vi.fn()
  await startTracker(element, vi.fn(), onFail)
  await vi.waitFor(() => expect(onFail).toHaveBeenCalledWith('failed'))
  expect(mocks.detectForVideo).toHaveBeenCalledTimes(30)
  expect(mocks.stopTrack).toHaveBeenCalledTimes(2)
  expect(mocks.close).toHaveBeenCalledTimes(1)
  mocks.detectForVideo.mockImplementation(() => ({ landmarks: [[{ x: 0.5, y: 0.5 }]] }))
})

it('stops only once however often stop is called', async () => {
  const stop = await startTracker(video(), vi.fn())
  stop()
  stop()
  expect(mocks.close).toHaveBeenCalledTimes(1)
})

it('leaves a second start on the same element running when the first one stops', async () => {
  const stopB = vi.fn()
  const streamB = { getTracks: () => [{ stop: stopB }] } as unknown as MediaStream
  const element = video()
  const stopA = await startTracker(element, vi.fn())
  mocks.getUserMedia.mockResolvedValueOnce(streamB)
  await startTracker(element, vi.fn())
  stopA()
  expect(element.srcObject).toBe(streamB)
  expect(stopB).not.toHaveBeenCalled()
})

it('keeps tracking after the frame callback throws', async () => {
  // The browser reports a throw in a frame callback and carries on; this stub does the same, so the test sees it.
  const thrown: unknown[] = []
  vi.stubGlobal('requestAnimationFrame', (run: FrameRequestCallback) =>
    setTimeout(() => {
      try {
        run(0)
      } catch (error) {
        thrown.push(error)
      }
    }, 0) as unknown as number,
  )
  let t = 0
  const element = video()
  Object.defineProperty(element, 'currentTime', { get: () => ++t, configurable: true })
  const onFrame = vi.fn().mockImplementationOnce(() => {
    throw new Error('boom')
  })
  const stop = await startTracker(element, onFrame)
  await vi.waitFor(() => expect(onFrame.mock.calls.length).toBeGreaterThan(1))
  expect(thrown).toHaveLength(1)
  stop()
})
