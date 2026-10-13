import { expect, test } from './fixtures'

// Fake stack: the model answers with the first passage's opening words, copied exactly, so every card has a
// verified quote and a page.
test('Generate makes a card with a page, an edit shows AI · edited, and Export CSV downloads', async ({
  page, request, workspaceId, paperId,
}) => {
  expect((await request.put(`/api/workspaces/${workspaceId}/papers/${paperId}`)).status()).toBe(204)
  await page.goto(`/#/workspaces/${workspaceId}?tab=evidence`)

  await expect(page.getByText('Not generated')).toBeVisible()
  await page.getByRole('button', { name: 'Generate cards' }).click()

  const design = page.getByRole('button', { name: /^Edit Design for / })
  await expect(design).toBeVisible({ timeout: 60_000 })
  await expect(design).toContainText(/p\. \d+/)
  await expect(design).toContainText('AI')
  await expect(design).not.toContainText('edited')

  await design.click()
  const popover = page.getByRole('dialog')
  await expect(popover.locator('blockquote')).not.toBeEmpty()
  await popover.getByRole('textbox').fill('My own reading of the design')
  await popover.getByRole('button', { name: 'Save' }).click()
  await expect(page.getByRole('button', { name: /^Edit Design for / })).toContainText('My own reading of the design')
  await expect(page.getByRole('button', { name: /^Edit Design for / })).toContainText('AI · edited')

  const download = page.waitForEvent('download')
  await page.getByRole('link', { name: 'Export CSV' }).click()
  expect((await download).suggestedFilename()).toBe('evidence-cards.csv')
})
