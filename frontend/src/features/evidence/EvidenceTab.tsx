import { useState } from 'react'
import { Download } from 'lucide-react'
import { api, type EvidenceCardRow } from '@/api/client'
import { useEvidenceCards, useGenerateEvidenceCard, useGenerateEvidenceCards } from '@/api/queries'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Making } from './EvidenceBits'
import { EvidenceCell } from './EvidenceCell'
import { FIELDS, failureReason } from './evidenceFields'

/** The workspace Evidence tab: one row per paper, one column per card field. */
export function EvidenceTab({ workspaceId }: { workspaceId: string }) {
  const rows = useEvidenceCards(workspaceId)
  const generateMissing = useGenerateEvidenceCards(workspaceId)
  const [replacing, setReplacing] = useState<EvidenceCardRow | null>(null)
  const generateOne = useGenerateEvidenceCard(workspaceId)
  const error = rows.error ?? generateMissing.error ?? generateOne.error
  const generating = rows.data?.filter((r) => r.card?.status === 'generating').length ?? 0

  const regenerate = (row: EvidenceCardRow, confirm: boolean) =>
    generateOne.mutate({ paperId: row.paper_id, regenerate: row.card?.status === 'ready', confirm })

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={() => generateMissing.mutate()} disabled={generateMissing.isPending || !rows.data?.length}>
          Generate cards
        </Button>
        <Button variant="outline" asChild>
          <a href={api.evidenceCardsCsvUrl(workspaceId)} download>
            <Download aria-hidden /> Export CSV
          </a>
        </Button>
        {generating > 0 && (
          <span role="status" className="text-sm text-muted-foreground">
            Making {generating} {generating === 1 ? 'card' : 'cards'}…
          </span>
        )}
      </div>
      {error && <Alert variant="destructive"><AlertDescription>{error.message}</AlertDescription></Alert>}
      {rows.data?.length === 0 && (
        <p className="text-muted-foreground">Add papers to this workspace and their evidence cards will show up here.</p>
      )}
      {!!rows.data?.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Paper</TableHead>
              {FIELDS.map((f) => <TableHead key={f.key}>{f.label}</TableHead>)}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.data.map((row) => (
              <PaperRow
                key={row.paper_id}
                row={row}
                workspaceId={workspaceId}
                onGenerate={(r) => (r.card?.provenance === 'llm_edited' && r.card.status === 'ready' ? setReplacing(r) : regenerate(r, false))}
              />
            ))}
          </TableBody>
        </Table>
      )}
      <Dialog open={replacing !== null} onOpenChange={(open) => !open && setReplacing(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Replace your edits?</DialogTitle>
            <DialogDescription>
              You have edited this card. Making it again will replace your changes with a fresh card.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setReplacing(null)}>Keep my edits</Button>
            <Button
              onClick={() => {
                const row = replacing!
                setReplacing(null)
                regenerate(row, true)
              }}
            >
              Replace my edits
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function PaperRow({ row, workspaceId, onGenerate }: { row: EvidenceCardRow; workspaceId: string; onGenerate: (row: EvidenceCardRow) => void }) {
  const { card, title } = row
  const meta = [row.first_author, row.year].filter(Boolean).join(', ')
  return (
    <TableRow>
      <TableHead scope="row" className="max-w-56 whitespace-normal align-top">
        <div className="font-medium">{title}</div>
        {meta && <div className="text-xs font-normal text-muted-foreground">{meta}</div>}
        {card?.status === 'ready' && (
          <Button variant="link" size="sm" className="h-auto p-0" onClick={() => onGenerate(row)} aria-label={`Regenerate card for ${title}`}>
            Regenerate
          </Button>
        )}
      </TableHead>
      {card?.status === 'ready' ? (
        FIELDS.map((f) => (
          <TableCell key={f.key} className="max-w-56 whitespace-normal align-top">
            <EvidenceCell workspaceId={workspaceId} paperId={row.paper_id} title={title} card={card} field={f} />
          </TableCell>
        ))
      ) : (
        <TableCell colSpan={FIELDS.length} className="align-top">
          {card?.status === 'generating' ? (
            <Making title={title} />
          ) : (
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <span className="text-muted-foreground">{card?.status === 'failed' && card.error ? failureReason(card.error) : 'Not generated'}</span>
              <Button size="sm" variant="outline" onClick={() => onGenerate(row)} aria-label={`Generate card for ${title}`}>
                {card?.error ? 'Try again' : 'Generate'}
              </Button>
            </div>
          )}
        </TableCell>
      )}
    </TableRow>
  )
}
