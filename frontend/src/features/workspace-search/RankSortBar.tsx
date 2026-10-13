import { Info } from 'lucide-react'
import type { RankedHits } from '@/api/client'
import type { HitSort } from './hitSort'

/** The hit pool's sort choice and, in ranked order, what the ranking knows so far (M31a). Text only: the stop hint
 * never stops, hides or decides anything (spec §3.2). */
export function RankSortBar({
  sort,
  onSortChange,
  ranked,
  minRelevance = null,
  onMinRelevanceChange,
}: {
  sort: HitSort
  onSortChange: (sort: HitSort) => void
  ranked?: RankedHits
  minRelevance?: number | null
  onMinRelevanceChange?: (value: number | null) => void
}) {
  return (
    <div className="flex flex-col gap-1.5 border-b px-3 py-1.5 text-xs text-muted-foreground">
      <div className="flex flex-wrap items-center gap-3">
      <label className="flex items-center gap-2">
        <span>Sort hits</span>
        <select
          aria-label="Sort hits"
          value={sort}
          onChange={(event) => onSortChange(event.target.value as HitSort)}
          className="h-7 rounded-md border border-input bg-background px-1.5 text-xs text-foreground outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          <option value="found">Found order</option>
          <option value="ranked">Most likely relevant first</option>
          <option value="year">Newest publication first</option>
          <option value="ai">AI score, highest first</option>
        </select>
      </label>
      {onMinRelevanceChange && sort !== 'ranked' && (
        <label className="flex items-center gap-2">
          <span>Score ≥</span>
          <select
            aria-label="Score ≥"
            value={minRelevance ?? ''}
            onChange={(event) => onMinRelevanceChange(event.target.value ? Number(event.target.value) : null)}
            className="h-7 rounded-md border border-input bg-background px-1.5 text-xs text-foreground outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            <option value="">Any</option>
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      )}
      </div>
      {sort === 'ranked' && ranked && !ranked.trained && (
        <p className="flex items-center gap-1.5">
          <Info aria-hidden className="size-3.5 shrink-0" />
          Ranking learns once you've marked at least one hit relevant and one not relevant.
        </p>
      )}
      {sort === 'ranked' && ranked?.trained && (
        <p>
          Showing the {Math.min(ranked.items.length, 200)} most likely relevant of {ranked.total_unscreened} unscreened.
        </p>
      )}
      {sort === 'ranked' && ranked?.show_stop_hint && (
        <p role="status" className="flex items-center gap-1.5">
          <Info aria-hidden className="size-3.5 shrink-0" />
          Your last {ranked.streak} decisions were all not relevant. You may have found most of the relevant papers.
        </p>
      )}
    </div>
  )
}
