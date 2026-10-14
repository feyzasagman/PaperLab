import { useState } from 'react'
import type { Note } from '@/api/client'
import { useAllNotes, useNoteMutations, usePapers } from '@/api/queries'
import { AppShell } from '@/components/AppShell'
import { delayedIn } from '@/components/motion'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { plural } from '@/features/charts/chartMeta'
import { ErrorAlert, LoadError } from '@/features/library/ErrorAlert'
import { notesHref } from '@/lib/route'
import { cn } from '@/lib/utils'
import { ImportMarkdownButton } from './ImportMarkdownButton'
import { NoteCard } from './NoteCard'
import { emptyNotesText, filterOptions, filterValue, routeFilter } from './notesPage'

/** Every note, filtered by paper or "No paper" (D98): where a note on no paper is read, edited and relinked. */
export function NotesPage({ paper }: { paper: string | null }) {
  const notes = useAllNotes(paper)
  const papers = usePapers()
  const mutations = useNoteMutations()
  const [error, setError] = useState<string | null>(null)

  /** Runs a note action; a refusal shows in the alert instead of throwing. */
  async function attempt(action: () => Promise<unknown>): Promise<boolean> {
    try {
      await action()
      setError(null)
      return true
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      return false
    }
  }

  async function remove(note: Note) {
    if (!window.confirm('Delete this note?')) return
    await attempt(() => mutations.remove.mutateAsync(note.id))
  }

  function showFilter(value: string) {
    window.location.hash = notesHref(routeFilter(value)) // assign, not replace: Back walks back through filters
  }

  return (
    <AppShell
      title="Notes"
      actions={
        <div className="flex items-center gap-2">
          <Label htmlFor="notes-paper" className="text-sm font-medium">
            Paper
          </Label>
          <Select value={filterValue(paper)} onValueChange={showFilter}>
            <SelectTrigger id="notes-paper" className="w-64">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {filterOptions(papers.data ?? []).map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      }
      status={notes.data && plural(notes.data.length, 'note')}
    >
      <div className="flex max-w-5xl flex-col gap-4">
        <ImportMarkdownButton />
        {error && <ErrorAlert message={error} />}
        {notes.data === undefined ? (
          notes.isError ? (
            <LoadError message={notes.error.message} onRetry={() => void notes.refetch()} />
          ) : (
            <p className={cn('text-muted-foreground', delayedIn)}>Loading…</p>
          )
        ) : notes.data.length === 0 ? (
          <p className="notes-empty text-muted-foreground">{emptyNotesText(paper)}</p>
        ) : (
          <ul className="grid items-start gap-3 md:grid-cols-2">
            {notes.data.map((note) => (
              <li key={note.id}>
                <NoteCard
                  note={note}
                  onUpdate={(body) => attempt(() => mutations.update.mutateAsync({ id: note.id, body }))}
                  onColorChange={(hex) => attempt(() => mutations.update.mutateAsync({ id: note.id, color: hex }))}
                  onDelete={() => remove(note)}
                />
              </li>
            ))}
          </ul>
        )}
      </div>
    </AppShell>
  )
}
