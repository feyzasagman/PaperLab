import { plural } from '@/features/charts/chartMeta'

type Continuable = { status: string; sources_left?: string[]; paused_sources?: string[] }

/** The accessible name of a Continue button. */
export const continueLabel = (sourcesLeft: string[] = []) =>
  `Continue: fetch the next results from ${plural(sourcesLeft.length, 'source')}`

/** Plain words for a pass that stopped at the per-source cap; null when the run isn't paused. */
export function pausedText(run: Continuable): string | null {
  if (run.status !== 'exhausted' || !run.paused_sources?.length) return null
  return `Paused at 200 results per source per pass; Continue fetches more from ${plural((run.sources_left ?? []).length, 'source')}`
}

const REFUSALS: Record<string, string> = {
  nothing_left_to_fetch: 'Every source is already finished.',
  search_run_already_running: 'This search is already running.',
  search_run_still_stopping: 'Still stopping the last pass; try again in a few seconds.',
}

/** A refused Continue in plain words; an unknown message stays as it came. */
export const continueRefusal = (message: string) => REFUSALS[message] ?? message
