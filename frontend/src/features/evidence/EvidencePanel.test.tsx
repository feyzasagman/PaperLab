// @vitest-environment jsdom
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, test, vi } from 'vitest'
import { api, type EvidenceCard } from '@/api/client'
import { EvidencePanel } from './EvidencePanel'

const card = {
  paper_id: 'p1', status: 'ready', error: null, provenance: 'llm', model: 'm', prompt_version: 1, updated_at: null,
  fields: {
    research_question: { value: 'Does sleep help?', quote: 'does sleep help', page: 4, chunk_id: 'c9', flags: [] },
    design: { value: null, quote: null, page: null, chunk_id: null, flags: ['unsupported'] },
    sample: { n: null, population: null, quote: null, page: null, chunk_id: null, flags: [] },
    findings: [], effect_sizes: [], limitations: [],
  },
} as unknown as EvidenceCard

afterEach(() => vi.restoreAllMocks())

test('the reader section shows the card and a page link jumps to the cited passage', async () => {
  vi.spyOn(api, 'getEvidenceCard').mockResolvedValue(card)
  vi.spyOn(api, 'listChunks').mockResolvedValue([{ id: 'c9', page: 4, bbox: [[1, 2, 3, 4]] }] as never)
  const onShowRegion = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><EvidencePanel paperId="p1" onShowRegion={onShowRegion} /></QueryClientProvider>)
  expect(await screen.findByText('Does sleep help?')).toBeInTheDocument()
  expect(screen.getByText('1 left out: no supporting quote')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Go to page 4' }))
  await waitFor(() => expect(onShowRegion).toHaveBeenCalledWith(4, [[1, 2, 3, 4]]))
})

test('a paper with no card offers to make one', async () => {
  vi.spyOn(api, 'getEvidenceCard').mockRejectedValue(new Error('Paper p1 has no evidence card.'))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><EvidencePanel paperId="p1" onShowRegion={vi.fn()} /></QueryClientProvider>)
  expect(await screen.findByRole('button', { name: 'Generate evidence card' })).toBeInTheDocument()
})
