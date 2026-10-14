import type { Paper } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { SOURCE_TYPES } from './paperForm'

const EVIDENCE: Record<Paper['evidence_access'], string> = {
  full_text: 'Full text',
  abstract: 'Abstract only',
  metadata: 'Metadata only',
}

/** "Peer-reviewed", "Preprint · not peer-reviewed", or nothing while it is unknown: a guess would be worse than silence. */
function peerReviewLabel(paper: Paper): string | null {
  if (paper.peer_reviewed === 'true') return 'Peer-reviewed'
  if (paper.peer_reviewed === 'false') return paper.source_type === 'preprint' ? 'Preprint · not peer-reviewed' : 'Not peer-reviewed'
  return null
}

/** Where a paper comes from and how much of it the library can read; one set of chips for the list rows and the reader. */
export function SourceBadges({ paper }: { paper: Paper }) {
  const type = SOURCE_TYPES.find((t) => t.value === paper.source_type)?.label
  const review = peerReviewLabel(paper)
  return (
    <>
      {type && <Badge variant="secondary" className="source-type font-normal">{type}</Badge>}
      {review && <Badge variant="outline" className="peer-review font-normal">{review}</Badge>}
      <Badge variant="outline" className="evidence-access font-normal">{EVIDENCE[paper.evidence_access]}</Badge>
    </>
  )
}
