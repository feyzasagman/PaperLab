import { CircleHelp, CircleX, Sparkles, ThumbsUp } from 'lucide-react'
import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import type { Hit, HitReviewUpdate, SearchRun } from '@/api/client'
import {
  totalRawFound,
  useAcceptSuggestions,
  useBulkPatchSearchHits,
  useClearSearchHits,
  useImportAllHits,
  useImportSearchHits,
  useNewHitsAvailable,
  usePatchSearchHit,
  useRankedHits,
  useRefreshHits,
  useSearchHits,
  useSnowball,
  useUploadHitPdf,
} from '@/api/queries'
import { PanelResizeHandle } from '@/components/PanelResizeHandle'
import { loadPanelWidth, panelTrack, savePanelWidth, type PanelLimits } from '@/components/panelWidth'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { browserStorage } from '@/features/notes/highlightColors'
import { cn } from '@/lib/utils'
import { HitContextMenu, HitMenu } from './HitMenu'
import { EXCLUDE_REASONS, matchesFilter, sourceLabel } from './hitReview'
import { loadHitSort, saveHitSort, type HitSort } from './hitSort'
import { HitPreview } from './HitPreview'
import { errorText } from './schedule'
import { RankSortBar } from './RankSortBar'
import { snowballMessage } from './ScreeningTab'
import { StatusDot } from './StatusDot'
import { SuggestionChip } from './SuggestionChip'

type ExcludeReason = (typeof EXCLUDE_REASONS)[number]

const ROW_HEIGHT = 44
/** `AppShell`'s `p-3` on the view pane, between the preview's right edge and the window's. */
const PANE_PADDING_PX = 12

/** The hit preview column: 352px was the old fixed 22rem. Half the window at most, so the hit list keeps the larger part. */
const HIT_PREVIEW: PanelLimits = {
  storageKey: 'paperlab-hit-preview-width',
  defaultWidth: 352,
  minWidth: 280,
  maxShare: 0.5,
}

function hasPdfOrAbstract(hit: Hit): boolean {
  return hit.acquisition_status === 'imported' || hit.acquisition_status === 'manual' || Boolean(hit.abstract)
}

/** The hit pool for a search run: a virtualized list (rows can run into the thousands) beside a preview of the
 * selected one (title, byline, abstract — same split as the library's PaperList/PaperPreview). Rows with an
 * abstract to read or a PDF already in the corpus sort first (`hasPdfOrAbstract`) — those are the ones a reader
 * can actually judge; a bare title with neither is the hardest to screen. Selection moves by clicking a row or
 * with the ↑/↓ arrow keys (from the row list or the filter box) — not hover, so skimming the list with the mouse
 * doesn't fight with reading the panel. A filter box narrows the already-loaded rows by title OR abstract
 * (`matchesFilter`) client-side (no new request — see `useSearchHits`, still the full, unfiltered query
 * underneath) — whichever provider supplied the abstract, a term can turn up there even when the title doesn't
 * mention it. Stage-1 triage
 * is via a right-click menu or a trailing ⋮ button on each row, and there are two ways to get a PDF: one hit at a
 * time from the preview panel's "Add PDF" (fetching a PDF starts ingestion — chunking, embedding — for that paper,
 * so this is the cheap default), or "Import all with PDF in this filter" when the user has decided that cost is
 * worth paying for everything currently in view at once. "Clear old results" is the other direction — every
 * search a workspace has ever run lands in this same shared pool, with no built-in way to remove anything, so
 * this wipes every not-yet-imported hit back to empty (confirmed first — it's destructive) when the accumulated
 * history no longer matters; already-imported papers are untouched. `run` is read for two things: whether the worker has
 * found more since the pool was last loaded (the run's own live status is shown elsewhere, SearchTab, unaffected
 * by any of this), and the live "N found so far" count — the raw, pre-dedup total every source has turned up
 * (`totalRawFound`), distinct from "in pool," which is the de-duplicated count of what's actually been loaded. */
export function HitTable({ workspaceId, run }: { workspaceId: string; run?: SearchRun }) {
  const [sort, setSort] = useState<HitSort>(() => loadHitSort(browserStorage(), workspaceId))
  const [minRelevance, setMinRelevance] = useState<number | null>(null)
  const { data, hasNextPage, isFetchingNextPage, fetchNextPage } = useSearchHits(workspaceId, undefined, undefined, {
    sort: sort === 'ai' ? 'ai' : undefined,
    minRelevance: sort === 'ranked' ? undefined : (minRelevance ?? undefined),
  })
  const acceptSuggestions = useAcceptSuggestions(workspaceId)
  const importHits = useImportSearchHits(workspaceId)
  const importAllHits = useImportAllHits(workspaceId)
  const clearHits = useClearSearchHits(workspaceId)
  const reviewHit = usePatchSearchHit(workspaceId)
  const uploadHitPdf = useUploadHitPdf(workspaceId)
  const bulkPatch = useBulkPatchSearchHits(workspaceId)
  const snowball = useSnowball(workspaceId)
  const newHits = useNewHitsAvailable(run)
  const refreshHits = useRefreshHits(workspaceId)
  const parentRef = useRef<HTMLDivElement>(null)
  const [previewId, setPreviewId] = useState<string | null>(null)
  const [filterText, setFilterText] = useState('')
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set())
  const [bulkReason, setBulkReason] = useState<ExcludeReason | ''>('')
  const [previewWidth, setPreviewWidth] = useState(() => loadPanelWidth(HIT_PREVIEW, browserStorage()))
  useEffect(() => savePanelWidth(HIT_PREVIEW, browserStorage(), previewWidth), [previewWidth])
  useEffect(() => saveHitSort(browserStorage(), workspaceId, sort), [workspaceId, sort])
  const ranked = useRankedHits(workspaceId, sort === 'ranked')

  const rows = sort === 'ranked' ? (ranked.data?.items ?? []) : (data?.pages.flatMap((page) => page.items) ?? [])
  const rawFound = totalRawFound(run)
  const filter = filterText.trim().toLowerCase()
  const filteredRows = filter ? rows.filter((hit) => matchesFilter(hit, filter)) : rows
  // A hit with an abstract to read or a PDF already in the corpus is the one worth looking at first — a bare
  // title with neither is the hardest to judge relevance from. Stable sort (native since ES2019), so hits within
  // each group keep their existing (first_seen_at, id) order from the server. Ranked order is already sorted by
  // the server (most likely relevant first) and must stay that way — sorting it again here would undo the point.
  // ponytail: "year" sorts only the already-loaded found-order page(s) client-side, same data source as found
  // order (no new endpoint) — a hit with no year sorts last. Rows already on screen can shift as more pages load
  // in behind them; fine for this quick screening-helper view, upgrade to a server-sorted endpoint (like ranked)
  // if that ever matters.
  const visibleRows =
    sort === 'ranked'
      ? filteredRows
      : sort === 'ai'
        ? filteredRows // the server already ordered these by score
        : sort === 'year'
        ? filteredRows.slice().sort((a, b) => (b.year ?? -Infinity) - (a.year ?? -Infinity))
        : filteredRows.slice().sort((a, b) => Number(hasPdfOrAbstract(b)) - Number(hasPdfOrAbstract(a)))
  // Falls back to the first loaded row, so the panel is never empty on first paint (mirrors PaperList) — and to
  // whichever row is first once a filter drops the previously selected one out of view.
  const previewed = visibleRows.find((hit) => hit.id === previewId) ?? visibleRows[0]

  const virtualizer = useVirtualizer({
    count: visibleRows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 10,
    // Without this, the virtualizer reports zero virtual items until ResizeObserver fires with the real
    // container size (a first-paint flash of an empty list) — and jsdom has no ResizeObserver at all, so
    // tests would see an empty list forever. A reasonable guess for the panel's usual height fixes both.
    initialRect: { width: 600, height: 600 },
  })
  const virtualItems = virtualizer.getVirtualItems()
  const onReview = (hitId: string, body: HitReviewUpdate) => reviewHit.mutate({ hitId, body })
  // Seeds a snowball hop straight from this hit — no import required first (see HitMenu's onSnowball docstring).
  const onSnowball = (hitId: string) => snowball.mutate({ seed_hit_ids: [hitId], backward: true, forward: true })

  // Multi-select stage-1 triage: the same decision HitMenu/HitPreview apply one hit at a time, applied to every
  // checked row in a single request (useBulkPatchSearchHits). Selection is cleared optimistically on click, same
  // as ScreeningTab's confirmExclude — a failed request shows via bulkPatch.isError and the rows stay re-selectable.
  const allVisibleChecked = visibleRows.length > 0 && visibleRows.every((hit) => checkedIds.has(hit.id))
  const someVisibleChecked = visibleRows.some((hit) => checkedIds.has(hit.id))

  function toggleChecked(hitId: string) {
    setCheckedIds((prev) => {
      const next = new Set(prev)
      if (next.has(hitId)) next.delete(hitId)
      else next.add(hitId)
      return next
    })
  }

  function toggleCheckAllVisible() {
    setCheckedIds(allVisibleChecked ? new Set() : new Set(visibleRows.map((hit) => hit.id)))
  }

  function bulkMark(stage1_status: 'relevant' | 'maybe') {
    bulkPatch.mutate({ hit_ids: [...checkedIds], stage1_status })
    setCheckedIds(new Set())
  }

  function bulkAccept() {
    acceptSuggestions.mutate([...checkedIds])
    setCheckedIds(new Set())
  }

  function bulkMarkNotRelevant() {
    if (!bulkReason) return
    bulkPatch.mutate({ hit_ids: [...checkedIds], stage1_status: 'not_relevant', stage1_exclude_reason: bulkReason })
    setCheckedIds(new Set())
    setBulkReason('')
  }

  // ↑/↓ moves the selected row by one, wrapping never — the top/bottom just stop. Works from the filter box too
  // (arrow keys do nothing useful in a single-line input, so hijacking them here doesn't lose anything), and
  // scrolls the new selection into view since it can be off-screen in a long, virtualized list.
  function moveSelection(direction: 1 | -1, event: React.KeyboardEvent) {
    if (visibleRows.length === 0) return
    event.preventDefault()
    const currentIndex = visibleRows.findIndex((hit) => hit.id === previewed?.id)
    const nextIndex = Math.min(Math.max(currentIndex + direction, 0), visibleRows.length - 1)
    setPreviewId(visibleRows[nextIndex].id)
    virtualizer.scrollToIndex(nextIndex, { align: 'auto' })
  }

  function onListKeyDown(event: React.KeyboardEvent) {
    if (event.key === 'ArrowDown') moveSelection(1, event)
    else if (event.key === 'ArrowUp') moveSelection(-1, event)
  }

  // Data only ever moves into view because the user is scrolled to the bottom of what's loaded — never on a
  // background timer or poll, which doesn't scale once the pool reaches thousands of hits (re-fetching an
  // infinite query re-fetches every already-loaded page; see useNewHitsAvailable's docstring). Two cases at the
  // bottom: a known next page (hasNextPage) just fetches it, same as always. When there is no known next page but
  // the worker has found more since our last real fetch (newHits.available, from the already-polled run status),
  // the cached "no more pages" cursor is stale — pay the one real-refresh cost here, since the user scrolling to
  // the bottom asking for more is exactly the moment it's worth it. Keyed off the *filtered* list's own bottom —
  // a filter that's currently showing only a few matches should still be able to page in more of the pool looking
  // for further matches, the same way scrolling the unfiltered list does.
  useEffect(() => {
    if (sort === 'ranked') return // ranked order has its own, unpaginated query — nothing more to page in
    const lastItem = virtualItems.at(-1)
    if (!lastItem || lastItem.index < visibleRows.length - 1 || isFetchingNextPage) return
    if (hasNextPage) {
      fetchNextPage()
    } else if (newHits.available) {
      newHits.acknowledge()
      refreshHits()
    }
  }, [sort, virtualItems, hasNextPage, isFetchingNextPage, fetchNextPage, visibleRows.length, newHits, refreshHits])

  // Collapses every mutation's own isError/isSuccess into the single line actually shown (see the render site
  // below for why) — errors first, bulk/global actions before single-row ones, success notices last.
  const statusMessage: { tone: 'error' | 'status'; text: string } | null =
    (acceptSuggestions.isError && {
      tone: 'error' as const,
      text: errorText(acceptSuggestions.error.message) ?? acceptSuggestions.error.message,
    }) ||
    (bulkPatch.isError && { tone: 'error' as const, text: bulkPatch.error.message }) ||
    (importHits.isError && { tone: 'error' as const, text: importHits.error.message }) ||
    (importAllHits.isError && { tone: 'error' as const, text: importAllHits.error.message }) ||
    (clearHits.isError && { tone: 'error' as const, text: clearHits.error.message }) ||
    (reviewHit.isError && { tone: 'error' as const, text: reviewHit.error.message }) ||
    (uploadHitPdf.isError && { tone: 'error' as const, text: uploadHitPdf.error.message }) ||
    (snowball.isError && { tone: 'error' as const, text: snowball.error.message }) ||
    // A "failed" import is a resolved response (see useImportSearchHits), not a rejected mutation — no free
    // copy was found automatically, which isn't an error so much as a cue to use the manual options already
    // in the preview panel.
    (importHits.isSuccess &&
      importHits.data.failed > 0 && {
        tone: 'status' as const,
        text: 'No PDF found automatically for that paper — try the options below, or upload one.',
      }) ||
    (snowball.isSuccess && { tone: 'status' as const, text: snowballMessage(snowball.data) }) ||
    (acceptSuggestions.isSuccess && {
      tone: 'status' as const,
      text: `Accepted ${acceptSuggestions.data.accepted}, skipped ${acceptSuggestions.data.skipped.length}`,
    }) ||
    null

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between gap-3 border-b px-3 py-2 text-sm">
        <Checkbox
          aria-label="Select all visible hits"
          checked={someVisibleChecked && !allVisibleChecked ? 'indeterminate' : allVisibleChecked}
          onCheckedChange={toggleCheckAllVisible}
          disabled={visibleRows.length === 0}
          className="shrink-0"
        />
        <label className="flex flex-1 items-center gap-2">
          <span className="sr-only">Search hits</span>
          <input
            type="text"
            value={filterText}
            onChange={(event) => setFilterText(event.target.value)}
            onKeyDown={onListKeyDown}
            placeholder="Search by title or abstract…"
            aria-label="Search hits"
            className="h-8 w-full max-w-xs rounded-lg border bg-background px-2 text-sm"
          />
        </label>
        <span className="shrink-0 text-muted-foreground">
          {rawFound !== undefined && `${rawFound} found so far · `}
          {filter ? `${visibleRows.length} of ${rows.length} in pool` : `${rows.length} in pool`}
        </span>
        <Button type="button" size="sm" disabled={importAllHits.isPending} onClick={() => importAllHits.mutate()}>
          Import all with PDF in this filter
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={clearHits.isPending}
          onClick={() => {
            if (window.confirm('Clear every hit not yet imported? Papers already in your corpus stay untouched.')) {
              clearHits.mutate()
            }
          }}
        >
          Clear old results
        </Button>
      </div>
      {checkedIds.size > 0 && (
        <div className="flex flex-wrap items-center gap-2 border-b bg-muted/50 px-3 py-2 text-sm">
          <span className="font-medium">{checkedIds.size} selected</span>
          <Button type="button" size="sm" disabled={bulkPatch.isPending} onClick={() => bulkMark('relevant')}>
            <ThumbsUp aria-hidden />
            Mark relevant
          </Button>
          <Button type="button" size="sm" variant="outline" disabled={bulkPatch.isPending} onClick={() => bulkMark('maybe')}>
            <CircleHelp aria-hidden />
            Mark maybe
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={acceptSuggestions.isPending}
            onClick={bulkAccept}
          >
            <Sparkles aria-hidden />
            Accept AI suggestions
          </Button>
          <label htmlFor="bulk-exclude-reason" className="sr-only">
            Exclusion reason for selected hits
          </label>
          <select
            id="bulk-exclude-reason"
            value={bulkReason}
            onChange={(event) => setBulkReason(event.target.value as ExcludeReason | '')}
            aria-label="Exclusion reason for selected hits"
            className="h-8 rounded-lg border bg-background px-2 text-sm"
          >
            <option value="">Select a reason…</option>
            {EXCLUDE_REASONS.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <Button
            type="button"
            size="sm"
            variant="destructive"
            disabled={!bulkReason || bulkPatch.isPending}
            onClick={bulkMarkNotRelevant}
          >
            <CircleX aria-hidden />
            Mark not relevant
          </Button>
          <Button type="button" size="sm" variant="ghost" className="text-muted-foreground" onClick={() => setCheckedIds(new Set())}>
            Clear selection
          </Button>
        </div>
      )}
      <RankSortBar
        sort={sort}
        onSortChange={setSort}
        ranked={ranked.data}
        minRelevance={minRelevance}
        onMinRelevanceChange={setMinRelevance}
      />
      {/* One status line, not one paragraph per mutation: with up to 9 independent isError/isSuccess flags on
          this screen, stacking them all would both clutter the view and shift the hit list up/down as each one
          comes and goes. Errors win over a success notice, in roughly the order a reader would expect to act on
          them (bulk/global actions before single-row ones). */}
      {statusMessage && (
        <p
          role={statusMessage.tone === 'error' ? 'alert' : 'status'}
          className={cn('px-3 py-1 text-xs', statusMessage.tone === 'error' ? 'text-destructive' : 'text-muted-foreground')}
        >
          {statusMessage.text}
        </p>
      )}
      {filter && visibleRows.length === 0 && (
        <p className="px-3 py-1 text-xs text-muted-foreground">No hits match “{filterText.trim()}”.</p>
      )}
      <div
        className="grid min-h-0 flex-1 gap-3 lg:grid-cols-[minmax(0,1fr)_var(--preview-track)]"
        style={{ '--preview-track': panelTrack(HIT_PREVIEW, previewWidth) } as CSSProperties}
      >
        <div
          ref={parentRef}
          aria-label="Hit pool"
          tabIndex={0}
          onKeyDown={onListKeyDown}
          className="min-h-0 overflow-auto outline-none"
        >
          <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
            {virtualItems.map((virtualRow) => {
              const hit = visibleRows[virtualRow.index]
              const byline = [hit.authors?.slice(0, 3).join(', '), hit.year].filter(Boolean).join(' · ')
              const selected = hit.id === previewed?.id
              return (
                <HitContextMenu key={hit.id} hit={hit} onReview={onReview} onSnowball={onSnowball}>
                  <div
                    className={cn(
                      'flex w-full cursor-pointer items-center gap-2 border-b px-3 text-sm hover:bg-muted',
                      selected && 'bg-muted',
                    )}
                    style={{ position: 'absolute', top: virtualRow.start, height: virtualRow.size, width: '100%' }}
                    aria-selected={selected}
                    onClick={() => setPreviewId(hit.id)}
                    onFocus={() => setPreviewId(hit.id)}
                  >
                    <Checkbox
                      aria-label={`Select ${hit.title ?? hit.normalized_title}`}
                      checked={checkedIds.has(hit.id)}
                      onCheckedChange={() => toggleChecked(hit.id)}
                      onClick={(event) => event.stopPropagation()}
                      className="shrink-0"
                    />
                    <span className="flex-1 truncate">
                      {hit.title ?? hit.normalized_title}
                      {byline && <span className="text-muted-foreground"> · {byline}</span>}
                    </span>
                    {hit.sources[0] && (
                      <span className="shrink-0 rounded-full border px-1.5 py-0.5 text-xs text-muted-foreground">
                        {sourceLabel(hit.sources[0])}
                      </span>
                    )}
                    <SuggestionChip hit={hit} />
                    {(hit.suggestion === 'include' || hit.suggestion === 'exclude') && !hit.stage1_status && (
                      <Button
                        type="button"
                        size="xs"
                        variant="outline"
                        aria-label={`Accept: ${hit.title ?? hit.normalized_title}`}
                        disabled={acceptSuggestions.isPending}
                        onClick={(event) => {
                          event.stopPropagation()
                          acceptSuggestions.mutate([hit.id])
                        }}
                      >
                        Accept
                      </Button>
                    )}
                    <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      <StatusDot status={hit.stage1_status} />
                      {hit.stage1_status ?? 'unreviewed'}
                    </span>
                    <HitMenu hit={hit} onReview={onReview} onSnowball={onSnowball} />
                  </div>
                </HitContextMenu>
              )
            })}
          </div>
        </div>
        {/* Desktop only: the preview panel follows the click/arrow-key selection, which a touch screen doesn't
            have arrow keys for anyway — same split as PaperPreview. */}
        {previewed && (
          <div className="relative hidden min-h-0 min-w-0 lg:block">
            <PanelResizeHandle
              limits={HIT_PREVIEW}
              width={previewWidth}
              onWidthChange={setPreviewWidth}
              label="Resize preview"
              inset={PANE_PADDING_PX}
            />
            <HitPreview
              hit={previewed}
              onReview={onReview}
              onAddPdf={(hitId) => importHits.mutate([hitId])}
              addPdfPending={importHits.isPending}
              onUpload={(hitId, file) => uploadHitPdf.mutate({ hitId, file })}
              uploadPending={uploadHitPdf.isPending}
            />
          </div>
        )}
      </div>
      {isFetchingNextPage && <div className="border-t px-3 py-1 text-xs text-muted-foreground">Loading more…</div>}
    </div>
  )
}
