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

import { problemOf, startTracker, TrackerError } from './handTracker'

const stream = { getTracks: () => [{ stop: mocks.stopTrack }] } as unknown as MediaStream
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
  expect(mocks.stopTrack).toHaveBeenCalled()
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
  const failure = await startTracker(video(), vi.fn()).catch((error: unknown) => error)
  expect((failure as TrackerError).problem).toBe('failed')
  expect(mocks.stopTrack).toHaveBeenCalled()
})

it('says there is no camera when the browser offers none', async () => {
  Object.defineProperty(navigator, 'mediaDevices', { value: undefined, configurable: true })
  const failure = await startTracker(video(), vi.fn()).catch((error: unknown) => error)
  expect((failure as TrackerError).problem).toBe('no-camera')
})
