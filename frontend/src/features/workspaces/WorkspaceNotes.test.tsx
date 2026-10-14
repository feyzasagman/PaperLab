// @vitest-environment jsdom
import { render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { WorkspaceNotes } from './WorkspaceNotes'

vi.mock('@/api/queries', () => ({
  useWorkspaceNotes: () => ({ data: [], error: null }),
  useWorkspacePapers: () => ({ data: [], error: null }),
}))

test('the Notes tab offers a Markdown export that downloads the workspace zip', () => {
  render(<WorkspaceNotes workspaceId="w 1" />)
  const link = screen.getByRole('link', { name: 'Export to Markdown' })
  expect(link.getAttribute('href')).toBe('/api/workspaces/w%201/notes.zip')
  expect(link.hasAttribute('download')).toBe(true)
})
