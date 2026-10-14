import type { APIRequestContext, Locator, Page } from '@playwright/test'
import { expect, openReader, removePaperAndNotes, test } from './fixtures'
import { FREE_SOURCES_ON, MOVES_PAPER_SOURCES, readSourceSettings, setSourceSettings, type SourceSettings } from './paperSources'

// The API runs with DISCOVERY_PROVIDER=fake (backend/app/providers/discovery_fake.py): with every source but OpenAlex
// on, every search and every suggestion answers these three papers, and nothing reaches a real paper source.
const FREE = 'PaperLab Find Papers Fixture'
const LANDING = 'PaperLab Landing Page Fixture'
const CLOSED = 'PaperLab Closed Access Fixture'
const FAKE_DOI_PREFIX = '10.5555/paperlab-e2e-'
const QUERY_LABEL = 'Title, DOI, arXiv ID or OpenAlex ID'
const NO_SOURCE = 'No paper source that can look this up is on. Turn one on in Settings → Paper sources.'
// Every test here turns sources on or off: it runs after the parallel specs, one at a time (playwright.config.ts).
const tag = MOVES_PAPER_SOURCES

// A paper's DOI is unique and the fake's are fixed, so this file's tests take turns; no other spec adds them.
test.describe.configure({ mode: 'serial' })

async function removeFoundPapers(request: APIRequestContext) {
  const papers = await request.get('/api/papers')
  for (const paper of papers.ok() ? await papers.json() : []) {
    if (paper.doi?.startsWith(FAKE_DOI_PREFIX)) await removePaperAndNotes(request, paper.id)
  }
}

// The owner's switches and email, put back after each test (D76).
let owners: SourceSettings

test.beforeEach(async ({ request }) => {
  await removeFoundPapers(request) // a killed run's leftovers would show as "In library"
  owners = await readSourceSettings(request)
  await setSourceSettings(request, { enabled: FREE_SOURCES_ON })
  const found = await request.get('/api/discovery/search?q=fixture')
  const titles = found.ok() ? (await found.json()).results.map((c: { title: string }) => c.title) : []
  const hint = 'start the test stack with scripts/e2e-stack.sh up'
  expect(titles, hint).toEqual([FREE, LANDING, CLOSED])
})

test.afterEach(async ({ request }) => {
  await removeFoundPapers(request)
  await setSourceSettings(request, owners)
})

const rowOf = (scope: Locator, title: string) => scope.locator('.candidate-row').filter({ hasText: title })

async function searchFromHere(page: Page): Promise<Locator> {
  await page.getByRole('button', { name: 'Find papers' }).click()
  const dialog = page.getByRole('dialog', { name: 'Find papers' })
  await dialog.getByRole('textbox', { name: QUERY_LABEL }).fill('fixture')
  await dialog.getByRole('button', { name: 'Search' }).click()
  await expect(dialog.locator('.candidate-row')).toHaveCount(3)
  return dialog
}

/** Clicks Add on the row, waits for "In library", and returns the new paper's id. */
async function addFrom(row: Locator): Promise<string> {
  await row.getByRole('button', { name: 'Add' }).click()
  const inLibrary = row.getByRole('link', { name: 'In library' })
  await expect(inLibrary).toBeVisible()
  return (await inLibrary.getAttribute('href'))!.replace('#/papers/', '')
}

async function waitUntilReady(request: APIRequestContext, paperId: string) {
  await expect
    .poll(async () => (await (await request.get(`/api/papers/${paperId}`)).json()).status, { timeout: 30_000 })
    .toBe('ready')
}

test('find a paper from the library, add its free PDF, and see it in the library', { tag }, async ({
  page,
  request,
}) => {
  await page.goto('/')
  const dialog = await searchFromHere(page)

  await expect(rowOf(dialog, FREE).getByText('PDF', { exact: true })).toBeVisible()
  await expect(rowOf(dialog, CLOSED).getByText('No free PDF')).toBeVisible()
  await expect(rowOf(dialog, CLOSED).getByRole('button', { name: 'Add' })).toHaveCount(0)
  await expect(rowOf(dialog, CLOSED).getByRole('link', { name: 'Open page' })).toHaveAttribute(
    'href',
    `https://doi.org/${FAKE_DOI_PREFIX}closed`,
  )

  const paperId = await addFrom(rowOf(dialog, FREE))
  await waitUntilReady(request, paperId)

  await page.keyboard.press('Escape')
  const libraryRow = page.locator('.paper-row').filter({ has: page.locator(`a[href="#/papers/${paperId}"]`) })
  await expect(libraryRow).toContainText(FREE)

  // Reopening keeps the row's "In library" state: the dialog keeps its query, so no second search is needed.
  await page.getByRole('button', { name: 'Find papers' }).click()
  const reopened = page.getByRole('dialog', { name: 'Find papers' })
  await expect(rowOf(reopened, FREE).getByRole('link', { name: 'In library' })).toHaveAttribute(
    'href',
    `#/papers/${paperId}`,
  )
})

test('regaining window focus does not repeat a search or drop its results for a busy error', { tag }, async ({
  page,
}) => {
  await page.goto('/')
  const dialog = await searchFromHere(page)
  await addFrom(rowOf(dialog, FREE)) // marks discovery stale (D-fix 1)

  const asked: string[] = []
  page.on('request', (r) => {
    if (r.url().includes('/api/discovery/search')) asked.push(r.url())
  })
  // React Query v5's focusManager listens for `visibilitychange` on `window` (not `document`, despite MDN).
  await page.evaluate(() => window.dispatchEvent(new Event('visibilitychange')))
  await page.waitForTimeout(300)

  expect(asked).toEqual([])
  await expect(dialog.locator('.candidate-row')).toHaveCount(3) // the stale results are still shown, not an error
})

test('a paper whose PDF link is a web page says no free PDF was found', { tag }, async ({ page }) => {
  await page.goto('/')
  const dialog = await searchFromHere(page)

  await rowOf(dialog, LANDING).getByRole('button', { name: 'Add' }).click()

  await expect(rowOf(dialog, LANDING).getByRole('alert')).toContainText('No free PDF was found for this paper.')
  await expect(rowOf(dialog, LANDING).getByRole('button', { name: 'Add' })).toBeEnabled()
})

test('a paper found from a workspace joins that workspace', { tag }, async ({ page, request, workspaceId }) => {
  await page.goto(`/#/workspaces/${workspaceId}`)
  const dialog = await searchFromHere(page)

  const paperId = await addFrom(rowOf(dialog, FREE))

  const members: { id: string }[] = await (await request.get(`/api/workspaces/${workspaceId}/papers`)).json()
  expect(members.map((paper) => paper.id)).toContain(paperId)
  await waitUntilReady(request, paperId)
})

test('the Similar tab asks for suggestions only once opened, and adds one', { tag }, async ({
  page,
  request,
  paperId,
}) => {
  const asked: string[] = []
  page.on('request', (r) => {
    if (r.url().includes('/similar')) asked.push(r.url())
  })
  await openReader(page, paperId)
  expect(asked).toEqual([])

  await page.getByRole('tab', { name: 'Similar' }).click()
  const panel = page.getByRole('complementary', { name: 'Similar papers' })
  await expect(panel.locator('.candidate-row')).toHaveCount(3)
  expect(asked).toHaveLength(1)
  await expect(page).toHaveURL(new RegExp(`#/papers/${paperId}\\?tab=similar$`))

  await waitUntilReady(request, await addFrom(rowOf(panel, FREE)))
})

test('each row shows the sources that found it, and a source turned off in Settings loses its badge', { tag }, async ({
  page,
}) => {
  const sourcesOf = (dialog: Locator, title: string) => rowOf(dialog, title).locator('.candidate-sources li')
  await page.goto('/')
  let dialog = await searchFromHere(page)
  await expect(sourcesOf(dialog, FREE)).toHaveText(['Crossref', 'arXiv', 'CORE'])
  await expect(sourcesOf(dialog, LANDING)).toHaveText(['Crossref'])

  // In the same page, so the search has to be asked again rather than read from the cache it left behind.
  await page.keyboard.press('Escape')
  await page.getByRole('link', { name: 'Settings' }).click()
  // Settings is one page per section now: the rail opens Model connections, and Paper sources is its own. Clicked,
  // not `goto`, so this stays the same page and the search below is asked again rather than read from its cache.
  await page.getByRole('navigation', { name: 'Settings sections' }).getByRole('link', { name: 'Paper sources' }).click()
  const core = page.getByRole('region', { name: 'Paper sources' }).getByRole('checkbox', { name: 'CORE' })
  await core.click()
  await expect(core).not.toBeChecked()
  await page.getByRole('link', { name: 'Library' }).click()

  dialog = await searchFromHere(page)
  await expect(sourcesOf(dialog, FREE)).toHaveText(['Crossref', 'arXiv'])
  await expect(sourcesOf(dialog, LANDING)).toHaveText(['Crossref'])
})

test('with every source that searches titles off, Find papers says to turn one on', { tag }, async ({
  page,
  request,
}) => {
  // OpenAlex is off already; PubMed/PMC/Europe PMC/Zenodo/HAL/ACM DL/DOAJ/OpenAIRE all search titles
  // too and are on by default (SSRN defaults off), so each needs turning off here as well
  await setSourceSettings(
    request,
    {
      enabled: {
        crossref: false, arxiv: false, core: false, pubmed: false, pmc: false, europe_pmc: false,
        zenodo: false, hal: false, acm_dl: false, ssrn: false, doaj: false, openaire: false,
      },
    },
  )
  await page.goto('/')
  await page.getByRole('button', { name: 'Find papers' }).click()
  const dialog = page.getByRole('dialog', { name: 'Find papers' })
  await dialog.getByRole('textbox', { name: QUERY_LABEL }).fill('fixture')
  await dialog.getByRole('button', { name: 'Search' }).click()

  await expect(dialog.getByRole('alert')).toHaveText(NO_SOURCE)
  await expect(dialog.locator('.candidate-row')).toHaveCount(0)
})
