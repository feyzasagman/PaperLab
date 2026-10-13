import { useState } from 'react'
import type { Hit } from '@/api/client'
import { Badge } from '@/components/ui/badge'

const FLAG_WORDS: Record<string, string> = {
  no_abstract: 'no abstract',
  quote_dropped: "a quote didn't match",
  model_error: 'model error',
}

/** The model's suggestion for one hit: a muted outline badge, the same visual language as the "Found by" source
 * badges — informational, never a button or a colour-coded verdict. The reader decides. Beside it, in words: the
 * score, a low confidence, and anything that went wrong. The note shows on hover. */
export function SuggestionChip({ hit }: { hit: Hit }) {
  if (!hit.suggestion) return null
  const text =
    hit.suggestion === 'unsure'
      ? 'Unsure'
      : hit.suggestion === 'exclude'
        ? `Suggests: exclude · ${(hit.suggestion_reason ?? 'other').replaceAll('_', ' ')}`
        : 'Suggests: include'
  const flags = (hit.suggestion_flags ?? []).map((flag) => FLAG_WORDS[flag] ?? flag).join(' · ')
  return (
    <span className="flex shrink-0 items-center gap-1.5">
      <Badge variant="outline" title={hit.suggestion_note ?? undefined} className="shrink-0 font-normal text-muted-foreground">
        {text}
      </Badge>
      {hit.suggestion_relevance != null && (
        <span className="text-xs text-muted-foreground">Score {hit.suggestion_relevance}/5</span>
      )}
      {hit.suggestion_confidence === 'low' && <span className="text-xs text-muted-foreground">low confidence</span>}
      {flags && <span className="text-xs text-muted-foreground">{flags}</span>}
    </span>
  )
}

/** "Why": the passages the model quoted from the abstract, each in quotation marks, behind a disclosure. */
export function SuggestionWhy({ hit }: { hit: Hit }) {
  const [open, setOpen] = useState(false)
  const quotes = hit.suggestion_quotes ?? []
  if (quotes.length === 0) return null
  return (
    <div className="text-xs text-muted-foreground">
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="underline underline-offset-2">
        Why
      </button>
      {open && (
        <ul className="mt-1 flex flex-col gap-1">
          {quotes.map((quote) => (
            <li key={quote}>“{quote}”</li>
          ))}
        </ul>
      )}
    </div>
  )
}
