import { Table2, ZoomIn, ZoomOut } from 'lucide-react'
import type { Paper } from '@/api/client'
import { glass } from '@/components/glass'
import { ModeToggle } from '@/components/mode-toggle'
import { delayedIn } from '@/components/motion'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { ReadingControl } from '../reading/ReadingControl'
import { PaperDetailsDialog } from './PaperDetailsDialog'
import { SourceBadges } from './SourceBadges'
import { ZOOM_STEPS } from './zoom'

type Props = {
  paper: Paper | undefined
  zoomIndex: number
  onZoomChange: (index: number) => void
  /** Whether dragging on the pages draws a box to capture a table. */
  capturing: boolean
  onCaptureChange: (on: boolean) => void
  onOpenReferences: () => void
}

export function ReaderToolbar({ paper, zoomIndex, onZoomChange, capturing, onCaptureChange, onOpenReferences }: Props) {
  const title = paper?.title
  return (
    <>
    <header className={cn('col-span-full flex h-11 items-center gap-1 border-b border-glass-border px-2', glass)}>
      <Button variant="ghost" size="sm" asChild className="shrink-0 text-muted-foreground">
        <a href="#/">← Library</a>
      </Button>
      <h1 className={cn('mx-1.5 min-w-0 flex-1 truncate font-heading text-base leading-none font-semibold', title === undefined && delayedIn)} title={title}>
        {title ?? 'Loading…'}
      </h1>
      {paper && (
        <div className="hidden shrink-0 items-center gap-1 md:flex">
          <SourceBadges paper={paper} />
        </div>
      )}
      {paper && <ReadingControl paper={paper} onOpenReferences={onOpenReferences} />}
      <Button
        variant="ghost"
        size="sm"
        aria-pressed={capturing}
        className="aria-pressed:border-primary aria-pressed:bg-primary/10 aria-pressed:text-primary"
        onClick={() => onCaptureChange(!capturing)}
      >
        <Table2 aria-hidden />
        Capture table
      </Button>
      {paper && <PaperDetailsDialog paper={paper} />}
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Zoom out"
        disabled={zoomIndex === 0}
        onClick={() => onZoomChange(Math.max(0, zoomIndex - 1))}
      >
        <ZoomOut aria-hidden />
      </Button>
      <span className="zoom-level min-w-12 text-center text-sm tabular-nums">
        {Math.round(ZOOM_STEPS[zoomIndex] * 100)}%
      </span>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Zoom in"
        disabled={zoomIndex === ZOOM_STEPS.length - 1}
        onClick={() => onZoomChange(Math.min(ZOOM_STEPS.length - 1, zoomIndex + 1))}
      >
        <ZoomIn aria-hidden />
      </Button>
      <ModeToggle />
    </header>
    {paper && (
      <div className={cn('flex flex-wrap items-center gap-1 border-b border-glass-border px-3 py-1.5 md:hidden', glass)}>
        <SourceBadges paper={paper} />
      </div>
    )}
    </>
  )
}
