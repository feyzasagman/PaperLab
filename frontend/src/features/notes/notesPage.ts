import type { NotesImportResult, Paper } from '@/api/client'

/** The filter select's value for every note: a select item can't have an empty value. */
export const ALL_NOTES = 'all'

/** The select's value for the route's filter, and back. 'none' and a paper id are the same in both. */
export const filterValue = (paper: string | null): string => paper ?? ALL_NOTES
export const routeFilter = (value: string): string | null => (value === ALL_NOTES ? null : value)

/** All notes, No paper, then the library's papers by title (then id, so two copies of a paper keep one order). */
export function filterOptions(papers: Pick<Paper, 'id' | 'title'>[]): { value: string; label: string }[] {
  const byTitle = papers.toSorted((a, b) => a.title.localeCompare(b.title) || a.id.localeCompare(b.id))
  return [
    { value: ALL_NOTES, label: 'All notes' },
    { value: 'none', label: 'No paper' },
    ...byTitle.map((paper) => ({ value: paper.id, label: paper.title })),
  ]
}

/** What the page says when the filter has no notes: how notes get there, never a blank page. */
export function emptyNotesText(paper: string | null): string {
  if (paper === null) return 'No notes yet. Highlight a passage in a paper, or save a note chat suggests.'
  if (paper === 'none') return 'No notes without a paper. A note you take off its last paper shows here.'
  return 'No notes on this paper yet.'
}

const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** The line under the Import button: what was added, linked to papers and left without a match. */
export function importSummary(r: NotesImportResult): string {
  const skipped =
    r.skipped_duplicates > 0
      ? `${count(r.skipped_duplicates, 'file was', 'files were')} already in your notes.`
      : null
  if (r.created === 0) return ['No new notes.', skipped].filter(Boolean).join(' ')
  const parts = [`${count(r.created, 'note', 'notes')} added`]
  if (r.linked_papers > 0) parts.push(`${r.linked_papers} linked to papers`)
  if (r.unmatched_files > 0) parts.push(`${r.unmatched_files} without a match`)
  return [`${parts.join(', ')}.`, skipped].filter(Boolean).join(' ')
}
