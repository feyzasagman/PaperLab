import { useState } from 'react'
import { useDeleteSavedSearch, useRunSavedSearchNow, useSavedSearches, useUpdateSavedSearch } from '@/api/queries'
import type { SavedSearch } from '@/api/client'
import { glass } from '@/components/glass'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Switch } from '@/components/ui/switch'
import { cn } from '@/lib/utils'
import { SavedSearchDialog } from './SavedSearchDialog'
import { describeSchedule, describeYears } from './schedule'

const localTime = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })

function relative(iso: string): string {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  if (Math.abs(minutes) < 60) return rtf.format(-minutes, 'minute')
  if (Math.abs(minutes) < 1440) return rtf.format(-Math.round(minutes / 60), 'hour')
  return rtf.format(-Math.round(minutes / 1440), 'day')
}

type Editing = SavedSearch | 'new' | null

/** The workspace's saved (scheduled) searches, under the Start bar. */
export function SavedSearches({
  workspaceId,
  currentQuery,
  onRunStarted,
}: {
  workspaceId: string
  currentQuery: string
  onRunStarted: (runId: string) => void
}) {
  const list = useSavedSearches(workspaceId)
  const update = useUpdateSavedSearch(workspaceId)
  const remove = useDeleteSavedSearch(workspaceId)
  const runNow = useRunSavedSearchNow(workspaceId)
  const [editing, setEditing] = useState<Editing>(null)
  const [deleting, setDeleting] = useState<SavedSearch | null>(null)
  const rows = list.data ?? []

  return (
    <section aria-label="Saved searches" className="grid gap-2 border-b px-3 py-2 text-sm">
      <div className="flex items-center justify-between">
        <h2 className="font-medium">Saved searches</h2>
        <Button variant="ghost" size="sm" onClick={() => setEditing('new')}>Save this search…</Button>
      </div>
      {rows.map((s) => (
        <div key={s.id} className="grid gap-0.5 rounded-md border px-3 py-2">
          <div className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate font-medium">{s.query}</span>
            <Switch size="sm" checked={s.enabled} aria-label={`Enabled: ${s.query}`}
              onCheckedChange={(enabled) => update.mutate({ id: s.id, body: { enabled } })} />
            <Button variant="ghost" size="sm" onClick={() => runNow.mutate(s.id, { onSuccess: (r) => onRunStarted(r.id) })}>
              Run now
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setEditing(s)}>Edit</Button>
            <Button variant="ghost" size="sm" onClick={() => setDeleting(s)}>Delete</Button>
          </div>
          <p className="text-xs text-muted-foreground">
            {[
              describeSchedule(s),
              s.sources.length ? `${s.sources.length} sources` : 'all sources',
              describeYears(s.year_from, s.year_to),
            ].filter(Boolean).join(' · ')}
          </p>
          <p className="text-xs text-muted-foreground">
            Last run: {s.last_run_at ? `${relative(s.last_run_at)}${s.last_run_status ? ` (${s.last_run_status})` : ''}` : 'never'}
            {' · '}
            <span>{s.enabled && s.next_run_at ? `Next run: ${localTime(s.next_run_at)}` : 'Paused'}</span>
          </p>
          {s.last_error && <p className="text-xs text-muted-foreground">{s.last_error}</p>}
        </div>
      ))}

      {editing && (
        <SavedSearchDialog
          workspaceId={workspaceId}
          saved={editing === 'new' ? null : editing}
          initialQuery={currentQuery}
          onClose={() => setEditing(null)}
        />
      )}
      <Dialog open={deleting !== null} onOpenChange={(open) => !open && setDeleting(null)}>
        <DialogContent className={cn(glass, 'bg-glass-strong ring-glass-border sm:max-w-sm')}>
          <DialogHeader>
            <DialogTitle>Delete saved search?</DialogTitle>
            <DialogDescription>
              “{deleting?.query}” will stop running. Papers it already found stay where they are.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="bg-transparent">
            <Button variant="outline" onClick={() => setDeleting(null)}>Cancel</Button>
            <Button variant="destructive" onClick={() => { remove.mutate(deleting!.id); setDeleting(null) }}>Delete</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  )
}
