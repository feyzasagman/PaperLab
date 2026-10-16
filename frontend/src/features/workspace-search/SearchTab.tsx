import { useState } from 'react'
import { useContinueSearchRun, useSearchRun, useStartSearchRun, useStopSearchRun } from '@/api/queries'
import { Scene3D } from '@/features/scene3d/Scene3D'
import type { SearchRunStatus } from '@/features/scene3d/poses'
import { SOURCE_NAMES } from '@/features/discovery/candidateMeta'
import { SearchControls } from './SearchControls'
import { HitTable } from './HitTable'
import { SavedSearches } from './SavedSearches'
import { RunsList } from './RunsList'
import { Button } from '@/components/ui/button'
import { continueLabel, continueRefusal, pausedText } from './continueText'
import { ScreeningAssist } from './ScreeningAssist'

/** The sources that failed on a run, each with its error text when the server gave a string; tolerant of bad shapes. */
function failedSources(stats: Record<string, unknown> | null | undefined): { id: string; error?: string }[] {
  const failed = stats?.failed_sources
  const errors = stats?.per_source_error
  const table = errors && typeof errors === 'object' && !Array.isArray(errors) ? (errors as Record<string, unknown>) : {}
  return (Array.isArray(failed) ? failed.filter((x): x is string => typeof x === 'string') : []).map((id) => {
    const error = Object.hasOwn(table, id) ? table[id] : undefined
    const name = Object.hasOwn(SOURCE_NAMES, id) ? SOURCE_NAMES[id as keyof typeof SOURCE_NAMES] : id
    return { id: name, error: typeof error === 'string' ? error : undefined }
  })
}

// Module level, so the scene never restarts on a re-render.
const loadSearchRunScene = () => import('@/features/scene3d/directors/searchRun')

/** `runId` lives in the URL (I1), not local state, so a reload keeps the active run instead of losing it — the
 * caller (WorkspacePage) reads it from the route and reports a new one back via `onRunIdChange`. */
export function SearchTab({
  workspaceId,
  runId,
  onRunIdChange,
}: {
  workspaceId: string
  runId: string | null
  onRunIdChange: (runId: string) => void
}) {
  const startRun = useStartSearchRun(workspaceId)
  const stopRun = useStopSearchRun(workspaceId)
  const continueRun = useContinueSearchRun(workspaceId)
  const run = useSearchRun(workspaceId, runId)
  const [query, setQuery] = useState('')

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <SearchControls
        isRunning={run.data?.status === 'running'}
        onQueryChange={setQuery}
        error={startRun.error?.message}
        onStart={(args) => startRun.mutate(args, { onSuccess: (created) => onRunIdChange(created.id) })}
        onStop={() => runId && stopRun.mutate(runId)}
      />
      <SavedSearches workspaceId={workspaceId} currentQuery={query} onRunStarted={onRunIdChange} />
      <RunsList workspaceId={workspaceId} runId={runId} onSelect={onRunIdChange} />
      <ScreeningAssist workspaceId={workspaceId} />
      {run.data && (
        <>
        <div className="flex items-center gap-2 border-b px-3 py-1 text-sm text-muted-foreground">
          <Scene3D
            load={loadSearchRunScene}
            input={{ status: run.data.status as SearchRunStatus }}
            fallback={null}
            className="size-8"
          />
          {pausedText(run.data) ?? run.data.status} · {(run.data.stats_json?.last_batch_new_hits as number | undefined) ?? 0} new in last
          batch
          {run.data.stats_json?.cut_short === true && run.data.stopped_at && (
            <span className="ml-2">
              Stopped early after {Math.round((Date.parse(run.data.stopped_at) - Date.parse(run.data.started_at)) / 60000)} min —{' '}
              {((run.data.stats_json.unfinished_sources as string[] | undefined) ?? []).length} sources not finished
            </span>
          )}
          {run.data.can_continue && (
            <Button
              variant="outline"
              size="sm"
              className="ml-auto"
              aria-label={continueLabel(run.data.sources_left)}
              disabled={continueRun.isPending}
              onClick={() => continueRun.mutate(run.data.id)}
            >
              Continue
            </Button>
          )}
        </div>
        {continueRun.error && continueRun.variables === run.data.id && (
          <div role="alert" className="border-b px-3 py-1 text-sm text-destructive">
            {continueRefusal(continueRun.error.message)}
          </div>
        )}
        {failedSources(run.data.stats_json).map(({ id, error }) => (
          <div key={id} className="border-b px-3 py-1 text-sm text-muted-foreground">
            {id} didn't answer{error ? `: ${error}` : ''}
          </div>
        ))}
        </>
      )}
      <HitTable workspaceId={workspaceId} run={run.data} />
    </div>
  )
}
