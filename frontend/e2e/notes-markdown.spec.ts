import { readFile } from 'node:fs/promises'
import { expect, test } from './fixtures'

// Fake stack: a fixture paper in a workspace. Export to Markdown downloads a zip holding that paper's note file.
test('Export to Markdown downloads a zip with a file for the paper', async ({ page, request, workspaceId, paperId }) => {
  expect((await request.put(`/api/workspaces/${workspaceId}/papers/${paperId}`)).status()).toBe(204)
  await page.goto(`/#/workspaces/${workspaceId}`)
  await page.getByRole('tab', { name: 'Notes' }).click()

  const download = page.waitForEvent('download')
  await page.getByRole('link', { name: 'Export to Markdown' }).click()
  const file = await download
  expect(file.suggestedFilename()).toMatch(/\.zip$/)
  const bytes = await readFile((await file.path())!)
  expect(bytes.subarray(0, 2).toString()).toBe('PK') // a zip
  // File names are stored uncompressed in a zip's headers: the paper's note file and the index are in it.
  expect(bytes.toString('latin1')).toContain('.md')
})
