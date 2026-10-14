import { describe, expect, it } from 'vitest'
import { ALL_NOTES, emptyNotesText, filterOptions, filterValue, routeFilter } from './notesPage'

describe('the Notes page filter', () => {
  it('offers All notes, No paper, then the library’s papers by title, then id', () => {
    const papers = [
      { id: 'p2', title: 'BERT' },
      { id: 'p1', title: 'Attention' },
      { id: 'p0', title: 'BERT' },
    ]
    expect(filterOptions(papers)).toEqual([
      { value: 'all', label: 'All notes' },
      { value: 'none', label: 'No paper' },
      { value: 'p1', label: 'Attention' },
      { value: 'p0', label: 'BERT' },
      { value: 'p2', label: 'BERT' },
    ])
  })

  it('maps the route’s filter to the select and back', () => {
    expect([filterValue(null), filterValue('none'), filterValue('p1')]).toEqual([ALL_NOTES, 'none', 'p1'])
    expect([routeFilter(ALL_NOTES), routeFilter('none'), routeFilter('p1')]).toEqual([null, 'none', 'p1'])
  })

  it('says, for each filter, that it has no notes', () => {
    expect(emptyNotesText(null)).toBe('No notes yet. Highlight a passage in a paper, or save a note chat suggests.')
    expect(emptyNotesText('none')).toBe('No notes without a paper. A note you take off its last paper shows here.')
    expect(emptyNotesText('p1')).toBe('No notes on this paper yet.')
  })
})

import { importSummary } from './notesPage'

describe('importSummary', () => {
  it('says what was added, linked and left without a match', () => {
    expect(importSummary({ created: 3, linked_papers: 2, unmatched_files: 1, skipped_duplicates: 0 })).toBe(
      '3 notes added, 2 linked to papers, 1 without a match.',
    )
    expect(importSummary({ created: 1, linked_papers: 1, unmatched_files: 0, skipped_duplicates: 0 })).toBe(
      '1 note added, 1 linked to papers.',
    )
  })

  it('mentions files already in the notes, and says so when nothing is new', () => {
    expect(importSummary({ created: 0, linked_papers: 0, unmatched_files: 0, skipped_duplicates: 2 })).toBe(
      'No new notes. 2 files were already in your notes.',
    )
  })
})
