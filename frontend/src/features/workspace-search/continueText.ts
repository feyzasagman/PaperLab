import { plural } from '@/features/charts/chartMeta'

type Continuable = { status: string; sources_left?: string[]; paused_sources?: string[] }

/** The accessible name of a Continue button. */
export const continueLabel = (sourcesLeft: string[] = []) =>
  `Continue: fetch the next results from ${plural(sourcesLeft.length, 'source')}`

/** Plain words for a pass that stopped at the per-source cap; null when the run isn't paused. */
export function pausedText(run: Continuable): string | null {
  if (run.status !== 'exhausted' || !run.paused_sources?.length) return null
  return `Paused at 200 results per source; Continue fetches more from ${plural((run.sources_left ?? []).length, 'source')}`
}
