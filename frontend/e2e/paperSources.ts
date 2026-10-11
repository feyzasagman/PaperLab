import { expect, type APIRequestContext } from '@playwright/test'

export type SourceId =
  | 'openalex' | 'crossref' | 'semantic_scholar' | 'arxiv' | 'core' | 'unpaywall' | 'pubmed' | 'pmc' | 'europe_pmc'
  | 'zenodo' | 'hal' | 'acm_dl' | 'ssrn' | 'doaj' | 'openaire' | 'iacr_eprint' | 'biorxiv' | 'medrxiv'
export type Switches = Record<SourceId, boolean>

/**
 * What a test may change and must put back: the switches and the contact email. Never keys (spec P8): GET never
 * returns one, so a key the test replaced or removed could not be restored on the owner's database.
 */
export type SourceSettings = { contact_email: string | null; enabled: Switches }

/** The tag for every test that changes them: it runs one at a time, after the parallel specs (playwright.config.ts). */
export const MOVES_PAPER_SOURCES = '@moves-paper-sources'

/**
 * Every source on but OpenAlex. The fake answers them all offline, but OpenAlex's switch also drives the worker's
 * metadata lookups, which the fake doesn't cover: a paper a test adds would reach the real OpenAlex.
 */
export const FREE_SOURCES_ON: Switches = {
  openalex: false,
  crossref: true,
  semantic_scholar: true,
  arxiv: true,
  core: true,
  unpaywall: true,
  pubmed: false,
  pmc: false,
  europe_pmc: false,
  zenodo: false,
  hal: false,
  acm_dl: false,
  ssrn: false,
  doaj: false,
  openaire: false,
  iacr_eprint: false,
  biorxiv: false,
  medrxiv: false,
}

/** The owner's switches and email, to put back after the test (E2E runs on the owner's database). */
export async function readSourceSettings(request: APIRequestContext): Promise<SourceSettings> {
  const response = await request.get('/api/paper-sources')
  expect(response.status(), 'the owner’s paper sources have to be readable before a test changes them').toBe(200)
  const body: { contact_email: string | null; sources: { id: SourceId; enabled: boolean }[] } = await response.json()
  const enabled = Object.fromEntries(body.sources.map((source) => [source.id, source.enabled])) as Switches
  return { contact_email: body.contact_email, enabled }
}

/** Sends only switches and the email: the type has no room for `api_keys`. */
export async function setSourceSettings(
  request: APIRequestContext,
  changes: { contact_email?: string | null; enabled?: Partial<Switches> },
) {
  const response = await request.patch('/api/paper-sources', { data: changes })
  expect(response.status(), `could not set the paper sources to ${JSON.stringify(changes)}`).toBe(200)
}
