// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { api } from '@/api/client'
import { ImportMarkdownButton } from './ImportMarkdownButton'

afterEach(() => vi.restoreAllMocks())

function setup() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ImportMarkdownButton />
    </QueryClientProvider>,
  )
  return screen.getByLabelText('Markdown files') as HTMLInputElement
}

const files = [new File(['# a'], 'a.md'), new File(['# b'], 'b.md')]

test('the picker takes several .md files and the result line says what happened', async () => {
  const send = vi
    .spyOn(api, 'importNotes')
    .mockResolvedValue({ created: 2, linked_papers: 1, unmatched_files: 1, skipped_duplicates: 0 })
  const input = setup()
  expect(input.multiple).toBe(true)
  expect(input.accept).toBe('.md,text/markdown')
  fireEvent.change(input, { target: { files } })
  expect(await screen.findByText('2 notes added, 1 linked to papers, 1 without a match.')).toBeTruthy()
  expect(send).toHaveBeenCalledWith(files)
})

test('a refusal shows in plain words', async () => {
  vi.spyOn(api, 'importNotes').mockRejectedValue(new Error('a.md is not UTF-8 text.'))
  fireEvent.change(setup(), { target: { files } })
  expect((await screen.findByRole('alert')).textContent).toContain('a.md is not UTF-8 text.')
})
