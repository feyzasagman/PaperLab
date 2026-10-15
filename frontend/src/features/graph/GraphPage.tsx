import { useEffect, useMemo, useState } from 'react'
import type { GraphLink } from '@/api/client'
import { useEmbeddingStatus, useLibraryGraph, usePaperLinkMutations, useWorkspaces } from '@/api/queries'
import { AppShell } from '@/components/AppShell'
import { glass } from '@/components/glass'
import { useChartTheme } from '@/features/charts/useChartTheme'
import { ErrorAlert, LoadError } from '@/features/library/ErrorAlert'
import { browserStorage } from '@/features/notes/highlightColors'
import { rebuildLine } from '@/features/settings/searchSources'
import { cn } from '@/lib/utils'
import { GraphControls } from './GraphControls'
import { GraphPanel } from './GraphPanel'
import { GraphViews } from './GraphViews'
import { hopTarget, linkedInOrder } from './paperWalk'
import { LinkDialog, type LinkDraft } from './LinkDialog'
import {
  countsLine,
  DEFAULT_LAYERS,
  focusedIds,
  layerCounts,
  legendEntries,
  visibleLinks,
  workspaceColors,
  type LinkKind,
} from './graphModel'
import type { TimeAxis } from './timelineModel'
import { readView, writeView, type GraphView } from './viewModel'

const EMPTY =
  'No links yet. Import references, add papers to a workspace, or turn on OpenAlex to fill this in.'

export function GraphPage() {
  const [workspaceId, setWorkspaceId] = useState<string | null>(null)
  const [layers, setLayers] = useState<LinkKind[]>(DEFAULT_LAYERS)
  const [focusId, setFocusId] = useState<string | null>(null)
  // The paper stepping started from: Next and Previous go round its linked papers until the owner opens a paper
  // some other way.
  const [stepFrom, setStepFrom] = useState<string | null>(null)
  const [hops, setHops] = useState(1)
  const [dialog, setDialog] = useState<{ editing: GraphLink | null } | null>(null)
  const [view, setView] = useState<GraphView>(() => readView(browserStorage()))
  const [axis, setAxis] = useState<TimeAxis>('published')
  const theme = useChartTheme()
  const embedding = useEmbeddingStatus()
  const graph = useLibraryGraph(workspaceId)
  const workspaces = useWorkspaces()
  const links = usePaperLinkMutations()

  const nodes = useMemo(() => graph.data?.nodes ?? [], [graph.data])
  const allLinks = useMemo(() => graph.data?.links ?? [], [graph.data])
  const shown = useMemo(() => visibleLinks(allLinks, layers), [allLinks, layers])
  const counts = useMemo(() => layerCounts(allLinks), [allLinks])
  // K20: from every workspace, not the papers on screen, so the workspace filter never recolours one.
  const workspaceNames = useMemo(() => (workspaces.data ?? []).map((workspace) => workspace.name), [workspaces.data])
  const colors = useMemo(() => workspaceColors(workspaceNames, theme), [workspaceNames, theme])
  const legend = useMemo(() => legendEntries(nodes, colors, theme), [nodes, colors, theme])
  const focused = useMemo(() => nodes.find((node) => node.id === focusId) ?? null, [nodes, focusId])
  const inFocus = useMemo(
    () => (focused === null ? null : focusedIds(shown, focused.id, hops)),
    [shown, focused, hops]
  )
  /** Opens a paper the ordinary way (a click, the list, pointing, the tour), which ends any stepping. */
  function focus(id: string | null) {
    setStepFrom(null)
    setFocusId(id)
  }
  const stepOrder = useMemo(() => {
    const from = stepFrom ?? focused?.id ?? null
    return from === null ? [] : linkedInOrder(shown, from)
  }, [shown, stepFrom, focused])
  function step(direction: 1 | -1) {
    const from = stepFrom ?? focused?.id ?? null
    if (from === null) return
    const next = hopTarget(stepOrder, focused?.id === from ? null : (focused?.id ?? null), direction)
    if (next === null) return
    setStepFrom(from)
    setFocusId(next)
  }
  const stepping =
    focused === null || stepOrder.length === 0
      ? null
      : {
          from: stepFrom === null ? null : (nodes.find((node) => node.id === stepFrom)?.title ?? null),
          position: stepOrder.indexOf(focused.id) + 1,
          count: stepOrder.length,
        }

  function chooseView(next: GraphView) {
    setView(next)
    writeView(browserStorage(), next)
  }

  // K20: a failed removal belongs to the paper and the workspace it happened in.
  useEffect(() => {
    links.remove.reset()
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [focused?.id, workspaceId])

  const toggleLayer = (kind: LinkKind) =>
    setLayers((current) => (current.includes(kind) ? current.filter((k) => k !== kind) : [...current, kind]))

  function save(draft: LinkDraft) {
    const editing = dialog?.editing
    // Show what was just drawn: Your links is off by default (P5), and a link the owner can't see is confusing.
    const showManual = () => setLayers((current) => (current.includes('manual') ? current : [...current, 'manual']))
    const done = {
      onSuccess: () => {
        setDialog(null)
        showManual()
      },
    }
    if (editing?.id) links.rename.mutate({ id: editing.id, label: draft.label }, done)
    // A 409 (ALREADY_LINKED) points at the existing link under Your links, so it also needs to be on.
    else if (focused)
      links.create.mutate({ from: focused.id, to: draft.toPaper, label: draft.label }, { ...done, onError: showManual })
  }

  /** Whether the owner confirmed the removal, so the panel knows whether to move focus. */
  function remove(link: GraphLink): boolean {
    const other = nodes.find((node) => node.id === (link.source === focusId ? link.target : link.source))
    if (!link.id) return false
    if (!window.confirm(`Remove your link to "${other?.title ?? 'this paper'}"?`)) return false
    links.remove.mutate(link.id)
    return true
  }

  const counted = countsLine(nodes.length, shown.length)
  const editing = dialog?.editing ?? null
  const editingOther = editing
    ? nodes.find((node) => node.id === (editing.source === focusId ? editing.target : editing.source))
    : undefined

  return (
    <AppShell
      fills
      title="Graph"
      status={
        <>
          {counted}
          {graph.data?.truncated && ' · Showing the first 2000 links.'}
          {embedding.data?.rebuild && ` · ${rebuildLine(embedding.data.source.label, embedding.data.rebuild)}`}
        </>
      }
    >
      {graph.data === undefined || workspaces.data === undefined ? (
        graph.isError || workspaces.isError ? (
          <LoadError
            message={(graph.isError ? graph.error! : workspaces.error!).message}
            onRetry={() => {
              if (graph.isError) void graph.refetch()
              if (workspaces.isError) void workspaces.refetch()
            }}
          />
        ) : (
          <div className="min-h-0 flex-1 animate-pulse rounded-xl bg-muted" />
        )
      ) : (
        // Narrower than they were as a page: the rail takes 14rem of the window now, and the canvas is what
        // should get what's left.
        <div className="grid min-h-0 flex-1 gap-3 lg:grid-cols-[12rem_minmax(0,1fr)_17rem]">
          <div className={cn('min-h-0 overflow-y-auto rounded-xl border border-glass-border p-4', glass)}>
            <GraphControls
              counts={counts}
              layers={layers}
              onToggleLayer={toggleLayer}
              workspaces={workspaces.data ?? []}
              workspaceId={workspaceId}
              onWorkspace={(id) => {
                setWorkspaceId(id)
                focus(null)
              }}
              legend={legend}
              hops={hops}
              onHops={setHops}
              focused={focused !== null}
              view={view}
              axis={axis}
              onAxis={setAxis}
            />
          </div>

          <div className={cn('flex min-h-0 flex-col rounded-xl border border-glass-border', glass)}>
            <p className="sr-only">{`${counted}. Use the papers list to explore connections.`}</p>
            {nodes.length === 0 || allLinks.length === 0 ? (
              <div className="grid flex-1 place-items-center px-6 text-center text-muted-foreground">{EMPTY}</div>
            ) : (
              <GraphViews
                view={view}
                onView={chooseView}
                axis={axis}
                nodes={nodes}
                links={shown}
                theme={theme}
                colors={colors}
                focusId={focused?.id ?? null}
                hops={hops}
                inFocus={inFocus}
                onSelect={focus}
                onStep={step}
              />
            )}
          </div>

          <div className={cn('flex min-h-0 flex-col gap-3 rounded-xl border border-glass-border p-4', glass)}>
            {links.removeError && <ErrorAlert message={links.removeError} />}
            <GraphPanel
              nodes={nodes}
              links={shown}
              focused={focused}
              hops={hops}
              onFocus={focus}
              onClear={() => focus(null)}
              stepping={stepping}
              onStep={step}
              onAddLink={() => setDialog({ editing: null })}
              onEditLink={(link) => setDialog({ editing: link })}
              onRemoveLink={remove}
            />
          </div>
        </div>
      )}

      {dialog !== null && focused !== null && (
        <LinkDialog
          open
          onOpenChange={(open) => {
            if (!open) {
              setDialog(null)
              links.create.reset()
              links.rename.reset()
            }
          }}
          fromTitle={focused.title}
          choices={nodes.filter((node) => node.id !== focused.id)}
          editing={editing ? { label: editing.label ?? '', toTitle: editingOther?.title ?? '' } : null}
          pending={links.create.isPending || links.rename.isPending}
          error={links.saveError}
          onSubmit={save}
        />
      )}
    </AppShell>
  )
}
