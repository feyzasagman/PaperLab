import { expect, test } from './fixtures'
import { fakeCamera, handsToggle, open3d } from './hands'

// Parallel-safe (D51). The fake camera plays two open hands moving apart, looping; the loop's jump back is ignored.
test.use(fakeCamera('two-hands-apart.mjpeg'))
test.use({ reducedMotion: 'reduce' })

test('two open hands moving apart zoom the 3D view in', async ({ page, request, paperId, secondPaperId, workspaceId, workspaceName }) => {
  const view = await open3d(page, request, [paperId, secondPaperId], true, workspaceId, workspaceName)
  test.skip(view === null, 'this browser has no WebGL 2')
  const distance = async () => Number(await view!.getAttribute('data-camera-distance'))
  const before = await distance()
  await handsToggle(page).click()
  await expect(view!.getByRole('status')).toHaveText('Two open hands: move them apart to zoom in, together to zoom out', {
    timeout: 30_000,
  })
  await expect.poll(distance, { timeout: 15_000 }).toBeLessThan(before * 0.9)
})
