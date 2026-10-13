// @vitest-environment jsdom
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, test, vi } from 'vitest'
import { SearchControls } from './SearchControls'
import * as queries from '@/api/queries'
import type { PaperSources } from '@/api/client'

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

/** Mirrors app/core/paper_sources.py's defaults: OpenAlex off (P3, it can cost money), everything else on,
 * including Unpaywall — whose `enabled` flag must be ignored here, since it has no search role at all (C1). */
function mockPaperSources(overrides: Partial<Record<PaperSources['sources'][number]['id'], boolean>> = {}) {
  const enabled = (id: PaperSources['sources'][number]['id'], fallback: boolean) => overrides[id] ?? fallback
  const sources: PaperSources['sources'] = [
    { id: 'openalex', name: 'OpenAlex', enabled: enabled('openalex', false), has_key: null, key_hint: null },
    { id: 'crossref', name: 'Crossref', enabled: enabled('crossref', true), has_key: null, key_hint: null },
    {
      id: 'semantic_scholar', name: 'Semantic Scholar', enabled: enabled('semantic_scholar', true),
      has_key: null, key_hint: null,
    },
    { id: 'arxiv', name: 'arXiv', enabled: enabled('arxiv', true), has_key: null, key_hint: null },
    { id: 'core', name: 'CORE', enabled: enabled('core', true), has_key: null, key_hint: null },
    { id: 'unpaywall', name: 'Unpaywall', enabled: enabled('unpaywall', true), has_key: null, key_hint: null },
  ]
  vi.spyOn(queries, 'usePaperSources').mockReturnValue({
    data: { contact_email: null, sources },
  } as any)
}

afterEach(() => {
  vi.restoreAllMocks()
})

test('typing a query and clicking Start calls onStart with the enabled search sources, never unpaywall', () => {
  mockPaperSources()
  const onStart = vi.fn()
  renderWithClient(<SearchControls onStart={onStart} isRunning={false} />)

  fireEvent.change(screen.getByLabelText('Search query'), { target: { value: 'code review LLM' } })
  fireEvent.click(screen.getByRole('button', { name: 'Start' }))

  expect(onStart).toHaveBeenCalledTimes(1)
  const [args] = onStart.mock.calls[0]
  expect(args).toEqual({ query: 'code review LLM', filters: {}, sources: expect.any(Array), query_overrides: {} })
  expect([...args.sources].sort()).toEqual(['arxiv', 'core', 'crossref', 'semantic_scholar'])
  expect(args.sources).not.toContain('unpaywall')
})

test('reflects a source turned on in settings (OpenAlex) and still excludes unpaywall even when it is enabled', () => {
  mockPaperSources({ openalex: true })
  const onStart = vi.fn()
  renderWithClient(<SearchControls onStart={onStart} isRunning={false} />)

  fireEvent.change(screen.getByLabelText('Search query'), { target: { value: 'q' } })
  fireEvent.click(screen.getByRole('button', { name: 'Start' }))

  const [args] = onStart.mock.calls[0]
  expect([...args.sources].sort()).toEqual(['arxiv', 'core', 'crossref', 'openalex', 'semantic_scholar'])
})

test('shows Stop instead of Start while a run is in progress', () => {
  mockPaperSources()
  renderWithClient(<SearchControls onStart={vi.fn()} isRunning={true} />)
  expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument()
})

test('disables Start until a query is typed', () => {
  mockPaperSources()
  renderWithClient(<SearchControls onStart={vi.fn()} isRunning={false} />)
  expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled()
})

test('disables Start while paper sources have not loaded yet, even with a query typed', () => {
  vi.spyOn(queries, 'usePaperSources').mockReturnValue({ data: undefined } as any)
  renderWithClient(<SearchControls onStart={vi.fn()} isRunning={false} />)

  fireEvent.change(screen.getByLabelText('Search query'), { target: { value: 'code review LLM' } })

  expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled()
})

test('disables Start when every search source is off in settings, instead of sending an empty list', () => {
  mockPaperSources({ crossref: false, semantic_scholar: false, arxiv: false, core: false, openalex: false })
  renderWithClient(<SearchControls onStart={vi.fn()} isRunning={false} />)

  fireEvent.change(screen.getByLabelText('Search query'), { target: { value: 'code review LLM' } })

  expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled()
})

test('year inputs add only the filled years to filters', () => {
  mockPaperSources()
  const onStart = vi.fn()
  renderWithClient(<SearchControls onStart={onStart} isRunning={false} />)
  fireEvent.change(screen.getByLabelText('Search query'), { target: { value: 'q' } })

  fireEvent.change(screen.getByLabelText('From year'), { target: { value: '2023' } })
  fireEvent.click(screen.getByRole('button', { name: 'Start' }))
  expect(onStart.mock.calls[0][0].filters).toEqual({ year_from: 2023 })

  fireEvent.change(screen.getByLabelText('From year'), { target: { value: '' } })
  fireEvent.click(screen.getByRole('button', { name: 'Start' }))
  expect(onStart.mock.calls[1][0].filters).toEqual({})
})

test('Start is disabled while the first year is after the last year', () => {
  mockPaperSources()
  renderWithClient(<SearchControls onStart={vi.fn()} isRunning={false} />)
  fireEvent.change(screen.getByLabelText('Search query'), { target: { value: 'q' } })
  fireEvent.change(screen.getByLabelText('From year'), { target: { value: '2025' } })
  fireEvent.change(screen.getByLabelText('To year'), { target: { value: '2020' } })
  expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('To year'), { target: { value: '2026' } })
  expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled()
})
