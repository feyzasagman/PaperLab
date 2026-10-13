// @vitest-environment jsdom
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { api, type EvidenceCard, type EvidenceCardRow } from '@/api/client'
import { EvidenceTab } from './EvidenceTab'

const item = (value: string, page = 3, flags: string[] = []) => ({ value, quote: `quote of ${value}`, page, chunk_id: 'c1', flags })
const ready = (over: Record<string, unknown> = {}, provenance = 'llm'): EvidenceCard =>
  ({
    paper_id: 'p1', status: 'ready', error: null, provenance, model: 'm', prompt_version: 1, updated_at: null,
    fields: {
      research_question: item('Does sleep help memory?'),
      design: item('Randomised trial'),
      sample: { n: 40, population: 'students', quote: 'forty students', page: 2, chunk_id: 'c2', flags: [] },
      findings: [item('Sleep helped'), item('Naps helped'), { value: null, quote: null, page: null, chunk_id: null, flags: ['unsupported'] }],
      effect_sizes: [],
      limitations: [],
      ...over,
    },
  }) as EvidenceCard
const row = (card: EvidenceCard | null, paper_id = 'p1', title = 'Sleep paper'): EvidenceCardRow =>
  ({ paper_id, title, year: 2020, first_author: 'Lee', card: card && { ...card, paper_id } })

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><EvidenceTab workspaceId="w1" /></QueryClientProvider>)
}

beforeEach(() => {
  vi.spyOn(api, 'editEvidenceCard').mockResolvedValue(ready())
  vi.spyOn(api, 'generateEvidenceCards').mockResolvedValue([])
  vi.spyOn(api, 'generateEvidenceCard').mockResolvedValue([])
})
afterEach(() => vi.restoreAllMocks())

test('shows the first value, a +N more count, the page and an AI badge', async () => {
  vi.spyOn(api, 'listEvidenceCards').mockResolvedValue([row(ready())])
  renderTab()
  const cell = await screen.findByRole('button', { name: 'Edit Findings for Sleep paper' })
  expect(cell).toHaveTextContent('Sleep helped')
  expect(cell).toHaveTextContent('+1 more')
  expect(cell).toHaveTextContent('p. 3')
  expect(screen.getAllByText('AI').length).toBeGreaterThan(0)
})

test('never shows the text of an unsupported item, only a count', async () => {
  vi.spyOn(api, 'listEvidenceCards').mockResolvedValue([row(ready())])
  renderTab()
  await screen.findByRole('button', { name: 'Edit Findings for Sleep paper' })
  expect(screen.getByText('1 left out: no supporting quote')).toBeInTheDocument()
})

test('an edited card says AI · edited', async () => {
  vi.spyOn(api, 'listEvidenceCards').mockResolvedValue([row(ready({ design: { ...item('Randomised trial'), flags: ['edited'] } }, 'llm_edited'))])
  renderTab()
  const cell = await screen.findByRole('button', { name: 'Edit Design for Sleep paper' })
  expect(cell).toHaveTextContent('AI · edited')
})

test('an empty card says Not generated and its Generate button asks for that paper', async () => {
  vi.spyOn(api, 'listEvidenceCards').mockResolvedValue([row(null)])
  renderTab()
  expect(await screen.findByText('Not generated')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Generate card for Sleep paper' }))
  await waitFor(() => expect(api.generateEvidenceCard).toHaveBeenCalledWith('p1', false, false))
})

test('failed cards say why, in words; generating shows a spinner', async () => {
  const failed = { ...ready(), status: 'failed', error: 'no_text' } as EvidenceCard
  const busy = { ...ready(), status: 'generating' } as EvidenceCard
  vi.spyOn(api, 'listEvidenceCards').mockResolvedValue([row(failed, 'p1', 'A'), row(busy, 'p2', 'B')])
  renderTab()
  expect(await screen.findByText(/no readable text/)).toBeInTheDocument()
  expect(screen.getByRole('status', { name: 'Making the card for B' })).toBeInTheDocument()
})

test('Generate cards asks for the missing ones; Export CSV links to the CSV route', async () => {
  vi.spyOn(api, 'listEvidenceCards').mockResolvedValue([row(null)])
  renderTab()
  await screen.findByText('Not generated')
  fireEvent.click(screen.getByRole('button', { name: 'Generate cards' }))
  await waitFor(() => expect(api.generateEvidenceCards).toHaveBeenCalledWith('w1'))
  expect(screen.getByRole('link', { name: 'Export CSV' })).toHaveAttribute('href', '/api/workspaces/w1/evidence-cards.csv')
})

test('the edit popover shows the quote and page read-only and saves only the changed value', async () => {
  vi.spyOn(api, 'listEvidenceCards').mockResolvedValue([row(ready())])
  renderTab()
  fireEvent.click(await screen.findByRole('button', { name: 'Edit Findings for Sleep paper' }))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText('quote of Naps helped')).toBeInTheDocument()
  fireEvent.change(within(dialog).getByRole('textbox', { name: 'Findings 2' }), { target: { value: 'Naps helped a little' } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
  await waitFor(() =>
    expect(api.editEvidenceCard).toHaveBeenCalledWith('p1', [{ field: 'findings', index: 1, values: { value: 'Naps helped a little' } }]),
  )
})

test('regenerating an edited card asks in the app, then sends confirm', async () => {
  vi.spyOn(api, 'listEvidenceCards').mockResolvedValue([row(ready({}, 'llm_edited'))])
  const confirm = vi.spyOn(window, 'confirm')
  renderTab()
  fireEvent.click(await screen.findByRole('button', { name: 'Regenerate card for Sleep paper' }))
  expect(api.generateEvidenceCard).not.toHaveBeenCalled()
  fireEvent.click(await screen.findByRole('button', { name: 'Replace my edits' }))
  await waitFor(() => expect(api.generateEvidenceCard).toHaveBeenCalledWith('p1', true, true))
  expect(confirm).not.toHaveBeenCalled()
})
