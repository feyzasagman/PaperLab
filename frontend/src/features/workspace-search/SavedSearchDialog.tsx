import { useState, type FormEvent } from 'react'
import { useCreateSavedSearch, usePaperSources, useUpdateSavedSearch } from '@/api/queries'
import type { SavedSearch, SavedSearchCreate } from '@/api/client'
import { glass } from '@/components/glass'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { isSearchSource } from './SearchControls'
import { WEEKDAYS, browserTimeZone, errorText } from './schedule'

type Kind = SavedSearchCreate['schedule_kind']
type Source = SavedSearchCreate['sources'][number]

const yearOrNull = (value: string) => (value ? Number(value) : null)

type Props = { workspaceId: string; saved: SavedSearch | null; initialQuery: string; onClose: () => void }

/** Create (saved = null) or edit a saved search. Mount it only while open, so every opening starts from fresh state. */
export function SavedSearchDialog({ workspaceId, saved, initialQuery, onClose }: Props) {
  const create = useCreateSavedSearch(workspaceId)
  const update = useUpdateSavedSearch(workspaceId)
  const paperSources = usePaperSources()
  const choices = (paperSources.data?.sources ?? []).filter((s) => s.enabled && isSearchSource(s.id))

  const [query, setQuery] = useState(saved?.query ?? initialQuery)
  const [sources, setSources] = useState<Source[]>(saved?.sources.filter(isSearchSource) ?? [])
  const [yearFrom, setYearFrom] = useState(saved?.year_from?.toString() ?? '')
  const [yearTo, setYearTo] = useState(saved?.year_to?.toString() ?? '')
  const [kind, setKind] = useState<Kind>((saved?.schedule_kind as Kind | undefined) ?? 'daily')
  const [time, setTime] = useState(saved?.time ?? '02:00')
  const [weekday, setWeekday] = useState(saved?.weekday ?? 1)
  const [cron, setCron] = useState(saved?.schedule_kind === 'custom' ? saved.schedule : '')
  const [timezone, setTimezone] = useState(saved?.timezone ?? browserTimeZone())

  const mutation = saved ? update : create
  const error = errorText(mutation.error?.message)

  async function submit(event: FormEvent) {
    event.preventDefault()
    const body = {
      query: query.trim(),
      sources,
      year_from: yearOrNull(yearFrom),
      year_to: yearOrNull(yearTo),
      schedule_kind: kind,
      time: kind === 'custom' ? null : time,
      weekday: kind === 'weekly' ? weekday : null,
      cron: kind === 'custom' ? cron.trim() : null,
      timezone: timezone.trim(),
    }
    try {
      if (saved) await update.mutateAsync({ id: saved.id, body })
      else await create.mutateAsync(body)
      onClose()
    } catch {
      // shown in the alert below
    }
  }

  const toggle = (id: Source, on: boolean) => setSources(on ? [...sources, id] : sources.filter((s) => s !== id))
  const ready = query.trim() !== '' && timezone.trim() !== '' && (kind !== 'custom' || cron.trim() !== '')

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className={cn(glass, 'bg-glass-strong ring-glass-border sm:max-w-lg')}>
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>{saved ? 'Edit saved search' : 'Save this search'}</DialogTitle>
          </DialogHeader>

          <div className="grid gap-1.5">
            <Label htmlFor="ss-query">Query</Label>
            <Input id="ss-query" autoFocus required value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>

          <fieldset className="grid gap-1.5">
            <legend className="text-sm font-medium">Sources</legend>
            <p className="text-xs text-muted-foreground">Leave all unticked to use every source that is turned on.</p>
            <div className="grid grid-cols-2 gap-1.5">
              {choices.map((s) => (
                <Label key={s.id} className="font-normal">
                  <Checkbox
                    checked={sources.includes(s.id as Source)}
                    onCheckedChange={(on) => toggle(s.id as Source, on === true)}
                  />
                  {s.name}
                </Label>
              ))}
            </div>
          </fieldset>

          <div className="flex gap-3">
            {([['ss-year-from', 'From year', yearFrom, setYearFrom], ['ss-year-to', 'To year', yearTo, setYearTo]] as const).map(([id, label, value, set]) => (
              <div key={id} className="grid gap-1.5">
                <Label htmlFor={id}>{label}</Label>
                <Input id={id} type="number" min={1900} max={2100} className="w-28 tabular-nums"
                  value={value} onChange={(e) => set(e.target.value)} />
              </div>
            ))}
          </div>

          <div className="flex flex-wrap gap-3">
            <div className="grid gap-1.5">
              <Label htmlFor="ss-kind">Schedule</Label>
              <Select value={kind} onValueChange={(v) => setKind(v as Kind)}>
                <SelectTrigger id="ss-kind" className="w-32"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="daily">Daily</SelectItem>
                  <SelectItem value="weekly">Weekly</SelectItem>
                  <SelectItem value="custom">Custom</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {kind === 'weekly' && (
              <div className="grid gap-1.5">
                <Label htmlFor="ss-weekday">Weekday</Label>
                <Select value={String(weekday)} onValueChange={(v) => setWeekday(Number(v))}>
                  <SelectTrigger id="ss-weekday" className="w-36"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {WEEKDAYS.map((name, i) => <SelectItem key={name} value={String(i)}>{name}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
            )}
            {kind === 'custom' ? (
              <div className="grid flex-1 gap-1.5">
                <Label htmlFor="ss-cron">Cron</Label>
                <Input id="ss-cron" className="font-mono" placeholder="0 2 * * 1-5" value={cron}
                  onChange={(e) => setCron(e.target.value)} />
              </div>
            ) : (
              <div className="grid gap-1.5">
                <Label htmlFor="ss-time">Time</Label>
                <Input id="ss-time" type="time" required value={time} onChange={(e) => setTime(e.target.value)} />
              </div>
            )}
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="ss-tz">Time zone</Label>
            <Input id="ss-tz" value={timezone} onChange={(e) => setTimezone(e.target.value)} />
          </div>

          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <DialogFooter className="bg-transparent">
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={!ready || mutation.isPending}>{mutation.isPending ? 'Saving…' : 'Save'}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
