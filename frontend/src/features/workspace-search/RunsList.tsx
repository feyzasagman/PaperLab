import { useState } from 'react'
import { useContinueSearchRun, useSearchRuns } from '@/api/queries'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { plural } from '@/features/charts/chartMeta'
import { relative } from './schedule'
import { continueLabel, pausedText } from './continueText'

/** The workspace's recent search runs, collapsed under Saved searches; picking one opens it (its id goes in the URL). */
export function RunsList({
  workspaceId,
  runId,
  onSelect,
}: {
  workspaceId: string
  runId: string | null
  onSelect: (runId: string) => void
}) {
  const runs = useSearchRuns(workspaceId).data ?? []
  const cont = useContinueSearchRun(workspaceId)
  const [open, setOpen] = useState(false)
  if (runs.length === 0) return null

  return (
    <section aria-label="Runs" className="grid gap-2 border-b px-3 py-2 text-sm">
      <Button variant="ghost" size="sm" className="justify-start" aria-expanded={open} onClick={() => setOpen(!open)}>
        Runs ({runs.length})
      </Button>
      {cont.error && <p role="alert" className="text-sm text-destructive">{cont.error.message}</p>}
      {open &&
        runs.map((r) => (
          <div key={r.id} className="flex items-center gap-2">
            <button
              type="button"
              aria-current={r.id === runId ? 'true' : undefined}
              onClick={() => onSelect(r.id)}
              className={cn('flex min-w-0 flex-1 items-center gap-2 rounded-md border px-3 py-2 text-left hover:bg-muted', r.id === runId && 'bg-muted')}
            >
              <span className="min-w-0 flex-1 truncate font-medium">{r.query_text}</span>
              <span className="text-xs text-muted-foreground">
                {pausedText(r) ?? r.status} · {relative(r.started_at)} · {plural(r.hit_count, 'new hit')}
              </span>
            </button>
            {r.can_continue && (
              <Button
                variant="outline"
                size="sm"
                aria-label={continueLabel(r.sources_left)}
                disabled={cont.isPending}
                onClick={() => cont.mutate(r.id)}
              >
                Continue
              </Button>
            )}
          </div>
        ))}
    </section>
  )
}
