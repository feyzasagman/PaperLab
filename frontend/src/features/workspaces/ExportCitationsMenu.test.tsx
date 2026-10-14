// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, test, vi } from 'vitest'
import { api } from '@/api/client'
import * as clipboard from '@/lib/clipboard'
import { ExportCitationsMenu } from './ExportCitationsMenu'

afterEach(() => vi.restoreAllMocks())

async function openMenu() {
  const client = new QueryClient()
  render(<QueryClientProvider client={client}><ExportCitationsMenu workspaceId="w1" /></QueryClientProvider>)
  fireEvent.pointerDown(screen.getByRole('button', { name: 'Export citations' }))
  await screen.findByRole('menuitem', { name: 'BibTeX' })
}

test('offers BibTeX, RIS and CSL-JSON as downloads from the workspace route', async () => {
  await openMenu()
  for (const [name, format] of [['BibTeX', 'bibtex'], ['RIS', 'ris'], ['CSL-JSON', 'csljson']]) {
    const item = screen.getByRole('menuitem', { name })
    expect(item.getAttribute('href')).toBe(`/api/workspaces/w1/citations?format=${format}`)
    expect(item.hasAttribute('download')).toBe(true)
  }
})

test('APA 7 opens a preview rendered as HTML and Copy copies the plain text', async () => {
  const text = vi.spyOn(api, 'citationsText').mockImplementation(async (_id, format) =>
    format === 'apa-html' ? 'Smith, J. (2020). <i>Sleep</i>.' : 'Smith, J. (2020). Sleep.')
  const copy = vi.spyOn(clipboard, 'copyText').mockResolvedValue()
  await openMenu()
  fireEvent.click(screen.getByRole('menuitem', { name: 'APA 7…' }))
  const preview = await screen.findByText('Sleep')
  expect(preview.tagName).toBe('I')
  fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
  await waitFor(() => expect(copy).toHaveBeenCalledWith('Smith, J. (2020). Sleep.'))
  expect(text).toHaveBeenCalledWith('w1', 'apa-html')
  expect(await screen.findByText('Copied')).toBeTruthy()
})

test('a refused export shows its reason in the dialog', async () => {
  vi.spyOn(api, 'citationsText').mockRejectedValue(new Error('no_papers'))
  await openMenu()
  fireEvent.click(screen.getByRole('menuitem', { name: 'APA 7…' }))
  expect(await screen.findByText(/no_papers/)).toBeTruthy()
})
