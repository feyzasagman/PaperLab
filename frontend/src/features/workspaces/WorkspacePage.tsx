import { useEffect, useRef } from 'react'
import { useWorkspace } from '@/api/queries'
import { AppShell, shellTitle } from '@/components/AppShell'
import { glass } from '@/components/glass'
import { delayedIn, fadeIn } from '@/components/motion'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ManualAcquisitionTab } from '@/features/workspace-search/ManualAcquisitionTab'
import { PrismaTab } from '@/features/workspace-search/PrismaTab'
import { ReadingQueueTab } from '@/features/workspace-search/ReadingQueueTab'
import { ScreeningTab } from '@/features/workspace-search/ScreeningTab'
import { SearchTab } from '@/features/workspace-search/SearchTab'
import { EvidenceTab } from '../evidence/EvidenceTab'
import { workspaceHref, type WorkspaceTab } from '@/lib/route'
import { cn } from '@/lib/utils'
import { WorkspaceChat } from './WorkspaceChat'
import { WorkspaceNotes } from './WorkspaceNotes'
import { WorkspacePapers } from './WorkspacePapers'
import { countsLine } from './workspaceMeta'

// Every tab stays mounted, so each keeps its scroll position across a switch, and fades in each time it is shown,
// like the reader's panels. `text-base` undoes TabsContent's `text-sm`.
const panel = cn('min-h-0 flex-1 overflow-auto text-base data-[state=inactive]:hidden', fadeIn)

/** A workspace's home: its name and counts over Papers, Notes and Chat tabs, inside the app shell. `runId` is the
 * active search run, read from the URL (I1) so SearchTab survives a reload instead of resetting to no run. */
export function WorkspacePage({ workspaceId, tab, runId }: { workspaceId: string; tab: WorkspaceTab; runId: string | null }) {
  const workspace = useWorkspace(workspaceId)
  // replace, not assign: switching tabs shouldn't add history entries for Back to walk through. Carries the
  // current runId along so switching tabs and back (or a reload from another tab) doesn't drop it.
  const showTab = (next: WorkspaceTab) => window.location.replace(workspaceHref(workspaceId, next, runId))
  // SearchTab reports a new run id here instead of holding it in local state, so the URL (and a reload) stays in
  // sync with whatever run is actually active.
  const setRunId = (next: string) => window.location.replace(workspaceHref(workspaceId, 'search', next))
  const title = workspace.data?.name ?? (workspace.data === null ? 'Workspace not found' : 'Loading…')

  // The page is keyed by workspace, so opening another one remounts it from scratch and whatever a click or the
  // rail's own "New workspace" flow just focused is gone with the old tree. Land focus on the heading instead: it's
  // the one thing every arrival at this page has in common.
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => headingRef.current?.focus(), [])

  return (
    <AppShell
      fills
      title={
        <h1
          ref={headingRef}
          tabIndex={-1}
          // `focus:` (not `focus-visible:`), so this always shows: it's not Tab-reachable, so the only way here is
          // our own arrival focus or a direct click, and `:focus-visible` can go unset for either depending on
          // what the user did just before landing (a mouse click earlier in the flow is enough to suppress it).
          className={cn(
            shellTitle,
            'rounded-sm py-0.5 outline-none focus:ring-3 focus:ring-ring/50',
            workspace.data === undefined && delayedIn,
          )}
          title={title}
        >
          {title}
        </h1>
      }
      status={workspace.data && countsLine(workspace.data.paper_count, workspace.data.note_count)}
    >
      {workspace.error && (
        <Alert variant="destructive" className={cn('self-start border-glass-border', glass)}>
          <AlertDescription>{workspace.error.message}</AlertDescription>
        </Alert>
      )}
      {workspace.data === null && (
        <p className="text-muted-foreground">This workspace doesn't exist. It may have been deleted.</p>
      )}
      {workspace.data && (
        <Tabs value={tab} onValueChange={(value) => showTab(value as WorkspaceTab)} className="min-h-0 min-w-0 flex-1 gap-3">
          <TabsList>
            <TabsTrigger value="papers">Papers</TabsTrigger>
            <TabsTrigger value="notes">Notes</TabsTrigger>
            <TabsTrigger value="chat">Chat</TabsTrigger>
            {/* Everything left of here is a library tool; everything right is one pipeline, in order:
                find hits, get a PDF, screen it, export the funnel, read what made it through. */}
            <div aria-hidden className="mx-1 w-px self-stretch bg-border" />
            <TabsTrigger value="search">Search</TabsTrigger>
            <TabsTrigger value="acquisition">Acquisition</TabsTrigger>
            <TabsTrigger value="screening">Screening</TabsTrigger>
            <TabsTrigger value="prisma">PRISMA</TabsTrigger>
            <TabsTrigger value="reading">Reading</TabsTrigger>
            <TabsTrigger value="evidence">Evidence</TabsTrigger>
          </TabsList>
          <TabsContent value="papers" forceMount className={panel}>
            <WorkspacePapers workspaceId={workspaceId} />
          </TabsContent>
          <TabsContent value="notes" forceMount className={panel}>
            <WorkspaceNotes workspaceId={workspaceId} />
          </TabsContent>
          {/* The chat panel scrolls its own answer list and keeps the question box in view. */}
          <TabsContent value="chat" forceMount className={cn(panel, 'flex flex-col overflow-hidden')}>
            <WorkspaceChat workspace={workspace.data} onShowNotes={() => showTab('notes')} />
          </TabsContent>
          {/* SearchTab's HitTable virtualizes its own row list — it needs to measure a real, bounded viewport
              to know which rows are actually visible. Without flex/overflow-hidden here (mirroring the chat
              panel above), `panel`'s plain overflow-auto never actually clips anything: nothing constrains
              HitTable's own scroll container to a real pixel height, so it grows to fit every loaded row, the
              virtualizer renders all of them as real DOM nodes instead of just the visible ones, and "am I
              scrolled to the bottom" is trivially always true. */}
          <TabsContent value="search" forceMount className={cn(panel, 'flex flex-col overflow-hidden')}>
            <SearchTab workspaceId={workspaceId} runId={runId} onRunIdChange={setRunId} />
          </TabsContent>
          <TabsContent value="acquisition" forceMount className={panel}>
            <ManualAcquisitionTab workspaceId={workspaceId} />
          </TabsContent>
          {/* ScreeningTab and PrismaTab both root a `flex min-h-0 flex-1 flex-col` layout of their own (like
              WorkspaceChat/SearchTab above), so they need the same bounded viewport here, not `panel`'s plain
              overflow-auto. */}
          <TabsContent value="screening" forceMount className={cn(panel, 'flex flex-col overflow-hidden')}>
            <ScreeningTab workspaceId={workspaceId} />
          </TabsContent>
          <TabsContent value="prisma" forceMount className={cn(panel, 'flex flex-col overflow-hidden')}>
            <PrismaTab workspaceId={workspaceId} />
          </TabsContent>
          <TabsContent value="reading" forceMount className={panel}>
            <ReadingQueueTab workspaceId={workspaceId} runId={runId} />
          </TabsContent>
          <TabsContent value="evidence" forceMount className={panel}>
            <EvidenceTab workspaceId={workspaceId} />
          </TabsContent>
        </Tabs>
      )}
    </AppShell>
  )
}
