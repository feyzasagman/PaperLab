import type { Page } from '@playwright/test'
import { expect, test } from './fixtures'
import {
  MOVES_PAPER_SOURCES,
  readSourceSettings,
  setSourceSettings,
  type SourceId,
  type SourceSettings,
} from './paperSources'

const tag = MOVES_PAPER_SOURCES
const EMAIL_PROBLEM = 'Enter an email address like name@example.org.'
const UNPAYWALL_NOTE = 'Add a contact email to use Unpaywall.'

// E2E runs on the owner's database (D76): each test snapshots the switches and the email, and puts them back after.
let owners: SourceSettings

test.beforeEach(async ({ request }) => {
  owners = await readSourceSettings(request)
})

// Unconditional, like the owner's default model in fixtures.ts: a no-op PATCH when nothing moved is harmless.
test.afterEach(async ({ request }) => {
  await setSourceSettings(request, owners)
})

const section = (page: Page) => page.getByRole('region', { name: 'Paper sources' })
const rowOf = (page: Page, id: SourceId) => section(page).locator(`.paper-source-row[data-source-id="${id}"]`)

test('ticking a source off and on saves it, and it stays that way after a reload', { tag }, async ({
  page,
  request,
}) => {
  await setSourceSettings(request, { enabled: { crossref: true } })
  await page.goto('/#/settings/sources')
  const crossref = section(page).getByRole('checkbox', { name: 'Crossref' })
  await expect(crossref).toBeChecked()

  await crossref.click()
  await expect(crossref).not.toBeChecked() // shown once saved and read back
  await page.reload()
  await expect(crossref).not.toBeChecked()
  expect((await readSourceSettings(request)).enabled.crossref).toBe(false)

  await crossref.click()
  await expect(crossref).toBeChecked()
  await page.reload()
  await expect(crossref).toBeChecked()
})

test('OpenAlex says it may cost money, and Unpaywall asks for a contact email', { tag }, async ({ page, request }) => {
  await setSourceSettings(request, { contact_email: null })
  await page.goto('/#/settings/sources')

  await expect(rowOf(page, 'openalex')).toContainText('May cost money')
  await expect(rowOf(page, 'openalex')).toContainText('Free up to $0.10 of use a day without a key')
  await expect(rowOf(page, 'ssrn')).toContainText('May cost money')
  await expect(rowOf(page, 'crossref')).not.toContainText('May cost money')
  await expect(rowOf(page, 'unpaywall')).toContainText(UNPAYWALL_NOTE)
})

test('the contact email is checked before it is sent, saved, and removed', { tag }, async ({ page, request }) => {
  await setSourceSettings(request, { contact_email: null })
  await page.goto('/#/settings/sources')
  const email = section(page).getByRole('textbox', { name: 'Contact email' })
  const save = section(page).getByRole('button', { name: 'Save', exact: true })
  const remove = section(page).getByRole('button', { name: 'Remove', exact: true })

  await email.fill('not-an-email')
  await save.click()
  await expect(section(page).getByRole('alert')).toHaveText(EMAIL_PROBLEM)
  expect((await readSourceSettings(request)).contact_email).toBeNull()

  await email.fill('paperlab-e2e@example.org')
  await save.click()
  await expect(remove).toBeVisible()
  await expect(rowOf(page, 'unpaywall')).not.toContainText(UNPAYWALL_NOTE)
  await page.reload()
  await expect(email).toHaveValue('paperlab-e2e@example.org')

  await remove.click()
  await expect(remove).toBeHidden()
  await expect(email).toHaveValue('')
  await expect(rowOf(page, 'unpaywall')).toContainText(UNPAYWALL_NOTE)
  expect((await readSourceSettings(request)).contact_email).toBeNull()
})

const NAMES: Record<SourceId, string> = {
  openalex: 'OpenAlex',
  crossref: 'Crossref',
  semantic_scholar: 'Semantic Scholar',
  arxiv: 'arXiv',
  core: 'CORE',
  unpaywall: 'Unpaywall',
  pubmed: 'PubMed',
  pmc: 'PMC',
  europe_pmc: 'Europe PMC',
  zenodo: 'Zenodo',
  hal: 'HAL',
  acm_dl: 'ACM DL',
  ssrn: 'SSRN',
  doaj: 'DOAJ',
  openaire: 'OpenAIRE',
  iacr_eprint: 'IACR ePrint',
  biorxiv: 'bioRxiv',
  medrxiv: 'medRxiv',
}
const KEYED: SourceId[] = ['openalex', 'semantic_scholar', 'core', 'pubmed', 'pmc', 'zenodo']

/** GET /api/paper-sources as the API answers it, with CORE's key saved (`coreHint`: its last 4 characters) or not. */
function stubbedSources(coreHint: string | null) {
  return {
    contact_email: null,
    sources: (Object.keys(NAMES) as SourceId[]).map((id) => ({
      id,
      name: NAMES[id],
      enabled: id !== 'openalex',
      has_key: KEYED.includes(id) ? id === 'core' && coreHint !== null : null,
      key_hint: id === 'core' ? coreHint : null,
    })),
  }
}

test('an API key is saved and removed without ever being shown back', { tag }, async ({ page }) => {
  // Stubbed, never the real API (spec P8): GET never returns a key, so a real save would replace the owner's key for
  // good. The API's own key rules (kept, removed, blank refused, never in a response) are pytest's.
  const KEY = 'core-e2e-SECRET-9876'
  const sent: unknown[] = []
  let answer = stubbedSources(null)
  await page.route('**/api/paper-sources', async (route) => {
    if (route.request().method() === 'PATCH') {
      const patch = route.request().postDataJSON()
      sent.push(patch)
      answer = stubbedSources(patch.api_keys?.core ? patch.api_keys.core.slice(-4) : null)
    }
    await route.fulfill({ json: answer })
  })
  await page.goto('/#/settings/sources')
  const core = rowOf(page, 'core')

  await core.getByLabel('CORE API key').fill(KEY)
  await core.getByRole('button', { name: 'Save key' }).click()
  await expect(core.locator('.key-hint')).toHaveText('Key ending in 9876')
  expect(sent).toEqual([{ api_keys: { core: KEY } }])
  await expect(page.locator('body')).not.toContainText(KEY)
  const fieldValues = await page.locator('input').evaluateAll((inputs) =>
    inputs.map((input) => (input as HTMLInputElement).value),
  )
  expect(fieldValues).not.toContain(KEY)

  page.once('dialog', (confirm) => void confirm.accept())
  await core.getByRole('button', { name: 'Remove key' }).click()
  await expect(core.getByLabel('CORE API key')).toHaveValue('')
  expect(sent).toEqual([{ api_keys: { core: KEY } }, { api_keys: { core: null } }])
})
