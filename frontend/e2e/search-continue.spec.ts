import { expect, test } from './fixtures'

// arxiv only: the fake stack has no fixtures for bioRxiv/medRxiv.
test('Continue on a stopped run restarts it', async ({ page, request, workspaceId }) => {
  const started = await request.post(`/api/workspaces/${workspaceId}/search/runs`, { data: { query: 'bert', sources: ['arxiv'] } })
  expect(started.status()).toBe(201)
  const { id } = await started.json()
  await request.post(`/api/workspaces/${workspaceId}/search/runs/${id}/stop`)

  await page.goto(`/#/workspaces/${workspaceId}?tab=search&run=${id}`)
  const cont = page.getByRole('button', { name: 'Continue: fetch the next results from 1 source' })
  await expect(cont).toBeVisible()
  await cont.click()
  await expect(page.getByText(/^stopped ·/)).toBeHidden()
  await expect(cont).toBeHidden()
})
