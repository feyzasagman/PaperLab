// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, expect, it, vi } from 'vitest'
import type { Paper } from '@/api/client'
import { api } from '@/api/client'
import { PublishedVersionBanner } from './PublishedVersionBanner'

const paper = { id: 'p1', published_doi: '10.1/pub' } as Paper
const candidate = (over: object) => ({ title: 'T', doi: '10.1/pub', pdf_urls: [], paper_id: null, ...over })
const renderBanner = () =>
  render(
    <QueryClientProvider client={new QueryClient()}>
      <PublishedVersionBanner paper={paper} />
    </QueryClientProvider>,
  )

beforeEach(() => vi.restoreAllMocks())

it('announces the published version with its DOI link', () => {
  renderBanner()
  expect(screen.getByText('A published version of this preprint exists')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'doi:10.1/pub' })).toHaveAttribute('href', 'https://doi.org/10.1%2Fpub')
})

it('adds the published version when it has a free PDF', async () => {
  const found = candidate({ pdf_urls: ['https://x/a.pdf'] })
  vi.spyOn(api, 'searchPapers').mockResolvedValue({ results: [found], notices: [] } as never)
  const add = vi.spyOn(api, 'addCandidate').mockResolvedValue({ id: 'p2' } as Paper)
  renderBanner()
  fireEvent.click(screen.getByRole('button', { name: 'Add the published version' }))
  await waitFor(() => expect(add).toHaveBeenCalledWith(found))
  expect(await screen.findByText('Added to your library.')).toBeInTheDocument()
})

it('links the landing page when there is no free PDF', async () => {
  vi.spyOn(api, 'searchPapers').mockResolvedValue({ results: [candidate({})], notices: [] } as never)
  const add = vi.spyOn(api, 'addCandidate')
  renderBanner()
  fireEvent.click(screen.getByRole('button', { name: 'Add the published version' }))
  expect(await screen.findByText(/no free PDF/i)).toBeInTheDocument()
  expect(add).not.toHaveBeenCalled()
})

it('points at the paper already in the library', async () => {
  vi.spyOn(api, 'searchPapers').mockResolvedValue({ results: [candidate({ paper_id: 'p9' })], notices: [] } as never)
  renderBanner()
  fireEvent.click(screen.getByRole('button', { name: 'Add the published version' }))
  expect(await screen.findByRole('link', { name: 'Open it' })).toHaveAttribute('href', '#/papers/p9')
})
