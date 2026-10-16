// @vitest-environment jsdom
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import * as queries from '@/api/queries'
import type { SearchRunSummary } from '@/api/client'
import { api } from '@/api/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RunsList } from './RunsList'

const run = (id: string, query: string): SearchRunSummary => ({
  id, query_text: query, status: 'exhausted', started_at: '2026-10-14T00:00:00Z', stopped_at: null,
  sources_json: ['arxiv'], per_source_raw_count: { arxiv: 3 }, failed_sources: [], hit_count: 3,
  can_continue: false, sources_left: [], paused_sources: [],
})

function setup(rows: SearchRunSummary[], runId: string | null = null, onSelect = vi.fn()) {
  vi.spyOn(queries, 'useSearchRuns').mockReturnValue({ data: rows } as any)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RunsList workspaceId="w1" runId={runId} onSelect={onSelect} /></QueryClientProvider>)
  return onSelect
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })

test('opening Runs (2) lists both queries newest first with hit counts', () => {
  setup([run('r2', 'newer query'), run('r1', 'older query')])
  const toggle = screen.getByRole('button', { name: 'Runs (2)' })
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  expect(screen.queryByText('newer query')).toBeNull()
  fireEvent.click(toggle)
  expect(toggle.getAttribute('aria-expanded')).toBe('true')
  const rows = screen.getAllByRole('button', { name: /query/ })
  expect(rows.map((r) => r.textContent)).toEqual([expect.stringContaining('newer query'), expect.stringContaining('older query')])
  expect(screen.getAllByText(/3 new hits/)).toHaveLength(2)
})

test('a run with one hit says "1 new hit"', () => {
  setup([{ ...run('r1', 'solo query'), hit_count: 1 }])
  fireEvent.click(screen.getByRole('button', { name: 'Runs (1)' }))
  expect(screen.getByText(/1 new hit(?!s)/)).toBeTruthy()
})

test('clicking a row selects that run', () => {
  const onSelect = setup([run('r2', 'newer query'), run('r1', 'older query')])
  fireEvent.click(screen.getByRole('button', { name: 'Runs (2)' }))
  fireEvent.click(screen.getByRole('button', { name: /older query/ }))
  expect(onSelect).toHaveBeenCalledWith('r1')
})

test('the current run is marked aria-current', () => {
  setup([run('r2', 'newer query'), run('r1', 'older query')], 'r1')
  fireEvent.click(screen.getByRole('button', { name: 'Runs (2)' }))
  expect(screen.getByRole('button', { name: /older query/ }).getAttribute('aria-current')).toBe('true')
  expect(screen.getByRole('button', { name: /newer query/ }).getAttribute('aria-current')).toBeNull()
})

test('an empty list renders nothing', () => {
  setup([])
  expect(screen.queryByRole('button', { name: /Runs/ })).toBeNull()
})

const pausedRun = {
  ...run('r3', 'paused query'), can_continue: true, sources_left: ['arxiv', 'crossref'], paused_sources: ['arxiv', 'crossref'],
}

test('Continue shows only on can_continue rows, with the full accessible name, as a sibling of the row button', () => {
  setup([pausedRun, run('r1', 'older query')])
  fireEvent.click(screen.getByRole('button', { name: 'Runs (2)' }))
  const buttons = screen.getAllByRole('button', { name: /^Continue/ })
  expect(buttons).toHaveLength(1)
  expect(buttons[0].getAttribute('aria-label')).toBe('Continue: fetch the next results from 2 sources')
  expect(buttons[0].textContent).toBe('Continue')
  expect(buttons[0].parentElement?.closest('button')).toBeNull()
})

test('one source left reads "1 source"', () => {
  setup([{ ...pausedRun, sources_left: ['arxiv'], paused_sources: ['arxiv'] }])
  fireEvent.click(screen.getByRole('button', { name: 'Runs (1)' }))
  expect(screen.getByRole('button', { name: 'Continue: fetch the next results from 1 source' })).toBeTruthy()
})

test('clicking Continue calls the API and does not select the row', async () => {
  const cont = vi.spyOn(api, 'continueSearchRun').mockResolvedValue({} as never)
  const onSelect = setup([pausedRun])
  fireEvent.click(screen.getByRole('button', { name: 'Runs (1)' }))
  fireEvent.click(screen.getByRole('button', { name: /^Continue/ }))
  await waitFor(() => expect(cont).toHaveBeenCalledWith('w1', 'r3'))
  expect(onSelect).not.toHaveBeenCalled()
})

test('a paused run says so in plain words instead of "exhausted"', () => {
  setup([pausedRun])
  fireEvent.click(screen.getByRole('button', { name: 'Runs (1)' }))
  expect(screen.getByText(/Paused at 200 results per source; Continue fetches more from 2 sources/)).toBeTruthy()
  expect(screen.queryByText(/exhausted/)).toBeNull()
})
