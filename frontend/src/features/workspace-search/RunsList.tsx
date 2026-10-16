import { useState } from 'react'
import { useSearchRuns } from '@/api/queries'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { relative } from './schedule'

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
  const [open, setOpen] = useState(false)
  if (runs.length === 0) return null

  return (
    <section aria-label="Runs" className="grid gap-2 border-b px-3 py-2 text-sm">
      <Button variant="ghost" size="sm" className="justify-start" aria-expanded={open} onClick={() => setOpen(!open)}>
        Runs ({runs.length})
      </Button>
      {open &&
        runs.map((r) => (
          <button
            key={r.id}
            type="button"
            aria-current={r.id === runId ? 'true' : undefined}
            onClick={() => onSelect(r.id)}
            className={cn('flex items-center gap-2 rounded-md border px-3 py-2 text-left hover:bg-muted', r.id === runId && 'bg-muted')}
          >
            <span className="min-w-0 flex-1 truncate font-medium">{r.query_text}</span>
            <span className="text-xs text-muted-foreground">
              {r.status} · {relative(r.started_at)} · {r.hit_count} hits
            </span>
          </button>
        ))}
    </section>
  )
}
