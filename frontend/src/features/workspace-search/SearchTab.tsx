import { useState } from 'react'
import { useSearchRun, useStartSearchRun, useStopSearchRun } from '@/api/queries'
import { Scene3D } from '@/features/scene3d/Scene3D'
import type { SearchRunStatus } from '@/features/scene3d/poses'
import { SearchControls } from './SearchControls'
import { HitTable } from './HitTable'
import { SavedSearches } from './SavedSearches'
import { ScreeningAssist } from './ScreeningAssist'

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
  const run = useSearchRun(workspaceId, runId)
  const [query, setQuery] = useState('')

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <SearchControls
        isRunning={run.data?.status === 'running'}
        onQueryChange={setQuery}
        onStart={(args) => startRun.mutate(args, { onSuccess: (created) => onRunIdChange(created.id) })}
        onStop={() => runId && stopRun.mutate(runId)}
      />
      <SavedSearches workspaceId={workspaceId} currentQuery={query} onRunStarted={onRunIdChange} />
      <ScreeningAssist workspaceId={workspaceId} />
      {run.data && (
        <div className="flex items-center gap-2 border-b px-3 py-1 text-sm text-muted-foreground">
          <Scene3D
            load={loadSearchRunScene}
            input={{ status: run.data.status as SearchRunStatus }}
            fallback={null}
            className="size-8"
          />
          {run.data.status} · {(run.data.stats_json?.last_batch_new_hits as number | undefined) ?? 0} new in last
          batch
        </div>
      )}
      <HitTable workspaceId={workspaceId} run={run.data} />
    </div>
  )
}
