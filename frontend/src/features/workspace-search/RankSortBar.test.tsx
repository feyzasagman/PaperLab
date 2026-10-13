// @vitest-environment jsdom
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { RankedHits } from '@/api/client'
import { RankSortBar } from './RankSortBar'

const ranked = (over = {}) => ({
  items: [], trained: true, total_unscreened: 2710, streak: 12, threshold: 210, show_stop_hint: false, ...over,
})

describe('RankSortBar', () => {
  it('switches between found order and most likely relevant first', () => {
    const onSortChange = vi.fn()
    render(<RankSortBar sort="found" onSortChange={onSortChange} />)
    fireEvent.change(screen.getByLabelText('Sort hits'), { target: { value: 'ranked' } })
    expect(onSortChange).toHaveBeenCalledWith('ranked')
  })

  it('explains the cold start before the ranking has learned anything', () => {
    render(<RankSortBar sort="ranked" onSortChange={() => {}} ranked={ranked({ trained: false })} />)
    expect(
      screen.getByText("Ranking learns once you've marked at least one hit relevant and one not relevant."),
    ).toBeInTheDocument()
  })

  it('says how many it is showing once trained', () => {
    const items = Array.from({ length: 200 }, (_, i) => ({ id: String(i) }))
    render(<RankSortBar sort="ranked" onSortChange={() => {}} ranked={ranked({ items }) as RankedHits} />)
    expect(screen.getByText('Showing the 200 most likely relevant of 2710 unscreened.')).toBeInTheDocument()
  })

  it('shows the stop hint as a quiet status, never a dialog', () => {
    render(<RankSortBar sort="ranked" onSortChange={() => {}} ranked={ranked({ streak: 210, show_stop_hint: true })} />)
    expect(screen.getByRole('status')).toHaveTextContent(
      'Your last 210 decisions were all not relevant. You may have found most of the relevant papers.',
    )
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('shows no ranking lines in found order', () => {
    render(<RankSortBar sort="found" onSortChange={() => {}} ranked={ranked({ show_stop_hint: true })} />)
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('offers the AI score order and a Score filter', () => {
    const onSortChange = vi.fn()
    const onMinRelevanceChange = vi.fn()
    render(<RankSortBar sort="found" onSortChange={onSortChange} minRelevance={null} onMinRelevanceChange={onMinRelevanceChange} />)
    fireEvent.change(screen.getByLabelText('Sort hits'), { target: { value: 'ai' } })
    expect(onSortChange).toHaveBeenCalledWith('ai')
    fireEvent.change(screen.getByLabelText('Score ≥'), { target: { value: '4' } })
    expect(onMinRelevanceChange).toHaveBeenCalledWith(4)
    fireEvent.change(screen.getByLabelText('Score ≥'), { target: { value: '' } })
    expect(onMinRelevanceChange).toHaveBeenCalledWith(null)
  })
})
