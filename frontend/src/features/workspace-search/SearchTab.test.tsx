// @vitest-environment jsdom
import { useState } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { SearchTab } from './SearchTab'
import { api } from '@/api/client'
import type { PaperSources } from '@/api/client'

/** `runId` is a prop from the URL in real life (WorkspacePage owns it) — this tiny stand-in plays that role for
 * tests that need to see a run actually start, so `onRunIdChange` has somewhere real to land. */
function Harness({ workspaceId }: { workspaceId: string }) {
  const [runId, setRunId] = useState<string | null>(null)
  return <SearchTab workspaceId={workspaceId} runId={runId} onRunIdChange={setRunId} />
}

const enabledSources: PaperSources = {
  contact_email: null,
  sources: [
    { id: 'arxiv', name: 'arXiv', enabled: true, has_key: null, key_hint: null },
    { id: 'crossref', name: 'Crossref', enabled: true, has_key: null, key_hint: null },
    { id: 'core', name: 'CORE', enabled: true, has_key: null, key_hint: null },
    { id: 'semantic_scholar', name: 'Semantic Scholar', enabled: true, has_key: null, key_hint: null },
    { id: 'openalex', name: 'OpenAlex', enabled: false, has_key: null, key_hint: null },
    { id: 'unpaywall', name: 'Unpaywall', enabled: true, has_key: null, key_hint: null },
  ],
}

function renderWithClient(ui: React.ReactElement, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return { client, ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) }
}

beforeEach(() => {
  vi.spyOn(api, 'startSearchRun')
  vi.spyOn(api, 'getSearchRun')
  vi.spyOn(api, 'stopSearchRun')
  vi.spyOn(api, 'paperSources').mockResolvedValue(enabledSources)
})

afterEach(() => {
  vi.restoreAllMocks()
})

test('renders the search controls', () => {
  renderWithClient(<SearchTab workspaceId="ws-1" runId={null} onRunIdChange={vi.fn()} />)
  expect(screen.getByLabelText('Search query')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Start' })).toBeInTheDocument()
})

test('shows a status indicator once a run has started, and reports the new run id (I1: for the URL, not local state)', async () => {
  const runningRun = {
    id: 'run-1',
    workspace_id: 'ws-1',
    query_text: 'code review LLM',
    filters_json: {},
    sources_json: ['arxiv'],
    status: 'running',
    started_at: '2026-09-24T00:00:00Z',
    stopped_at: null,
    stats_json: { last_batch_new_hits: 7 },
  } as never
  vi.mocked(api.startSearchRun).mockResolvedValue(runningRun)
  vi.mocked(api.getSearchRun).mockResolvedValue(runningRun)

  renderWithClient(<Harness workspaceId="ws-1" />)

  fireEvent.change(screen.getByLabelText('Search query'), { target: { value: 'code review LLM' } })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Start' }))

  await waitFor(() => expect(api.getSearchRun).toHaveBeenCalledWith('ws-1', 'run-1'))
  await waitFor(() => expect(screen.getByText(/7 new in last batch/)).toBeInTheDocument())
})

test('a runId already set (as if restored from the URL on reload) shows the run at once, with no Start click', async () => {
  const runningRun = {
    id: 'run-1', workspace_id: 'ws-1', query_text: 'q', filters_json: {}, sources_json: ['arxiv'],
    status: 'running', started_at: '2026-09-24T00:00:00Z', stopped_at: null, stats_json: {},
  } as never
  vi.mocked(api.getSearchRun).mockResolvedValue(runningRun)

  renderWithClient(<SearchTab workspaceId="ws-1" runId="run-1" onRunIdChange={vi.fn()} />)

  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument())
  expect(api.getSearchRun).toHaveBeenCalledWith('ws-1', 'run-1')
})

test('clicking Stop stops the in-progress run', async () => {
  const runningRunData = {
    id: 'run-1',
    workspace_id: 'ws-1',
    query_text: 'code review LLM',
    filters_json: {},
    sources_json: ['arxiv'],
    status: 'running',
    started_at: '2026-09-24T00:00:00Z',
    stopped_at: null,
    stats_json: {},
  }
  const stoppedRunData = { ...runningRunData, status: 'stopped', stopped_at: '2026-09-24T00:01:00Z' }
  vi.mocked(api.startSearchRun).mockResolvedValue(runningRunData as never)
  vi.mocked(api.getSearchRun).mockResolvedValue(runningRunData as never)
  vi.mocked(api.stopSearchRun).mockResolvedValue(stoppedRunData as never)

  renderWithClient(<Harness workspaceId="ws-1" />)

  fireEvent.change(screen.getByLabelText('Search query'), { target: { value: 'code review LLM' } })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Start' }))

  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: 'Stop' }))

  await waitFor(() => expect(api.stopSearchRun).toHaveBeenCalledWith('ws-1', 'run-1'))
})

test('a 409 from Stop (the run already finished naturally) is swallowed, not shown as an error (Minor 5)', async () => {
  const runningRun = {
    id: 'run-1', workspace_id: 'ws-1', query_text: 'q', filters_json: {}, sources_json: ['arxiv'],
    status: 'running', started_at: '2026-09-24T00:00:00Z', stopped_at: null, stats_json: {},
  }
  const exhaustedRun = { ...runningRun, status: 'exhausted', stopped_at: '2026-09-24T00:01:00Z' }
  vi.mocked(api.getSearchRun).mockResolvedValue(runningRun as never)
  vi.mocked(api.stopSearchRun).mockRejectedValue(new Error('search_run_not_running'))

  renderWithClient(<SearchTab workspaceId="ws-1" runId="run-1" onRunIdChange={vi.fn()} />)

  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument())
  vi.mocked(api.getSearchRun).mockResolvedValue(exhaustedRun as never) // what a real Stop-race would actually find
  fireEvent.click(screen.getByRole('button', { name: 'Stop' }))

  // The mutation resolves (via a re-fetch of the run) instead of erroring, so the UI catches up to "exhausted"
  // and no alert ever appears.
  await waitFor(() => expect(screen.getByText(/exhausted/)).toBeInTheDocument())
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

// The hit pool no longer auto-refetches or shows a banner from here at all — HitTable owns that decision
// entirely now, gated on the user actually being scrolled to the bottom (see HitTable.test.tsx's "at the bottom
// with no known next page, real new progress triggers exactly one refetch" test). SearchTab's status line above
// stays real-time regardless (covered by the "shows a status indicator" test above), since it's just the
// already-polled run object rendered directly — no separate fetch of its own to gate.

test('a refused start shows its reason as a readable sentence', async () => {
  vi.mocked(api.startSearchRun).mockRejectedValue(new Error('invalid_years'))
  renderWithClient(<Harness workspaceId="ws-1" />)
  fireEvent.change(screen.getByLabelText('Search query'), { target: { value: 'q' } })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Start' }))
  await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/first year must not be after/))
})

test('a run stopped by its time limit says so and how many sources were not finished (D266)', async () => {
  const cutRun = {
    id: 'run-2', workspace_id: 'ws-1', query_text: 'q', filters_json: {}, sources_json: ['arxiv'],
    status: 'exhausted', started_at: '2026-09-24T00:00:00Z', stopped_at: '2026-09-24T00:29:00Z',
    stats_json: { cut_short: true, unfinished_sources: ['arxiv', 'crossref', 'openalex', 'core'] },
  } as never
  vi.mocked(api.getSearchRun).mockResolvedValue(cutRun)
  renderWithClient(<SearchTab workspaceId="ws-1" runId="run-2" onRunIdChange={vi.fn()} />)
  await waitFor(() => expect(screen.getByText('Stopped early after 29 min — 4 sources not finished')).toBeInTheDocument())
})
