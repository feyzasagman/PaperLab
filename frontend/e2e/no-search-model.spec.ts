import { expect, openReader, pickChatModel, saveNoteOn, test } from './fixtures'

// Needs the stack started with no search model, and runs as its own project (README, End-to-end tests):
//   scripts/e2e-stack.sh up --no-search-model
//   npx playwright test --project=no-search-model --no-deps
// On a stack that has the model it skips itself, so a plain `npm run e2e` stays green there. The download itself is
// never run: POST /api/embedding/model is answered by the test (spec §8.7).

const DOWNLOAD = /^Download search model · \d+ MB$/
const event = (name: string, data: object) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`

test.describe('with no search model @no-search-model', () => {
  test.beforeEach(async ({ request }) => {
    const status = await (await request.get('/api/embedding')).json()
    // Built-in only (spec D159): with another source there is no download to offer.
    test.skip(
      status.model_present || status.source.kind !== 'builtin',
      'needs Built-in without its model: start the stack with MODELS_DIR=/models/none, on Built-in',
    )
  })

  test('a PDF uploads to ready, and can be read, highlighted and noted', async ({ page, request, paperId }) => {
    expect((await (await request.get(`/api/papers/${paperId}`)).json()).status).toBe('ready')
    const chunks = await (await request.get(`/api/papers/${paperId}/chunks?page=1`)).json()
    expect(chunks.length).toBeGreaterThan(0) // chunked, only not embedded

    const line = await openReader(page, paperId)
    const card = await saveNoteOn(page, line, 'Noted with no search model.')

    await expect(card).toContainText('Highlights are the anchor')
    await expect(page.locator('.highlight:not(.draft)').first()).toBeVisible()
  })

  test("the library says search isn't set up, and a long paper's chat offers the download", async ({
    page,
    longPaperId,
    llmConnection,
  }) => {
    await pickChatModel(page, llmConnection.modelId)
    await page.goto('/#/')
    const notice = page.locator('.search-notice')
    await expect(notice).toContainText("Search isn't set up: long papers and workspaces can't be searched yet.")
    await expect(notice.getByRole('button', { name: DOWNLOAD })).toBeVisible()

    await page.goto(`/#/papers/${longPaperId}?tab=chat`)
    await page.getByRole('textbox', { name: 'Question' }).fill('What does this paper say about retrieval?')
    await page.getByRole('textbox', { name: 'Question' }).press('Enter')

    const refused = page.locator('.chat-error')
    await expect(refused).toContainText("Search isn't set up, so this can't be searched yet.")
    await expect(page.getByRole('button', { name: DOWNLOAD })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Use another search source' })).toHaveAttribute('href', '#/settings/search')
    await expect(refused.getByRole('button', { name: 'Retry' })).toHaveCount(0)
  })

  test('Settings → Search shows the status, and the download runs in place', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))
    page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
    let release = () => {}
    const held = new Promise<void>((resolve) => (release = resolve))
    await page.route('**/api/embedding/model', async (route) => {
      await held // the download "runs" until the test lets it finish
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body:
          event('progress', { file: 'onnx/model_int8.onnx', completed: 58_000_000, total: 138_007_688 }) +
          event('done', { papers_queued: 1 }),
      })
    })
    await page.goto('/#/settings/search')
    const section = page.getByRole('region', { name: 'Search' })
    const status = section.locator('.search-model-status')
    await expect(status).toHaveText('Search model: not downloaded')
    await expect(section.getByRole('button', { name: 'Re-index library' })).toBeDisabled() // nothing to embed with yet

    await section.getByRole('button', { name: DOWNLOAD }).click()
    await expect(status).toHaveText('Search model: downloading… 0%')
    await expect(section.getByRole('progressbar', { name: 'Downloading the search model' })).toBeVisible()
    // The 3D moment sits above the bar when this browser can draw it; the bar is there either way.
    const webgl = await page.evaluate(() => document.createElement('canvas').getContext('webgl2') !== null)
    const canvas = section.locator('canvas[aria-hidden="true"]')
    if (webgl) await expect(canvas).toBeVisible() // hidden until the director runs, so this proves it drew
    else await expect(canvas).toHaveCount(0)
    release()

    await expect(status).toHaveText('Search model: ready')
    await expect(section.getByRole('button', { name: DOWNLOAD })).toHaveCount(0)
    expect(errors).toEqual([])
  })

  test.describe('with reduced motion', () => {
    // test.use keeps the project's baseURL; a hand-made browser.newContext() would not.
    test.use({ reducedMotion: 'reduce' })
    test('Settings → Search has no 3D, only the download controls', async ({ page }) => {
      await page.goto('/#/settings/search')
      const section = page.getByRole('region', { name: 'Search' })
      await expect(section.getByRole('button', { name: DOWNLOAD })).toBeVisible()
      await expect(section.locator('canvas')).toHaveCount(0)
    })
  })
})
