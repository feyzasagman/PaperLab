import type { APIRequestContext, Page } from '@playwright/test'
import { expect, test } from './fixtures'

// The fake model rates by a stable hash of the title: fixture 1 include (4), 2 exclude (2), 3 unsure (3).
async function searchAndWait(page: Page, request: APIRequestContext, workspaceId: string) {
  // arxiv only: the fake stack has no fixtures for bioRxiv/medRxiv, so a run over every source never exhausts.
  const started = await request.post(`/api/workspaces/${workspaceId}/search/runs`, {
    data: { query: 'bert', sources: ['arxiv'] },
  })
  expect(started.status()).toBe(201)
  const { id: runId } = await started.json()
  await expect
    .poll(async () => (await (await request.get(`/api/workspaces/${workspaceId}/search/runs/${runId}`)).json()).status, {
      timeout: 60_000,
    })
    .toBe('exhausted')
  await page.goto(`/#/workspaces/${workspaceId}?tab=search`)
  await expect(page.getByText(/[1-9]\d* in pool/)).toBeVisible()
}

// The assist panel (three criteria fields and the settings) is tall: at 720px high the hit list gets no height.
test.use({ viewport: { width: 1280, height: 1500 } })

function row(page: Page, title: string) {
  return page.locator('[data-slot="context-menu-trigger"]', { hasText: new RegExp(title, 'i') })
}

test('AI pre-screening scores hits, shows why, and Accept makes the decision', async ({ page, request, workspaceId }) => {
  await searchAndWait(page, request, workspaceId)
  await page.getByLabel('Research question').fill('How is pagination tested?')
  await page.getByLabel('Include when…').fill('Studies of pagination')
  await page.getByRole('button', { name: 'Save criteria' }).click()
  page.on('dialog', (dialog) => dialog.accept())
  await page.getByRole('button', { name: 'Suggest for unscreened hits' }).click()
  await expect(page.getByText('Suggestions stopped')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Suggest for unscreened hits' })).toBeVisible({ timeout: 30_000 })

  await page.getByLabel('Sort hits').selectOption('ai')
  const first = row(page, 'paperlab pagination fixture 1')
  const second = row(page, 'paperlab pagination fixture 2')
  await expect(page.locator('[data-slot="context-menu-trigger"]').first()).toContainText(/fixture 1/i)
  await expect(first.getByText('Score 4/5')).toBeVisible()
  await expect(first.getByText('Suggests: include')).toBeVisible()

  await first.click()
  await page.getByRole('button', { name: 'Why' }).click()
  await expect(page.getByLabel('Hit preview').getByText(/“.+”/)).toBeVisible()

  await first.getByRole('button', { name: /^Accept:/ }).click()
  await expect(first.getByText('relevant', { exact: true })).toBeVisible()

  await page.getByRole('checkbox', { name: /^Select paperlab pagination fixture 2/i }).check()
  await page.getByRole('button', { name: 'Accept AI suggestions' }).click()
  await expect(second.getByText('not_relevant', { exact: true })).toBeVisible()
})
