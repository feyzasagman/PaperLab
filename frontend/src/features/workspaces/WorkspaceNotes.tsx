import { Download } from 'lucide-react'
import { notesZipUrl, type Note } from '@/api/client'
import { useWorkspaceNotes, useWorkspacePapers } from '@/api/queries'
import { glass } from '@/components/glass'
import { delayedIn, isFresh, slideUpIn } from '@/components/motion'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { ProvenanceBadge } from '@/features/notes/ProvenanceBadge'
import { noteHref } from '@/lib/route'
import { cn } from '@/lib/utils'
import { notesByPaper } from './workspaceMeta'

/** A read-only note: the whole card opens the reader focused on it. Editing happens there. */
function WorkspaceNote({ note, paperId }: { note: Note; paperId: string }) {
  const anchor = note.anchors.find((a) => a.paper_id === paperId)
  return (
    <a
      href={noteHref(paperId, note.id)}
      data-note-id={note.id}
      // A note saved from the Chat tab a moment ago fades up once, like a new card in the reader.
      className={cn(
        'workspace-note block h-full rounded-xl outline-none focus-visible:ring-3 focus-visible:ring-ring/50',
        isFresh(note.created_at) && slideUpIn,
      )}
    >
      {/* Provenance is never subtle: the badge, and AI text on its own surface. */}
      <Card
        size="sm"
        className={cn(
          'h-full bg-glass-strong ring-glass-border transition-shadow duration-150 hover:ring-primary/60',
          note.provenance !== 'human' && 'bg-provenance-llm-surface',
        )}
      >
        <CardHeader className="flex items-center justify-between">
          <ProvenanceBadge provenance={note.provenance} />
          {anchor && <span className="text-xs text-muted-foreground">p. {anchor.page}</span>}
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {anchor && (
            <blockquote className="line-clamp-2 border-l-2 pl-2 text-muted-foreground" title={anchor.quoted_text}>
              {anchor.quoted_text}
            </blockquote>
          )}
          {note.body && <p className="line-clamp-6 whitespace-pre-wrap">{note.body}</p>}
        </CardContent>
      </Card>
    </a>
  )
}

function ExportLink({ workspaceId }: { workspaceId: string }) {
  return (
    <Button asChild variant="outline" size="sm" className="self-start">
      <a href={notesZipUrl(workspaceId)} download>
        <Download aria-hidden /> Export to Markdown
      </a>
    </Button>
  )
}

/** The Notes tab: every note on the workspace's papers, grouped by paper. */
export function WorkspaceNotes({ workspaceId }: { workspaceId: string }) {
  const notes = useWorkspaceNotes(workspaceId)
  const papers = useWorkspacePapers(workspaceId)
  const error = notes.error ?? papers.error

  if (error) {
    return (
      <Alert variant="destructive" className={cn('border-glass-border', glass)}>
        <AlertDescription>{error.message}</AlertDescription>
      </Alert>
    )
  }
  if (!notes.data || !papers.data) return <p className={cn('py-2 text-muted-foreground', delayedIn)}>Loading…</p>

  const groups = notesByPaper(notes.data, papers.data)
  if (groups.length === 0) {
    return (
      <div className="flex flex-col gap-3 py-2">
        <p className="text-muted-foreground">No notes yet. Highlight a passage in one of these papers to add one.</p>
        <ExportLink workspaceId={workspaceId} />
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-6 py-2">
      <ExportLink workspaceId={workspaceId} />
      {groups.map((group) => (
        <section key={group.paper.id} data-paper-id={group.paper.id} className="flex flex-col gap-2">
          <h2 className="truncate font-heading text-xl font-semibold" title={group.paper.title}>
            {group.paper.title}
          </h2>
          <ul className="grid gap-3 md:grid-cols-2">
            {group.notes.map((note) => (
              <li key={note.id}>
                <WorkspaceNote note={note} paperId={group.paper.id} />
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}
