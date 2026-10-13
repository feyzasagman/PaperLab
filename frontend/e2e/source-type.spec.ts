import { FIXTURE_TITLE, expect, openReader, test } from './fixtures'

// The published-preprint banner is covered in vitest: published_doi is set only by enrichment, so it can't be seeded here.

test('a fixture paper shows its evidence chip in the library and the reader, and a source type set in the dialog sticks', async ({
  page,
  request,
  paperId,
}) => {
  await page.goto('/')
  const row = page.locator('.paper-row').filter({ has: page.locator(`a[href="#/papers/${paperId}"]`) })
  await expect(row.locator('.evidence-access')).toHaveText('Full text')

  const line = await openReader(page, paperId)
  await expect(page.getByRole('heading', { name: FIXTURE_TITLE })).toBeVisible()
  await expect(page.locator('header .evidence-access')).toHaveText('Full text')
  await expect(page.locator('header .source-type')).toHaveCount(0)

  await page.getByRole('button', { name: 'Edit details' }).click()
  const dialog = page.getByRole('dialog', { name: 'Edit details' })
  await dialog.getByLabel('Source type').selectOption('preprint')
  await dialog.getByLabel('Peer review').selectOption('false')
  await dialog.getByRole('button', { name: 'Save' }).click()
  await expect(dialog).toBeHidden()
  await expect(page.locator('header .source-type')).toHaveText('Preprint')
  await expect(page.locator('header .peer-review')).toHaveText('Preprint · not peer-reviewed')

  await page.reload()
  await expect(line).toBeVisible()
  await expect(page.locator('header .source-type')).toHaveText('Preprint')
  const saved = await (await request.get(`/api/papers/${paperId}`)).json()
  expect(saved).toMatchObject({ source_type: 'preprint', peer_reviewed: 'false' })
})
