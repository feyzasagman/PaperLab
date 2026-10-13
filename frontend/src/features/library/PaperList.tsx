import { BookOpenCheck, FileText, LoaderCircle, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { Paper, ReadingIn } from '@/api/client'
import { useDeletePaper, useSetReading, useWorkspaceMembership } from '@/api/queries'
import { glass } from '@/components/glass'
import { fadeIn } from '@/components/motion'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { matchesReading, READING_FILTERS, readingChip, type ReadingFilter } from '@/features/reading/passes'
import { readerHref } from '@/lib/route'
import { cn } from '@/lib/utils'
import { EmbeddingFlight, isEmbedding } from './EmbeddingFlight'
import { PaperContextMenu, PaperMenu } from './PaperMenu'
import { NoMatches, PaperSearch } from './PaperSearch'
import { PaperPreview } from './PaperPreview'
import { SourceBadges } from '../reader/SourceBadges'
import { byline, matchesPaper, pageCountLabel } from './paperMeta'

/** Skimming the list with the mouse shouldn't open a PDF for every row it crosses. */
const HOVER_PREVIEW_DELAY_MS = 150
/** Rows the search brings back fade in one after another, this far apart, the delay stopping growing after a few. */
const STAGGER_MS = 40
const MAX_STAGGER_STEPS = 6

const isIngesting = (paper: Paper) => paper.status !== 'ready' && paper.status !== 'failed'

type RowProps = {
  paper: Paper
  previewed: boolean
  /** Fade in when mounted, this many ms late; null keeps the row still. */
  enterDelayMs: number | null
  workspaceId?: string
  onPreview: (id: string, immediate: boolean) => void
  onDelete: (paper: Paper) => void
  onMembershipChange: (paper: Paper, workspaceId: string, member: boolean) => void
  onReadingChange: (paper: Paper, changes: ReadingIn) => void
}

function PaperRow({ paper, previewed, enterDelayMs, workspaceId, onPreview, onDelete, onMembershipChange, onReadingChange }: RowProps) {
  // Decided once, at mount: a row that stays on screen while the query changes must not replay it. Cleared once played,
  // so a tab panel shown again (display: none restarts animations) doesn't replay it either.
  const [enterDelay, setEnterDelay] = useState(enterDelayMs)
  const meta = [byline(paper), pageCountLabel(paper.page_count)].filter(Boolean).join(' · ')
  const onMembership = (id: string, member: boolean) => onMembershipChange(paper, id, member)
  const onReading = (changes: ReadingIn) => onReadingChange(paper, changes)
  return (
    <PaperContextMenu paper={paper} workspaceId={workspaceId} onMembershipChange={onMembership} onReadingChange={onReading}>
      <li
        data-paper-id={paper.id}
        className={cn(
          'paper-row group relative isolate flex items-start gap-3 px-4 py-3 transition-colors duration-150 hover:bg-foreground/5',
          previewed && 'lg:bg-primary/5 lg:shadow-[inset_3px_0_0_var(--color-primary)]',
          enterDelay !== null && [fadeIn, 'motion-safe:fill-mode-backwards'],
        )}
        style={enterDelay ? { animationDelay: `${enterDelay}ms` } : undefined}
        // Menus inside the row animate too, and their events bubble here through the portal.
        onAnimationEnd={(event) => event.target === event.currentTarget && setEnterDelay(null)}
        onMouseEnter={() => onPreview(paper.id, false)}
        onFocus={() => onPreview(paper.id, true)}
      >
        {isEmbedding(paper) && <EmbeddingFlight />}
        <FileText aria-hidden className="mt-1 size-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          {/* The link's ::after covers the row, so the whole row opens the reader. */}
          <a
            href={readerHref(paper.id)}
            title={paper.title}
            className="line-clamp-2 rounded-sm font-heading wrap-anywhere text-lg leading-snug font-semibold outline-none after:absolute after:inset-0 focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            {paper.title}
          </a>
          {meta && <p className="mt-0.5 truncate text-sm text-muted-foreground">{meta}</p>}
          <div className="mt-1 flex flex-wrap items-center gap-1">
            <SourceBadges paper={paper} />
          </div>
          {paper.status_error && <p className="mt-0.5 text-xs text-destructive">{paper.status_error}</p>}
        </div>
        {readingChip(paper.reading_pass, paper.triage) && (
          <Badge variant="secondary" className="reading-chip mt-1 font-normal">
            <BookOpenCheck aria-hidden />
            <span className="sr-only">Reading: </span>
            {readingChip(paper.reading_pass, paper.triage)}
          </Badge>
        )}
        {/* Ready is the normal state: announced, not shown. Only in-progress and failed papers get a visible badge. */}
        <Badge
          variant={paper.status === 'failed' ? 'destructive' : 'outline'}
          className={cn('status mt-1', paper.status === 'ready' && 'sr-only')}
        >
          {isIngesting(paper) && <LoaderCircle aria-hidden className="motion-safe:animate-spin" />}
          {paper.status}
        </Badge>
        <PaperMenu paper={paper} workspaceId={workspaceId} onMembershipChange={onMembership} onReadingChange={onReading} />
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Delete"
          title="Delete"
          // Quiet until the row is in play: a red icon on every row would shout over the titles.
          className="relative z-10 text-muted-foreground group-focus-within:text-destructive group-hover:text-destructive hover:bg-destructive/10"
          onClick={() => onDelete(paper)}
        >
          <Trash2 aria-hidden />
        </Button>
      </li>
    </PaperContextMenu>
  )
}

type Props = {
  papers: Paper[]
  /** Set on a workspace home: rows then offer "Remove from workspace". */
  workspaceId?: string
}

/**
 * A search bar over paper rows, beside a preview of the hovered or focused one. The library and each workspace's Papers
 * tab use it.
 */
export function PaperList({ papers, workspaceId }: Props) {
  const remove = useDeletePaper()
  const membership = useWorkspaceMembership()
  const reading = useSetReading()
  const [filter, setFilter] = useState<ReadingFilter>('all')
  const hoverTimer = useRef<number | undefined>(undefined)
  const [previewId, setPreviewId] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const searchInput = useRef<HTMLInputElement>(null)
  const shown = papers.filter((paper) => matchesPaper(paper, query) && matchesReading(paper, filter))
  // The paper list as it was at the last keystroke. A row mounting while the list is still that one was brought back by
  // the search, and fades in; rows arriving from the server (first load, an upload) come with a new list, and stay still.
  const [typedOver, setTypedOver] = useState<Paper[] | null>(null)
  const searchBringsBack = typedOver === papers

  useEffect(() => () => window.clearTimeout(hoverTimer.current), [])

  function preview(id: string, immediate: boolean) {
    window.clearTimeout(hoverTimer.current)
    if (immediate) setPreviewId(id)
    else hoverTimer.current = window.setTimeout(() => setPreviewId(id), HOVER_PREVIEW_DELAY_MS)
  }

  function search(next: string) {
    setQuery(next)
    setTypedOver(papers)
  }

  function clearSearch() {
    search('')
    searchInput.current?.focus()
  }

  function onDelete(paper: Paper) {
    if (!window.confirm(`Delete "${paper.title}" from your library and every workspace? Its highlights go with it; notes are kept.`))
      return
    remove.mutate(paper.id)
  }

  const error = remove.error ?? membership.error ?? reading.error
  // Falls back to the first paper, so the panel is never empty and a deleted paper's preview goes away.
  const previewed = shown.find((paper) => paper.id === previewId) ?? shown[0]

  return (
    <>
      {error && (
        <Alert variant="destructive" className={cn('border-glass-border', glass)}>
          <AlertDescription>{error.message}</AlertDescription>
        </Alert>
      )}
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="flex min-w-0 flex-col gap-3">
          <div className="flex gap-2">
            <div className="flex-1">
              <PaperSearch
                query={query}
                shown={shown.length}
                total={papers.length}
                narrowed={query.trim() !== '' || filter !== 'all'}
                inputRef={searchInput}
                onQueryChange={search}
                onClear={clearSearch}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="reading-filter" className="sr-only">
                Reading
              </Label>
              <Select value={filter} onValueChange={(value) => setFilter(value as ReadingFilter)}>
                <SelectTrigger id="reading-filter" className="w-40">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {READING_FILTERS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          {shown.length === 0 ? (
            <NoMatches query={query} filter={filter} onClear={clearSearch} onShowAll={() => setFilter('all')} />
          ) : (
            <Card className={cn('gap-0 py-0 ring-glass-border', glass)}>
              <ul className="divide-y divide-border">
                {shown.map((paper, index) => (
                  <PaperRow
                    key={paper.id}
                    paper={paper}
                    previewed={paper.id === previewed?.id}
                    enterDelayMs={searchBringsBack ? Math.min(index, MAX_STAGGER_STEPS) * STAGGER_MS : null}
                    workspaceId={workspaceId}
                    onPreview={preview}
                    onDelete={onDelete}
                    onMembershipChange={(row, id, member) => membership.mutate({ workspaceId: id, paperIds: [row.id], member })}
                    onReadingChange={(row, changes) => reading.mutate({ paperId: row.id, changes })}
                  />
                ))}
              </ul>
            </Card>
          )}
        </div>
        {/* Desktop only: the preview follows hover and focus, which a touch screen doesn't have. */}
        {previewed && (
          <div className="sticky top-0 hidden lg:block">
            <PaperPreview paper={previewed} />
          </div>
        )}
      </div>
    </>
  )
}
