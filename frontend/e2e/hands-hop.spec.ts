import { expect, test } from './fixtures'
import { fakeCamera, handsToggle, open3d } from './hands'

// Parallel-safe (D51). The fake camera plays a two-finger hand sweeping sideways, looping.
test.use(fakeCamera('two-flick.mjpeg'))
test.use({ reducedMotion: 'reduce' })

test('a two-finger flick in hands mode steps to a linked paper', async ({
  page,
  request,
  paperId,
  secondPaperId,
  tablePaperId,
  workspaceId,
  workspaceName,
}) => {
  const run = workspaceName.slice(-8)
  const alpha = `Alpha ${run}`
  expect((await request.patch(`/api/papers/${paperId}`, { data: { title: alpha } })).status()).toBe(200)
  // Links belong to papers, not workspaces: this second link from Alpha is drawn before the page loads, and open3d
  // puts all three papers in the workspace and draws Alpha → the second paper.
  const extra = await request.post('/api/links', { data: { from_paper: paperId, to_paper: tablePaperId, label: 'extends' } })
  expect(extra.status()).toBe(201)
  const view = await open3d(page, request, [paperId, secondPaperId, tablePaperId], true, workspaceId, workspaceName)
  test.skip(view === null, 'this browser has no WebGL 2')
  await page.locator(`.graph-paper[data-paper-id="${paperId}"]`).click()

  await handsToggle(page).click()
  const status = page.getByRole('group', { name: '3D graph' }).getByRole('status')
  await expect(status).toHaveText('Two fingers: flick left or right to go to the previous or next linked paper', { timeout: 30_000 })
  const panel = page.getByRole('region', { name: 'Connected papers' })
  await expect(panel.getByText(new RegExp(`^Linked to ${alpha}: [12] of 2$`))).toBeVisible({ timeout: 15_000 })
})
