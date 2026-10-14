import { useEffect, useState } from 'react'
import { useChunksOnPage, useEvidenceCard, useGenerateEvidenceCard } from '@/api/queries'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import type { PdfRect } from '../reader/coords'
import { LeftOut, Making, SourceBadge } from './EvidenceBits'
import { FIELDS, cellView, failureReason } from './evidenceFields'

type Props = {
  paperId: string
  /** Scrolls the reader to a page and flashes the rects there. */
  onShowRegion: (page: number, rects: PdfRect[]) => void
}

/** The reader's "Evidence card" section: the card's values with page links that jump to the cited passage. */
export function EvidencePanel({ paperId, onShowRegion }: Props) {
  const card = useEvidenceCard(paperId)
  const generate = useGenerateEvidenceCard()
  const [jump, setJump] = useState<{ page: number; chunkId: string | null; id: number } | null>(null)
  const chunks = useChunksOnPage(paperId, jump?.page ?? null)

  // The chunk's rects arrive after the click; flash the page once they do (a page alone still jumps, rect-less).
  useEffect(() => {
    if (!jump || !chunks.data) return
    onShowRegion(jump.page, chunks.data.find((c) => c.id === jump.chunkId)?.bbox ?? [])
    setJump(null)
  }, [jump, chunks.data, onShowRegion])

  const request = (regenerate: boolean) => generate.mutate({ paperId, regenerate, confirm: false })
  const data = card.data
  return (
    <aside className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto p-4" aria-label="Evidence card">
      {(card.error ?? generate.error) && (
        <Alert variant="destructive"><AlertDescription>{(card.error ?? generate.error)!.message}</AlertDescription></Alert>
      )}
      {data === null && (
        <div className="grid place-items-center gap-2 rounded-xl border border-dashed border-glass-border px-4 py-10 text-center">
          <p className="text-sm text-muted-foreground">No evidence card for this paper yet.</p>
          <Button variant="outline" size="sm" onClick={() => request(false)}>Generate evidence card</Button>
        </div>
      )}
      {data?.status === 'generating' && <Making title="this paper" />}
      {data?.status === 'failed' && (
        <div className="flex flex-col items-start gap-2 text-sm">
          <p className="text-muted-foreground">{data.error ? failureReason(data.error) : 'No evidence card for this paper yet.'}</p>
          <Button variant="outline" size="sm" onClick={() => request(false)}>{data.error ? 'Try again' : 'Generate evidence card'}</Button>
        </div>
      )}
      {data?.status === 'ready' &&
        FIELDS.map((f) => {
          const view = cellView(data, f.key)
          return (
            <section key={f.key} aria-label={f.label} className="flex flex-col gap-1.5">
              <h3 className="text-sm font-semibold">{f.label}</h3>
              {view.shown.length === 0 && <p className="text-sm text-muted-foreground">Nothing found</p>}
              <ul className="flex flex-col gap-1.5">
                {view.shown.map((s) => (
                  <li key={s.index} className="flex flex-col gap-1 text-sm">
                    <span>{s.text}</span>
                    <span className="flex items-center gap-2 text-xs text-muted-foreground">
                      {s.page !== null && (
                        <Button
                          variant="link"
                          size="sm"
                          className="h-auto p-0 text-xs"
                          aria-label={`Go to page ${s.page}`}
                          onClick={() => setJump({ page: s.page!, chunkId: s.chunkId, id: Date.now() })}
                        >
                          p. {s.page}
                        </Button>
                      )}
                      <SourceBadge edited={view.edited} />
                    </span>
                  </li>
                ))}
              </ul>
              <LeftOut count={view.leftOut} />
            </section>
          )
        })}
    </aside>
  )
}
