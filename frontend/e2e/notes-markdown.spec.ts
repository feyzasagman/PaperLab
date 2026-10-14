import { execFileSync } from 'node:child_process'
import { addNote, expect, test } from './fixtures'

// Fake stack: a fixture paper with an anchored note. Export to Markdown downloads a zip whose `<key>.md` holds the
// quote as a blockquote and a paperlab:// link back to the paper.
test('Export to Markdown downloads a zip with the note quote in the paper file', async ({
  page, request, workspaceId, paperId,
}) => {
  expect((await request.put(`/api/workspaces/${workspaceId}/papers/${paperId}`)).status()).toBe(204)
  const note = await addNote(request, paperId, 1, 'markdown export note')
  try {
    await page.goto(`/#/workspaces/${workspaceId}`)
    await page.getByRole('tab', { name: 'Notes' }).click()

    const download = page.waitForEvent('download')
    await page.getByRole('link', { name: 'Export to Markdown' }).click()
    const file = await download
    expect(file.suggestedFilename()).toMatch(/\.zip$/)
    const zip = (await file.path())!

    // Entries are deflated, so read them with unzip rather than matching raw bytes.
    const entries = execFileSync('unzip', ['-Z1', zip], { encoding: 'utf8' }).split('\n').filter(Boolean)
    const entry = entries.find((name) => /^[^/]+\/[^/]+\.md$/.test(name) && !name.endsWith('/index.md'))
    expect(entry, `a <folder>/<key>.md entry in ${entries.join(', ')}`).toBeTruthy()
    const text = execFileSync('unzip', ['-p', zip, entry!], { encoding: 'utf8' })
    expect(text).toContain('> Quote for markdown export note')
    expect(text).toContain(`paperlab://papers/${paperId}`)
  } finally {
    await request.delete(`/api/notes/${note.id}`)
  }
})
