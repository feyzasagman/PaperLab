import type { APIRequestContext } from '@playwright/test'
import { expect, test } from './fixtures'

// arxiv only: the fake stack has no fixtures for bioRxiv/medRxiv, so a run over every source never exhausts.
async function runAndWait(request: APIRequestContext, workspaceId: string, query: string): Promise<string> {
  const started = await request.post(`/api/workspaces/${workspaceId}/search/runs`, { data: { query, sources: ['arxiv'] } })
  expect(started.status()).toBe(201)
  const { id } = await started.json()
  await expect
    .poll(async () => (await (await request.get(`/api/workspaces/${workspaceId}/search/runs/${id}`)).json()).status, {
      timeout: 60_000,
    })
    .toBe('exhausted')
  return id
}

test('the Runs list shows past runs and opens the one clicked', async ({ page, request, workspaceId }) => {
  const olderId = await runAndWait(request, workspaceId, 'bert')
  const newerId = await runAndWait(request, workspaceId, 'bert attention')

  await page.goto(`/#/workspaces/${workspaceId}?tab=search&run=${newerId}`)
  await page.getByRole('button', { name: 'Runs (2)' }).click()
  const runs = page.getByRole('region', { name: 'Runs' })
  await expect(runs.getByRole('button', { name: /bert attention/ })).toHaveAttribute('aria-current', 'true')

  await runs.getByRole('button', { name: /^bert\b(?! attention)/ }).click()
  await expect(page).toHaveURL(new RegExp(`run=${olderId}`))
  await expect(runs.getByRole('button', { name: /^bert\b(?! attention)/ })).toHaveAttribute('aria-current', 'true')
})
