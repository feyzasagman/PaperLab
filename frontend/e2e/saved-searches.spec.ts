import { expect, test } from './fixtures'

test('save a search on a schedule, pause it, run it now, then delete it', async ({ page, request, workspaceId }) => {
  const query = 'e2e saved query'
  await page.goto(`/#/workspaces/${workspaceId}?tab=search`)
  const saved = page.getByRole('region', { name: 'Saved searches' })

  await saved.getByRole('button', { name: 'Save this search…' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Query').fill(query)
  await dialog.getByLabel('Schedule').click()
  await page.getByRole('option', { name: 'Daily' }).click()
  await dialog.getByLabel('Time zone').fill('UTC')
  await dialog.getByLabel('Time', { exact: true }).fill('02:00')
  await dialog.getByRole('button', { name: 'Save', exact: true }).click()

  await expect(saved.getByText('Daily at 02:00 · UTC')).toBeVisible()
  await expect(saved.getByText(/Next run:/)).toBeVisible()

  const toggle = saved.getByRole('switch', { name: `Enabled: ${query}` })
  await toggle.click()
  await expect(saved.getByText('Paused')).toBeVisible()

  const [run] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/run') && r.request().method() === 'POST'),
    saved.getByRole('button', { name: 'Run now' }).click(),
  ])
  expect(run.status()).toBeLessThan(300)
  const { id: runId } = await run.json()
  expect(runId).toBeTruthy()
  // A real search run is going: stop it right away so none is left behind (the workspace delete also ends it).
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible()
  const stopped = await request.post(`/api/workspaces/${workspaceId}/search/runs/${runId}/stop`)
  expect(stopped.ok()).toBeTruthy()

  await saved.getByRole('button', { name: 'Delete' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click()
  await expect(saved.getByText(query)).toHaveCount(0)
})
