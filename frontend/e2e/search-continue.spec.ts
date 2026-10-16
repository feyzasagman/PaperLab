import { expect, test } from './fixtures'

// arxiv only: the fake stack has no fixtures for bioRxiv/medRxiv.
test('Continue on a stopped run restarts it', async ({ page, request, workspaceId }) => {
  const base = `/api/workspaces/${workspaceId}/search/runs`
  const started = await request.post(base, { data: { query: 'bert', sources: ['arxiv'] } })
  expect(started.status()).toBe(201)
  const { id } = await started.json()
  const stopped = await request.post(`${base}/${id}/stop`)
  expect([200, 409]).toContain(stopped.status()) // 409 = search_run_not_running
  const get = async () => (await request.get(`${base}/${id}`)).json()
  await expect.poll(async () => (await get()).status, { timeout: 30_000 }).not.toBe('running')
  test.skip(!(await get()).can_continue, 'the fake source finished before stop')

  await page.goto(`/#/workspaces/${workspaceId}?tab=search&run=${id}`)
  const cont = page.getByRole('button', { name: 'Continue: fetch the next results from 1 source' })
  await expect(cont).toBeVisible()
  await cont.click()
  await expect(page.getByText(/^(running|exhausted) ·/)).toBeVisible()
  await expect(cont).toBeHidden()
})
