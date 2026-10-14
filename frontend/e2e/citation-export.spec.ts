import { readFile } from 'node:fs/promises'
import { expect, test } from './fixtures'

// Fake stack: a fixture paper in a workspace. BibTeX arrives as a download; APA 7 opens a preview with the title.
test('Export citations downloads BibTeX and previews APA 7 with the paper title', async ({
  page, request, workspaceId, paperId,
}) => {
  expect((await request.put(`/api/workspaces/${workspaceId}/papers/${paperId}`)).status()).toBe(204)
  const title = ((await (await request.get(`/api/papers/${paperId}`)).json()) as { title: string }).title
  await page.goto(`/#/workspaces/${workspaceId}`)

  await page.getByRole('button', { name: 'Export citations' }).click()
  const download = page.waitForEvent('download')
  await page.getByRole('menuitem', { name: 'BibTeX' }).click()
  const file = await download
  expect(file.suggestedFilename()).toMatch(/\.bib$/)
  const text = await readFile((await file.path())!, 'utf8')
  expect(text).toContain('@')
  expect(text).toContain(title)

  await page.getByRole('button', { name: 'Export citations' }).click()
  await page.getByRole('menuitem', { name: 'APA 7…' }).click()
  await expect(page.getByRole('dialog').getByText(title)).toBeVisible()
})
