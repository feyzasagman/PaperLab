import { PencilLine } from 'lucide-react'
import { useState, type FormEvent, type ReactNode } from 'react'
import type { Paper } from '@/api/client'
import { useUpdatePaper } from '@/api/queries'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { SOURCE_TYPES, toForm, toUpdate, type PaperForm } from './paperForm'

const SELECT_CLASS =
  'h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-xs focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none'

const ABSTRACT_MAX_LENGTH = 10_000

const DETAIL_MESSAGES: Record<string, string> = { doi_taken: 'Another paper in your library already has this DOI.' }

function Field({ id, label, children }: { id: string; label: ReactNode; children: ReactNode }) {
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
    </div>
  )
}

/** The manual correction escape hatch: whatever is saved here, extraction and enrichment never overwrite. */
export function PaperDetailsDialog({ paper }: { paper: Paper }) {
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState<PaperForm>(() => toForm(paper))
  const save = useUpdatePaper(paper.id)
  const update = toUpdate(paper, form)
  const set = (patch: Partial<PaperForm>) => setForm((current) => ({ ...current, ...patch }))

  function openChange(next: boolean) {
    if (next) {
      setForm(toForm(paper)) // start from the saved paper, not an abandoned draft
      save.reset()
    }
    setOpen(next)
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    try {
      await save.mutateAsync(update)
      setOpen(false)
    } catch {
      // shown in the alert below from save.error
    }
  }

  const error = save.error?.message
  return (
    <Dialog open={open} onOpenChange={openChange}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="sm">
          <PencilLine aria-hidden />
          Edit details
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>Edit details</DialogTitle>
            <DialogDescription>Fields you change here are kept when the paper is processed again.</DialogDescription>
          </DialogHeader>
          <Field
            id="paper-title"
            label={
              <>
                Title <span aria-hidden="true">*</span>
              </>
            }
          >
            <Input id="paper-title" required value={form.title} onChange={(e) => set({ title: e.target.value })} />
          </Field>
          <Field id="paper-authors" label="Authors">
            <Textarea
              id="paper-authors"
              placeholder="One author per line"
              className="max-h-60 overflow-y-auto"
              value={form.authors}
              onChange={(e) => set({ authors: e.target.value })}
            />
          </Field>
          <div className="grid grid-cols-[6rem_1fr] gap-3">
            <Field id="paper-year" label="Year">
              <Input
                id="paper-year"
                type="number"
                min={1000}
                max={2100}
                value={form.year}
                onChange={(e) => set({ year: e.target.value })}
              />
            </Field>
            <Field id="paper-venue" label="Venue">
              <Input id="paper-venue" value={form.venue} onChange={(e) => set({ venue: e.target.value })} />
            </Field>
          </div>
          <Field id="paper-doi" label="DOI">
            <Input id="paper-doi" value={form.doi} onChange={(e) => set({ doi: e.target.value })} />
          </Field>
          <Field id="paper-abstract" label="Abstract">
            <Textarea
              id="paper-abstract"
              maxLength={ABSTRACT_MAX_LENGTH}
              className="max-h-60 overflow-y-auto"
              value={form.abstract}
              onChange={(e) => set({ abstract: e.target.value })}
            />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field id="paper-source-type" label="Source type">
              <select
                id="paper-source-type"
                className={SELECT_CLASS}
                value={form.sourceType}
                onChange={(e) => set({ sourceType: e.target.value as PaperForm['sourceType'] })}
              >
                <option value="">Not set</option>
                {SOURCE_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field id="paper-peer-reviewed" label="Peer review">
              <select
                id="paper-peer-reviewed"
                className={SELECT_CLASS}
                value={form.peerReviewed}
                onChange={(e) => set({ peerReviewed: e.target.value as PaperForm['peerReviewed'] })}
              >
                <option value="unknown">Unknown</option>
                <option value="true">Peer-reviewed</option>
                <option value="false">Not peer-reviewed</option>
              </select>
            </Field>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="paper-retracted"
              checked={form.isRetracted}
              onCheckedChange={(checked) => set({ isRetracted: checked === true })}
            />
            <Label htmlFor="paper-retracted">Retracted</Label>
          </div>
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{DETAIL_MESSAGES[error] ?? error}</AlertDescription>
            </Alert>
          )}
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline">
                Cancel
              </Button>
            </DialogClose>
            <Button
              type="submit"
              disabled={save.isPending || Object.keys(update).length === 0 || !form.title.trim()}
            >
              {save.isPending ? 'Saving…' : 'Save'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
