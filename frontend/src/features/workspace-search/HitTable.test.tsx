// @vitest-environment jsdom
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { HitTable } from './HitTable'
import { api, type Hit } from '@/api/client'

/** The row list only — excludes the preview panel, which also renders the current hit's title/byline. Always
 * present (even before data loads), so this can be synchronous. */
const pool = () => within(screen.getByLabelText('Hit pool'))
/** The preview panel only. Only mounts once there's a hit to preview, so this is async. */
const preview = async () => within(await screen.findByLabelText('Hit preview'))

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return { client, ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) }
}

const baseHit = {
  id: 'h1',
  run_id: 'run-1',
  external_ref_id: null,
  source_method: 'arxiv',
  normalized_title: 'a paper about llms',
  first_seen_at: '2026-09-24T00:00:00Z',
  stage1_status: null,
  stage1_exclude_reason: null,
  stage1_note: null,
  priority: null,
  topic_fit: null,
  acquisition_status: 'pending',
  paper_id: null,
  sources: [],
} satisfies Hit

beforeEach(() => {
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [baseHit], next_cursor: null })
  vi.spyOn(api, 'patchSearchHit').mockResolvedValue(baseHit)
  vi.spyOn(api, 'importSearchHits')
  vi.spyOn(api, 'uploadHitPdf')
  vi.spyOn(api, 'clearSearchHits')
  vi.spyOn(api, 'bulkPatchSearchHits').mockResolvedValue({ updated: 1 })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  // jsdom never lays anything out, so offsetHeight is always 0 — the virtualizer treats a zero-height
  // scroll container as "nothing visible" and renders no rows at all. Give it a plausible viewport.
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(600)
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(600)
})

afterEach(() => {
  vi.restoreAllMocks()
  // The sort choice persists to localStorage (hitSort.ts) keyed by workspace id; every test here uses 'ws-1', so
  // without this a sort change in one test (e.g. the ranked-order test below) leaks into the next one.
  localStorage.clear()
})

test('renders hit titles from the paginated query', async () => {
  renderWithClient(<HitTable workspaceId="ws-1" />)
  expect(await pool().findByText('a paper about llms')).toBeInTheDocument()
})

test('a hit with a linked ExternalRef shows its real title and a compact byline in the row, not just normalized_title', async () => {
  const richHit: Hit = {
    ...baseHit,
    title: 'Attention Is All You Need',
    authors: ['Ashish Vaswani', 'Noam Shazeer', 'Niki Parmar', 'Jakob Uszkoreit'],
    year: 2017,
  }
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [richHit], next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText(/Attention Is All You Need/)

  // Byline caps at 3 authors even though 4 are present.
  expect(pool().getByText(/Ashish Vaswani, Noam Shazeer, Niki Parmar · 2017/)).toBeInTheDocument()
  expect(pool().queryByText(/Jakob Uszkoreit/)).not.toBeInTheDocument()
  expect(pool().queryByText('a paper about llms')).not.toBeInTheDocument()
})

test('the row shows a label for the hit\'s first (most-trusted) source; a hit with none shows no label', async () => {
  const hits: Hit[] = [
    { ...baseHit, id: 'h1', title: 'Has Sources', normalized_title: 'has sources', sources: ['arxiv', 'openalex'] },
    { ...baseHit, id: 'h2', title: 'No Sources', normalized_title: 'no sources', sources: [] },
  ]
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: hits, next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('Has Sources')

  expect(pool().getByText('arXiv')).toBeInTheDocument()
  expect(pool().queryByText('OpenAlex')).not.toBeInTheDocument() // only the first (most-trusted) source shows
})

test('the preview panel lists every source that matched the hit, not just the first', async () => {
  const hit: Hit = { ...baseHit, sources: ['arxiv', 'openalex', 'core'] }
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [hit], next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()

  expect(await panel.findByText('Found via arXiv, OpenAlex, CORE')).toBeInTheDocument()
})

test('a hit with an abstract or an already-acquired PDF sorts before one with neither', async () => {
  const hits: Hit[] = [
    { ...baseHit, id: 'h1', title: 'No Info Paper', normalized_title: 'no info paper' },
    { ...baseHit, id: 'h2', title: 'Has Abstract Paper', normalized_title: 'has abstract paper', abstract: 'A summary.' },
    { ...baseHit, id: 'h3', title: 'Has PDF Paper', normalized_title: 'has pdf paper', acquisition_status: 'imported' },
  ]
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: hits, next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('No Info Paper')

  const text = screen.getByLabelText('Hit pool').textContent ?? ''
  expect(text.indexOf('Has Abstract Paper')).toBeLessThan(text.indexOf('No Info Paper'))
  expect(text.indexOf('Has PDF Paper')).toBeLessThan(text.indexOf('No Info Paper'))
})

test('the sort is stable: hits within the same group (both informative, or both bare) keep their server order', async () => {
  const hits: Hit[] = [
    { ...baseHit, id: 'h1', title: 'Bare First', normalized_title: 'bare first' },
    { ...baseHit, id: 'h2', title: 'Abstract First', normalized_title: 'abstract first', abstract: 'One.' },
    { ...baseHit, id: 'h3', title: 'Abstract Second', normalized_title: 'abstract second', abstract: 'Two.' },
    { ...baseHit, id: 'h4', title: 'Bare Second', normalized_title: 'bare second' },
  ]
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: hits, next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('Bare First')

  const text = screen.getByLabelText('Hit pool').textContent ?? ''
  expect(text.indexOf('Abstract First')).toBeLessThan(text.indexOf('Abstract Second'))
  expect(text.indexOf('Bare First')).toBeLessThan(text.indexOf('Bare Second'))
})

test('typing in the search box filters the pool by title, client-side, with no new request', async () => {
  const hits: Hit[] = [
    { ...baseHit, id: 'h1', title: 'Attention Is All You Need', normalized_title: 'attention is all you need' },
    { ...baseHit, id: 'h2', title: 'BERT: Pre-training', normalized_title: 'bert pre-training' },
  ]
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: hits, next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('Attention Is All You Need')
  const callsBeforeTyping = vi.mocked(api.listSearchHits).mock.calls.length

  fireEvent.change(screen.getByLabelText('Search hits'), { target: { value: 'bert' } })

  await waitFor(() => expect(pool().queryByText('Attention Is All You Need')).not.toBeInTheDocument())
  expect(pool().getByText('BERT: Pre-training')).toBeInTheDocument()
  expect(screen.getByText('1 of 2 in pool')).toBeInTheDocument()
  expect(api.listSearchHits).toHaveBeenCalledTimes(callsBeforeTyping)
})

test('the search box also matches the abstract, not just the title', async () => {
  const hits: Hit[] = [
    {
      ...baseHit, id: 'h1', title: 'Attention Is All You Need', normalized_title: 'attention is all you need',
      abstract: 'A novel network architecture based solely on attention mechanisms.',
    },
    {
      ...baseHit, id: 'h2', title: 'BERT: Pre-training', normalized_title: 'bert pre-training',
      abstract: 'We introduce a new language representation model.',
    },
  ]
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: hits, next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('Attention Is All You Need')

  // "mechanisms" appears only in the first hit's abstract, not in either title.
  fireEvent.change(screen.getByLabelText('Search hits'), { target: { value: 'mechanisms' } })

  await waitFor(() => expect(pool().queryByText('BERT: Pre-training')).not.toBeInTheDocument())
  expect(pool().getByText('Attention Is All You Need')).toBeInTheDocument()
})

test('a filter that matches nothing says so instead of showing an empty list', async () => {
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')

  fireEvent.change(screen.getByLabelText('Search hits'), { target: { value: 'nonexistent' } })

  expect(await screen.findByText('No hits match “nonexistent”.')).toBeInTheDocument()
})

test('the previewed hit shows its title, byline and abstract in the side panel', async () => {
  const richHit: Hit = {
    ...baseHit,
    title: 'Attention Is All You Need',
    authors: ['Ashish Vaswani'],
    year: 2017,
    venue: 'NeurIPS',
    abstract: 'The dominant sequence transduction models are based on complex recurrent networks.',
  }
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [richHit], next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  // A single hit is previewed by default (falls back to the first loaded row).
  const panel = await preview()
  await panel.findByText('Attention Is All You Need')

  expect(panel.getByText('Ashish Vaswani · 2017 · NeurIPS')).toBeInTheDocument()
  expect(
    panel.getByText('The dominant sequence transduction models are based on complex recurrent networks.'),
  ).toBeInTheDocument()
})

test('clicking a row previews it, and hovering another row does nothing', async () => {
  const hits: Hit[] = [
    { ...baseHit, id: 'h1', title: 'First Paper', normalized_title: 'first paper' },
    { ...baseHit, id: 'h2', title: 'Second Paper', normalized_title: 'second paper' },
  ]
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: hits, next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()
  await panel.findByText('First Paper') // the first row previews by default

  fireEvent.mouseEnter(pool().getByText('Second Paper'))
  expect(panel.queryByText('Second Paper')).not.toBeInTheDocument() // hover alone changes nothing

  fireEvent.click(pool().getByText('Second Paper'))
  await panel.findByText('Second Paper')
  expect(panel.queryByText('First Paper')).not.toBeInTheDocument()
})

test('↓/↑ move the previewed selection to the next/previous row and stop at the ends', async () => {
  const hits: Hit[] = [
    { ...baseHit, id: 'h1', title: 'First Paper', normalized_title: 'first paper' },
    { ...baseHit, id: 'h2', title: 'Second Paper', normalized_title: 'second paper' },
  ]
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: hits, next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()
  await panel.findByText('First Paper')

  const hitPool = screen.getByLabelText('Hit pool')
  fireEvent.keyDown(hitPool, { key: 'ArrowDown' })
  await panel.findByText('Second Paper')

  fireEvent.keyDown(hitPool, { key: 'ArrowDown' }) // already at the last row — stays put, no wrap
  await panel.findByText('Second Paper')

  fireEvent.keyDown(hitPool, { key: 'ArrowUp' })
  await panel.findByText('First Paper')
})

test('a hit with no linked ExternalRef previews its normalized title and says no abstract is available', async () => {
  renderWithClient(<HitTable workspaceId="ws-1" />)

  const panel = await preview()
  expect(await panel.findByText('a paper about llms')).toBeInTheDocument()
  expect(panel.getByText('No abstract available for this hit.')).toBeInTheDocument()
})

test('focusing a different row (via its ⋮ button) swaps the preview immediately, no hover delay', async () => {
  const hits: Hit[] = [
    { ...baseHit, id: 'h1', title: 'First Paper', normalized_title: 'first paper' },
    { ...baseHit, id: 'h2', title: 'Second Paper', normalized_title: 'second paper' },
  ]
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: hits, next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()
  await panel.findByText('First Paper') // the first row previews by default

  const [, secondRowMenuButton] = pool().getAllByRole('button', { name: 'Hit actions' })
  fireEvent.focus(secondRowMenuButton)

  await panel.findByText('Second Paper')
  expect(panel.queryByText('First Paper')).not.toBeInTheDocument()
})

test('the preview panel offers the same review actions as the menu, so reading and deciding are the same step', async () => {
  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()
  await panel.findByText('a paper about llms')

  fireEvent.click(panel.getByRole('button', { name: 'Relevant' }))

  await waitFor(() => expect(api.patchSearchHit).toHaveBeenCalledWith('ws-1', 'h1', { stage1_status: 'relevant' }))
})

test('the preview panel requires a reason before Not relevant can be clicked, same rule as the menu', async () => {
  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()
  await panel.findByText('a paper about llms')

  expect(panel.getByRole('button', { name: 'Not relevant' })).toBeDisabled()

  fireEvent.change(panel.getByLabelText('Exclusion reason'), { target: { value: 'duplicate' } })
  expect(panel.getByRole('button', { name: 'Not relevant' })).toBeEnabled()
  fireEvent.click(panel.getByRole('button', { name: 'Not relevant' }))

  await waitFor(() =>
    expect(api.patchSearchHit).toHaveBeenCalledWith('ws-1', 'h1', {
      stage1_status: 'not_relevant',
      stage1_exclude_reason: 'duplicate',
    }),
  )
})

test('the preview panel offers a way to get the PDF for a hit that needs one: search, open page, copy DOI, upload', async () => {
  const hit: Hit = { ...baseHit, doi: '10.1234/attention', acquisition_status: 'failed' }
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [hit], next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()
  await panel.findByText('a paper about llms')

  expect(panel.getByRole('button', { name: 'Add PDF' })).toBeInTheDocument()
  expect(panel.getByRole('link', { name: 'Search by title' })).toHaveAttribute(
    'href',
    'https://scholar.google.com/scholar?q=a%20paper%20about%20llms',
  )
  expect(panel.getByRole('link', { name: 'Open page' })).toHaveAttribute('href', 'https://doi.org/10.1234/attention')
  expect(panel.getByRole('button', { name: 'Copy DOI' })).toBeInTheDocument()
  expect(panel.getByLabelText('Upload PDF for a paper about llms')).toBeInTheDocument()
})

test('clicking "Add PDF" in the preview panel imports only that one hit, not the whole filter, and never re-fetches the pool', async () => {
  const hit: Hit = { ...baseHit, acquisition_status: 'pending' }
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [hit], next_cursor: null })
  vi.spyOn(api, 'importSearchHits').mockResolvedValue({ imported: 1, failed: 0 })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()
  await panel.findByText('a paper about llms')
  const callsBeforeClick = vi.mocked(api.listSearchHits).mock.calls.length

  fireEvent.click(panel.getByRole('button', { name: 'Add PDF' }))

  await waitFor(() => expect(api.importSearchHits).toHaveBeenCalledWith('ws-1', ['h1']))
  // A successful import updates the hit's own acquisition_status (the "Get the PDF" section disappears)
  // by patching the already-loaded row in place, not by re-fetching the pool from the server.
  await waitFor(() => expect(panel.queryByRole('button', { name: 'Add PDF' })).not.toBeInTheDocument())
  expect(api.listSearchHits).toHaveBeenCalledTimes(callsBeforeClick)
})

test('a hit with no free PDF found stays acquirable and tells the user to use the manual options', async () => {
  const hit: Hit = { ...baseHit, acquisition_status: 'pending' }
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [hit], next_cursor: null })
  vi.spyOn(api, 'importSearchHits').mockResolvedValue({ imported: 0, failed: 1 })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()
  await panel.findByText('a paper about llms')

  fireEvent.click(panel.getByRole('button', { name: 'Add PDF' }))

  expect(await screen.findByRole('status')).toHaveTextContent('No PDF found automatically')
  // Not a mutation error — the manual fallback options stay offered, same as any other unacquired hit.
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(panel.getByRole('link', { name: 'Search by title' })).toBeInTheDocument()
})

test('uploading a PDF from the preview panel calls the upload mutation for that hit', async () => {
  vi.spyOn(api, 'uploadHitPdf').mockResolvedValue({ ...baseHit, acquisition_status: 'manual', paper_id: 'p1' })
  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()
  await panel.findByText('a paper about llms')

  const file = new File(['%PDF-1.4'], 'paper.pdf', { type: 'application/pdf' })
  fireEvent.change(panel.getByLabelText('Upload PDF for a paper about llms'), { target: { files: [file] } })

  await waitFor(() => expect(api.uploadHitPdf).toHaveBeenCalledWith('ws-1', 'h1', file))
})

test('the preview panel hides the PDF-getting options once a hit is already imported or manually acquired', async () => {
  const hit: Hit = { ...baseHit, acquisition_status: 'imported' }
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [hit], next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()
  await panel.findByText('a paper about llms')

  expect(panel.queryByRole('link', { name: 'Search by title' })).not.toBeInTheDocument()
  expect(panel.queryByLabelText('Upload PDF for a paper about llms')).not.toBeInTheDocument()
})

test('the trailing ⋮ button opens a menu with Relevant, Maybe and Not relevant…', async () => {
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')
  fireEvent.pointerDown(pool().getByRole('button', { name: 'Hit actions' }))

  expect(await screen.findByRole('menuitem', { name: 'Relevant' })).toBeInTheDocument()
  expect(screen.getByRole('menuitem', { name: 'Maybe' })).toBeInTheDocument()
  expect(screen.getByRole('menuitem', { name: 'Not relevant…' })).toBeInTheDocument()
})

test('right-clicking a row opens the same menu', async () => {
  renderWithClient(<HitTable workspaceId="ws-1" />)
  fireEvent.contextMenu(await pool().findByText('a paper about llms'))

  expect(await screen.findByRole('menuitem', { name: 'Relevant' })).toBeInTheDocument()
})

test('marking relevant from the ⋮ menu sends only stage1_status', async () => {
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')
  fireEvent.pointerDown(pool().getByRole('button', { name: 'Hit actions' }))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Relevant' }))

  await waitFor(() => expect(api.patchSearchHit).toHaveBeenCalledWith('ws-1', 'h1', { stage1_status: 'relevant' }))
})

test('clicking Snowball from the ⋮ menu seeds the mutation with this hit, not a paper id', async () => {
  const snowballSpy = vi.spyOn(api, 'snowball').mockResolvedValue({ new_hits: 0, skipped_seeds: [], errors: {} })
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')
  fireEvent.pointerDown(pool().getByRole('button', { name: 'Hit actions' }))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Snowball' }))

  await waitFor(() =>
    expect(snowballSpy).toHaveBeenCalledWith('ws-1', { seed_hit_ids: ['h1'], backward: true, forward: true }),
  )
})

test('a successful snowball from the hit menu reports the new-hit count near the pool', async () => {
  vi.spyOn(api, 'snowball').mockResolvedValue({ new_hits: 2, skipped_seeds: [], errors: {} })
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')
  fireEvent.pointerDown(pool().getByRole('button', { name: 'Hit actions' }))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Snowball' }))

  expect(await screen.findByRole('status')).toHaveTextContent('Found 2 new papers')
})

test('reviewing a hit patches it into the pool in place, without re-fetching the pool', async () => {
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')
  const callsBeforeClick = vi.mocked(api.listSearchHits).mock.calls.length

  fireEvent.pointerDown(pool().getByRole('button', { name: 'Hit actions' }))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Relevant' }))

  await waitFor(() => expect(api.patchSearchHit).toHaveBeenCalled())
  expect(api.listSearchHits).toHaveBeenCalledTimes(callsBeforeClick)
})

test('picking a reason under Not relevant… sends stage1_status and the reason together', async () => {
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')
  fireEvent.pointerDown(pool().getByRole('button', { name: 'Hit actions' }))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Not relevant…' }))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'duplicate' }))

  await waitFor(() =>
    expect(api.patchSearchHit).toHaveBeenCalledWith('ws-1', 'h1', {
      stage1_status: 'not_relevant',
      stage1_exclude_reason: 'duplicate',
    }),
  )
})

test('a failed review shows an error message', async () => {
  vi.spyOn(api, 'patchSearchHit').mockRejectedValue(new Error('Review failed'))
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')
  fireEvent.pointerDown(pool().getByRole('button', { name: 'Hit actions' }))
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Maybe' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('Review failed')
})

test('checking a hit and clicking Mark relevant bulk-reviews just that one and clears the selection', async () => {
  const hits: Hit[] = [
    { ...baseHit, id: 'h1', title: 'First', normalized_title: 'first' },
    { ...baseHit, id: 'h2', title: 'Second', normalized_title: 'second' },
  ]
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: hits, next_cursor: null })
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('First')

  fireEvent.click(pool().getByRole('checkbox', { name: 'Select First' }))
  expect(screen.getByText('1 selected')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: 'Mark relevant' }))

  await waitFor(() =>
    expect(api.bulkPatchSearchHits).toHaveBeenCalledWith('ws-1', { hit_ids: ['h1'], stage1_status: 'relevant' }),
  )
  expect(screen.queryByText('1 selected')).not.toBeInTheDocument() // selection bar is gone once cleared
})

test('select-all-visible checks every row, and Mark not relevant is disabled until a reason is picked', async () => {
  const hits: Hit[] = [
    { ...baseHit, id: 'h1', title: 'First', normalized_title: 'first' },
    { ...baseHit, id: 'h2', title: 'Second', normalized_title: 'second' },
  ]
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: hits, next_cursor: null })
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('First')

  fireEvent.click(screen.getByRole('checkbox', { name: 'Select all visible hits' }))
  expect(screen.getByText('2 selected')).toBeInTheDocument()

  const notRelevantButton = screen.getByRole('button', { name: /Mark not relevant/ })
  expect(notRelevantButton).toBeDisabled()

  fireEvent.change(screen.getByRole('combobox', { name: 'Exclusion reason for selected hits' }), {
    target: { value: 'duplicate' },
  })
  fireEvent.click(notRelevantButton)

  await waitFor(() =>
    expect(api.bulkPatchSearchHits).toHaveBeenCalledWith('ws-1', {
      hit_ids: ['h1', 'h2'],
      stage1_status: 'not_relevant',
      stage1_exclude_reason: 'duplicate',
    }),
  )
})

test('a failed bulk review shows an error message', async () => {
  vi.spyOn(api, 'bulkPatchSearchHits').mockRejectedValue(new Error('Bulk review failed'))
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')

  fireEvent.click(pool().getByRole('checkbox', { name: 'Select a paper about llms' }))
  fireEvent.click(screen.getByRole('button', { name: 'Mark relevant' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('Bulk review failed')
})

test('stays virtualized: a large hit pool renders far fewer rows than it has hits', async () => {
  const manyHits: Hit[] = Array.from({ length: 200 }, (_, i) => ({ ...baseHit, id: `h${i}`, normalized_title: `hit-${i}` }))
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: manyHits, next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('hit-0')

  const rowMenuButtons = pool().getAllByRole('button', { name: 'Hit actions' })
  expect(rowMenuButtons.length).toBeGreaterThan(0)
  expect(rowMenuButtons.length).toBeLessThan(50)
})

test('at the bottom with no known next page, real new progress triggers exactly one refetch — never on its own', async () => {
  // A single hit with next_cursor: null means hasNextPage is false from the very first render, and a 1-item
  // list is trivially "scrolled to the bottom" — this isolates the fallback path (no known next page, but the
  // worker found more) from the ordinary fetchNextPage path, which a bigger pool would also exercise.
  const runAt = (arxivRawCount: number) => ({
    id: 'run-1', workspace_id: 'ws-1', query_text: 'q', filters_json: {}, sources_json: ['arxiv'],
    status: 'running', started_at: '2026-09-24T00:00:00Z', stopped_at: null,
    stats_json: { per_source_raw_count: { arxiv: arxivRawCount } },
  })

  const { client, rerender } = renderWithClient(<HitTable workspaceId="ws-1" run={runAt(10) as never} />)
  await pool().findByText('a paper about llms')
  expect(api.listSearchHits).toHaveBeenCalledTimes(1) // the first run tick only sets a baseline, nothing to refetch yet

  const rerenderWithClient = (run: unknown) =>
    rerender(
      <QueryClientProvider client={client}>
        <HitTable workspaceId="ws-1" run={run as never} />
      </QueryClientProvider>,
    )

  // A poll tick reporting the same total (nothing new) must not trigger a refetch.
  rerenderWithClient(runAt(10))
  await waitFor(() => expect(pool().getByText('a paper about llms')).toBeInTheDocument())
  expect(api.listSearchHits).toHaveBeenCalledTimes(1)

  // Real new progress (raw count grew) while sitting at the bottom triggers exactly one refetch.
  rerenderWithClient(runAt(25))
  await waitFor(() => expect(api.listSearchHits).toHaveBeenCalledTimes(2))

  // Acknowledged — a further tick with no additional progress must not trigger yet another one.
  rerenderWithClient(runAt(25))
  await new Promise((resolve) => setTimeout(resolve, 10))
  expect(api.listSearchHits).toHaveBeenCalledTimes(2)
})

test('shows the live raw found count from the run, separate from the deduped "in pool" count', async () => {
  const run = {
    id: 'run-1', workspace_id: 'ws-1', query_text: 'q', filters_json: {}, sources_json: ['arxiv', 'openalex'],
    status: 'running', started_at: '2026-09-24T00:00:00Z', stopped_at: null,
    stats_json: { per_source_raw_count: { arxiv: 80, openalex: 47 } },
  }

  renderWithClient(<HitTable workspaceId="ws-1" run={run as never} />)
  await pool().findByText('a paper about llms')

  expect(screen.getByText(/127 found so far/)).toBeInTheDocument()
  expect(screen.getByText(/1 in pool/)).toBeInTheDocument()
})

test('with no run yet, only the "in pool" count shows — no "found so far" claim with nothing to back it', async () => {
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')

  expect(screen.queryByText(/found so far/)).not.toBeInTheDocument()
})

test('a failed import shows an error message', async () => {
  vi.spyOn(api, 'importSearchHits').mockRejectedValue(new Error('Import failed'))
  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()
  await panel.findByText('a paper about llms')

  fireEvent.click(panel.getByRole('button', { name: 'Add PDF' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('Import failed')
})

test('two simultaneous mutation errors collapse to one status line, not a stack of two', async () => {
  vi.spyOn(api, 'bulkPatchSearchHits').mockRejectedValue(new Error('Bulk review failed'))
  vi.spyOn(api, 'importSearchHits').mockRejectedValue(new Error('Import failed'))
  renderWithClient(<HitTable workspaceId="ws-1" />)
  const panel = await preview()
  await panel.findByText('a paper about llms')
  fireEvent.click(panel.getByRole('button', { name: 'Add PDF' }))
  await screen.findByRole('alert') // the import error lands first

  fireEvent.click(pool().getByRole('checkbox', { name: 'Select a paper about llms' }))
  fireEvent.click(screen.getByRole('button', { name: 'Mark relevant' }))

  // bulkPatch outranks importHits in the priority order, and only one alert is ever on screen at once.
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Bulk review failed'))
  expect(screen.getAllByRole('alert')).toHaveLength(1)
})

test('"Import all with PDF in this filter" imports every hit still pending in the pool, not just one', async () => {
  vi.spyOn(api, 'importSearchHits').mockResolvedValue({ imported: 1, failed: 0 })
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')

  fireEvent.click(screen.getByRole('button', { name: 'Import all with PDF in this filter' }))

  await waitFor(() => expect(api.importSearchHits).toHaveBeenCalledWith('ws-1', undefined))
})

test('a failed bulk import shows an error message', async () => {
  vi.spyOn(api, 'importSearchHits').mockRejectedValue(new Error('Bulk import failed'))
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')

  fireEvent.click(screen.getByRole('button', { name: 'Import all with PDF in this filter' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('Bulk import failed')
})

test('"Clear old results" asks for confirmation before deleting anything', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(false)
  vi.spyOn(api, 'clearSearchHits').mockResolvedValue({ deleted: 5 })
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')

  fireEvent.click(screen.getByRole('button', { name: 'Clear old results' }))

  expect(window.confirm).toHaveBeenCalled()
  expect(api.clearSearchHits).not.toHaveBeenCalled()
})

test('confirming "Clear old results" deletes the not-yet-imported hits', async () => {
  vi.spyOn(api, 'clearSearchHits').mockResolvedValue({ deleted: 5 })
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')

  fireEvent.click(screen.getByRole('button', { name: 'Clear old results' }))

  await waitFor(() => expect(api.clearSearchHits).toHaveBeenCalledWith('ws-1'))
})

test('a failed clear shows an error message', async () => {
  vi.spyOn(api, 'clearSearchHits').mockRejectedValue(new Error('Clear failed'))
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')

  fireEvent.click(screen.getByRole('button', { name: 'Clear old results' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('Clear failed')
})

test('lists hits in ranked order when "Most likely relevant first" is chosen', async () => {
  // hitA has an abstract, hitB none: found order's hasPdfOrAbstract sort puts hitA first, so only the ranked
  // order (server-supplied, unsorted client-side) puts hitB first — the thing this test actually exercises.
  const hitA: Hit = { ...baseHit, id: 'h1', title: 'Has Abstract', abstract: 'A summary.' }
  const hitB: Hit = { ...baseHit, id: 'h2', title: 'No Abstract' }
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [hitA, hitB], next_cursor: null })
  vi.spyOn(api, 'rankedSearchHits').mockResolvedValue({
    items: [hitB, hitA], trained: true, total_unscreened: 2, streak: 0, threshold: 50, show_stop_hint: false,
  })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('Has Abstract')

  fireEvent.change(screen.getByLabelText('Sort hits'), { target: { value: 'ranked' } })

  await waitFor(() => {
    const titles = [...document.querySelectorAll('[data-slot="context-menu-trigger"]')].map((r) => r.textContent)
    expect(titles[0]).toContain(hitB.title)
    expect(titles[1]).toContain(hitA.title)
  })
})

test('lists hits newest publication first when "Newest publication first" is chosen, with no year last', async () => {
  const older: Hit = { ...baseHit, id: 'h1', title: 'Older Paper', year: 2018 }
  const newer: Hit = { ...baseHit, id: 'h2', title: 'Newer Paper', year: 2023 }
  const noYear: Hit = { ...baseHit, id: 'h3', title: 'No Year Paper', year: null }
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [older, newer, noYear], next_cursor: null })

  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('Older Paper')

  fireEvent.change(screen.getByLabelText('Sort hits'), { target: { value: 'year' } })

  await waitFor(() => {
    const titles = [...document.querySelectorAll('[data-slot="context-menu-trigger"]')].map((r) => r.textContent)
    expect(titles[0]).toContain(newer.title)
    expect(titles[1]).toContain(older.title)
    expect(titles[2]).toContain(noYear.title)
  })
})

test('the preview panel can be dragged wider with its resize handle, and remembers the width', async () => {
  localStorage.removeItem('paperlab-hit-preview-width')
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await preview()
  const handle = screen.getByRole('separator', { name: 'Resize preview' })
  expect(handle).toHaveAttribute('aria-valuenow', '352')

  fireEvent.keyDown(handle, { key: 'ArrowLeft' })

  expect(handle).toHaveAttribute('aria-valuenow', '368')
  expect(localStorage.getItem('paperlab-hit-preview-width')).toBe('368')
})

const suggested: Hit = {
  ...baseHit, id: 'h1', title: 'Sleep trial', suggestion: 'include', suggestion_relevance: 4, suggestion_confidence: 'high',
}

test('the AI score order asks the server for sort=ai, and Score ≥ sends min_relevance', async () => {
  localStorage.clear()
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('a paper about llms')
  fireEvent.change(screen.getByLabelText('Sort hits'), { target: { value: 'ai' } })
  await waitFor(() => expect(api.listSearchHits).toHaveBeenCalledWith('ws-1', expect.objectContaining({ sort: 'ai' })))
  fireEvent.change(screen.getByLabelText('Score ≥'), { target: { value: '4' } })
  await waitFor(() =>
    expect(api.listSearchHits).toHaveBeenCalledWith('ws-1', expect.objectContaining({ sort: 'ai', min_relevance: 4 })),
  )
})

test('a suggested undecided row has an Accept button named after the paper that accepts just that hit', async () => {
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [suggested], next_cursor: null })
  vi.spyOn(api, 'acceptSuggestions').mockResolvedValue({ accepted: 1, skipped: [] })
  renderWithClient(<HitTable workspaceId="ws-1" />)
  fireEvent.click(await pool().findByRole('button', { name: 'Accept: Sleep trial' }))
  await waitFor(() => expect(api.acceptSuggestions).toHaveBeenCalledWith('ws-1', ['h1']))
})

test('no Accept button on a decided row or an unsure suggestion', async () => {
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({
    items: [
      { ...suggested, id: 'a', title: 'Decided', stage1_status: 'relevant' },
      { ...suggested, id: 'b', title: 'Unsure one', suggestion: 'unsure' },
    ],
    next_cursor: null,
  })
  renderWithClient(<HitTable workspaceId="ws-1" />)
  await pool().findByText('Decided')
  expect(pool().queryByRole('button', { name: /^Accept:/ })).toBeNull()
})

test('Accept AI suggestions in the bulk bar sends the selected ids and reports accepted and skipped', async () => {
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [suggested], next_cursor: null })
  vi.spyOn(api, 'acceptSuggestions').mockResolvedValue({ accepted: 3, skipped: ['x', 'y'] })
  renderWithClient(<HitTable workspaceId="ws-1" />)
  fireEvent.click(await pool().findByRole('checkbox', { name: 'Select Sleep trial' }))
  fireEvent.click(screen.getByRole('button', { name: 'Accept AI suggestions' }))
  await waitFor(() => expect(api.acceptSuggestions).toHaveBeenCalledWith('ws-1', ['h1']))
  expect(await screen.findByText('Accepted 3, skipped 2')).toBeInTheDocument()
})

test('a failed accept shows the error', async () => {
  vi.spyOn(api, 'listSearchHits').mockResolvedValue({ items: [suggested], next_cursor: null })
  vi.spyOn(api, 'acceptSuggestions').mockRejectedValue(new Error('Accept failed'))
  renderWithClient(<HitTable workspaceId="ws-1" />)
  fireEvent.click(await pool().findByRole('button', { name: 'Accept: Sleep trial' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Accept failed')
})
