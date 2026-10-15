import { expect, test } from './fixtures'
import { fakeCamera, handsToggle, open3d } from './hands'

// Parallel-safe (D51). The fake camera plays an open hand sweeping across the picture, looping.
test.use(fakeCamera('palm-sweep.mjpeg'))
test.use({ reducedMotion: 'reduce' })

test('an open hand moving sideways turns the 3D view', async ({ page, request, paperId, secondPaperId, workspaceId, workspaceName }) => {
  const view = await open3d(page, request, [paperId, secondPaperId], true, workspaceId, workspaceName)
  test.skip(view === null, 'this browser has no WebGL 2')
  const angle = async () => Number(await view!.getAttribute('data-camera-azimuth'))
  const before = await angle()
  await handsToggle(page).click()
  await expect(view!.getByRole('status')).toHaveText('Open hand: move it to turn the view', { timeout: 30_000 })
  const turned = async () => Math.abs((((await angle()) - before + 540) % 360) - 180)
  await expect.poll(turned, { timeout: 15_000 }).toBeGreaterThan(10)
})
