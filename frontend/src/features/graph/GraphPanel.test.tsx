// @vitest-environment jsdom
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import type { GraphLink, GraphNode } from '@/api/client'
import { GraphPanel } from './GraphPanel'

const node = (id: string, title: string) => ({ id, title, year: null, workspaces: [], has_notes: false, status: 'ready' }) as unknown as GraphNode
const nodes = [node('a', 'Alpha'), node('b', 'Beta'), node('c', 'Gamma')]
const links = [{ source: 'a', target: 'b', kind: 'manual', label: null, id: 'l1' }] as unknown as GraphLink[]
const base = {
  nodes,
  links,
  hops: 1,
  onFocus: () => {},
  onClear: () => {},
  onAddLink: () => {},
  onEditLink: () => {},
  onRemoveLink: () => false,
}

it('steps through the linked papers with Previous and Next, and says where it is', () => {
  const onStep = vi.fn()
  render(<GraphPanel {...base} focused={nodes[1]} stepping={{ from: 'Alpha', position: 1, count: 2 }} onStep={onStep} />)
  const group = screen.getByRole('group', { name: 'Step through linked papers' })
  fireEvent.click(screen.getByRole('button', { name: 'Next linked paper' }))
  fireEvent.click(screen.getByRole('button', { name: 'Previous linked paper' }))
  expect(onStep.mock.calls).toEqual([[1], [-1]])
  expect(group).toBeInTheDocument()
  expect(screen.getByText('Linked to Alpha: 1 of 2')).toBeInTheDocument()
})

it('offers no stepping for a paper with no links, and no position line before the first step', () => {
  const { rerender } = render(<GraphPanel {...base} focused={nodes[2]} stepping={null} onStep={() => {}} />)
  expect(screen.queryByRole('group', { name: 'Step through linked papers' })).toBeNull()
  rerender(<GraphPanel {...base} focused={nodes[0]} stepping={{ from: null, position: 0, count: 1 }} onStep={() => {}} />)
  expect(screen.getByRole('group', { name: 'Step through linked papers' })).toBeInTheDocument()
  expect(screen.queryByText(/^Linked to/)).toBeNull()
})
