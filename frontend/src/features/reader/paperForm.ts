import type { Paper, PaperUpdate } from '@/api/client'

export type SourceType = NonNullable<Paper['source_type']>
export type PeerReviewed = Paper['peer_reviewed']

/** The eight source types, in the order the select lists them. */
export const SOURCE_TYPES: { value: SourceType; label: string }[] = [
  { value: 'journal_article', label: 'Journal article' },
  { value: 'proceedings', label: 'Conference paper' },
  { value: 'book', label: 'Book' },
  { value: 'chapter', label: 'Book chapter' },
  { value: 'preprint', label: 'Preprint' },
  { value: 'magazine', label: 'Magazine' },
  { value: 'thesis', label: 'Thesis' },
  { value: 'other', label: 'Other' },
]

/** The "Edit details" fields as the inputs hold them: plain strings, one author per line. */
export type PaperForm = {
  title: string
  authors: string
  year: string
  venue: string
  doi: string
  abstract: string
  isRetracted: boolean
  /** '' is "Not set". */
  sourceType: SourceType | ''
  peerReviewed: PeerReviewed
}

export const toForm = (paper: Paper): PaperForm => ({
  title: paper.title,
  authors: paper.authors.join('\n'),
  year: paper.year?.toString() ?? '',
  venue: paper.venue ?? '',
  doi: paper.doi ?? '',
  abstract: paper.abstract ?? '',
  isRetracted: paper.is_retracted,
  sourceType: paper.source_type ?? '',
  peerReviewed: paper.peer_reviewed,
})

const orNull = (value: string) => value.trim() || null

/** Only what the user changed, so the server marks only those fields as corrected. `{}` means nothing changed. */
export function toUpdate(paper: Paper, form: PaperForm): PaperUpdate {
  const authors = form.authors.split('\n').map((name) => name.trim()).filter(Boolean)
  const year = form.year.trim() ? Number(form.year) : null
  const update: PaperUpdate = {}
  // Compare against the stored values trimmed too, so an untouched but padded stored value isn't sent as a change.
  if (form.title.trim() !== paper.title.trim()) update.title = form.title.trim()
  if (authors.join('\n') !== paper.authors.join('\n')) update.authors = authors
  if (year !== paper.year) update.year = year
  if (orNull(form.venue) !== orNull(paper.venue ?? '')) update.venue = orNull(form.venue)
  if (orNull(form.doi) !== orNull(paper.doi ?? '')) update.doi = orNull(form.doi)
  if (orNull(form.abstract) !== orNull(paper.abstract ?? '')) update.abstract = orNull(form.abstract)
  if (form.isRetracted !== paper.is_retracted) update.is_retracted = form.isRetracted
  if (form.sourceType !== (paper.source_type ?? '')) update.source_type = form.sourceType || null
  if (form.peerReviewed !== paper.peer_reviewed) update.peer_reviewed = form.peerReviewed
  return update
}
