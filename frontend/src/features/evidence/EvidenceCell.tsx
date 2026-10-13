import { useState } from 'react'
import { useEditEvidenceCard } from '@/api/queries'
import type { EvidenceCard } from '@/api/client'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Textarea } from '@/components/ui/textarea'
import { LeftOut, SourceBadge } from './EvidenceBits'
import { cellView, editChange, type FieldKey } from './evidenceFields'

type Props = { workspaceId: string; paperId: string; title: string; card: EvidenceCard; field: { key: FieldKey; label: string } }

/** One table cell: the first value, "+N more", its page and badge. Clicking opens the edit popover. */
export function EvidenceCell({ workspaceId, paperId, title, card, field }: Props) {
  const view = cellView(card, field.key)
  const [open, setOpen] = useState(false)
  const [drafts, setDrafts] = useState<Record<number, string>>({})
  const edit = useEditEvidenceCard(paperId, workspaceId)
  const [first, ...rest] = view.shown

  if (!first) {
    return (
      <div className="flex flex-col gap-1 text-sm">
        <span className="text-muted-foreground">Nothing found</span>
        <LeftOut count={view.leftOut} />
      </div>
    )
  }

  const changes = view.shown
    .filter((s) => drafts[s.index] !== undefined && drafts[s.index] !== s.editText)
    .map((s) => editChange(field.key, s.index, drafts[s.index]))

  const close = () => {
    setOpen(false)
    setDrafts({})
    edit.reset()
  }
  const save = () => (changes.length === 0 ? close() : edit.mutate(changes, { onSuccess: close }))

  return (
    <div className="flex flex-col gap-1 text-sm">
      <Popover open={open} onOpenChange={(next) => (next ? setOpen(true) : close())}>
        <PopoverTrigger
          aria-label={`Edit ${field.label} for ${title}`}
          className="flex flex-col items-start gap-1 rounded-md text-left outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          <span>{first.text}</span>
          {rest.length > 0 && <span className="text-xs text-muted-foreground">+{rest.length} more</span>}
          <span className="flex items-center gap-2 text-xs text-muted-foreground">
            {first.page !== null && <span>p. {first.page}</span>}
            <SourceBadge edited={view.edited} />
          </span>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-96 gap-3" aria-label={`Edit ${field.label}`}>
          {view.shown.map((s, i) => (
            <div key={s.index} className="flex flex-col gap-1.5">
              <label className="text-xs font-medium" htmlFor={`ev-${paperId}-${field.key}-${s.index}`}>
                {field.label}{view.shown.length > 1 ? ` ${i + 1}` : ''}
              </label>
              <Textarea
                id={`ev-${paperId}-${field.key}-${s.index}`}
                value={drafts[s.index] ?? s.editText}
                onChange={(e) => setDrafts({ ...drafts, [s.index]: e.target.value })}
              />
              <blockquote className="border-l-2 border-border pl-2 text-xs text-muted-foreground">{s.quote}</blockquote>
              {s.page !== null && <span className="text-xs text-muted-foreground">Page {s.page}</span>}
            </div>
          ))}
          {edit.error && (
            <Alert variant="destructive"><AlertDescription>{edit.error.message}</AlertDescription></Alert>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={close}>Cancel</Button>
            <Button size="sm" onClick={save} disabled={edit.isPending}>Save</Button>
          </div>
        </PopoverContent>
      </Popover>
      <LeftOut count={view.leftOut} />
    </div>
  )
}
