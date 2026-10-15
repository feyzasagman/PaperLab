import { expect, test } from './fixtures'
import { fakeCamera, handsToggle, open3d } from './hands'

// Parallel-safe (D51): its own workspace and two linked papers. The camera is a fake that plays a pointing hand,
// fingertip at the centre; the view is brought to one paper first so it sits at the centre.
test.use(fakeCamera('point.mjpeg'))
test.use({ reducedMotion: 'reduce' })

test.describe('hands mode: pointing', () => {
  test('holding a pointing finger still on a paper opens it, and turning Hands mode off turns the camera off', async ({
    page,
    request,
    paperId,
    secondPaperId,
    workspaceId,
    workspaceName,
  }) => {
    // Every camera stream the page opens, to check each one ends.
    await page.addInitScript(() => {
      const open = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
      const streams: MediaStream[] = []
      Object.assign(window, { cameraStreams: streams })
      navigator.mediaDevices.getUserMedia = async (constraints) => {
        const stream = await open(constraints)
        streams.push(stream)
        return stream
      }
    })
    const title = `Hands ${workspaceName.slice(-8)}`
    expect((await request.patch(`/api/papers/${secondPaperId}`, { data: { title } })).status()).toBe(200)
    const view = await open3d(page, request, [paperId, secondPaperId], true, workspaceId, workspaceName)
    test.skip(view === null, 'this browser has no WebGL 2')
    await page.locator(`.graph-paper[data-paper-id="${secondPaperId}"]`).click()
    await page.waitForTimeout(1200) // the 900 ms fly to it
    await page.getByRole('region', { name: 'Connected papers' }).getByRole('button', { name: 'Clear focus' }).click()
    await expect(page.getByRole('region', { name: 'Connected papers' })).toHaveCount(0)

    await handsToggle(page).click()
    await expect(handsToggle(page)).toHaveAttribute('aria-pressed', 'true')
    await expect(view!.getByRole('status')).toHaveText('Pointing: hold still on a paper to open it', { timeout: 30_000 })
    await expect(page.locator('[data-hand-pointer]')).toContainText(title)
    // A one-second hold opens the paper, as a click would: the side panel shows it.
    const panel = page.getByRole('region', { name: 'Connected papers' })
    await expect(panel.getByText(title, { exact: true })).toBeVisible({ timeout: 10_000 })

    await handsToggle(page).click()
    await expect(view!.locator('video')).toHaveCount(0)
    const states = await page.evaluate(() =>
      (window as unknown as { cameraStreams: MediaStream[] }).cameraStreams.flatMap((s) => s.getTracks()).map((t) => t.readyState),
    )
    expect(states.length).toBeGreaterThan(0)
    expect(states.every((state) => state === 'ended')).toBe(true)
  })

  test('a blocked camera says how to allow it', async ({ page, request, paperId, secondPaperId, workspaceId, workspaceName }) => {
    await page.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Permission denied', 'NotAllowedError'))
    })
    const view = await open3d(page, request, [paperId, secondPaperId], true, workspaceId, workspaceName)
    test.skip(view === null, 'this browser has no WebGL 2')
    await handsToggle(page).click()
    await expect(view!.getByRole('status')).toHaveText(
      'Camera access is blocked. Allow PaperLab to use the camera in your system settings, then turn Hands mode on again.',
      { timeout: 30_000 },
    )
  })
})
