import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { keys, useSaveScreening, useScreeningState, useStartSuggestions, useStopSuggestions } from '@/api/queries'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { errorText } from './schedule'

const CRITERIA_MAX = 4000

type Fields = { research_question: string; inclusion_criteria: string; exclusion_criteria: string }
const FIELDS: { key: keyof Fields; label: string; hint: string }[] = [
  { key: 'research_question', label: 'Research question', hint: 'What are you trying to find out?' },
  { key: 'inclusion_criteria', label: 'Include when…', hint: 'One rule per line, e.g. randomised trials in adults' },
  { key: 'exclusion_criteria', label: 'Exclude when…', hint: 'One rule per line, e.g. animal studies' },
]

/** Research question, criteria, settings and the on-request suggestion job. Suggestions only start when asked. */
export function ScreeningAssist({ workspaceId }: { workspaceId: string }) {
  const state = useScreeningState(workspaceId)
  const save = useSaveScreening(workspaceId)
  const start = useStartSuggestions(workspaceId)
  const stop = useStopSuggestions(workspaceId)
  const client = useQueryClient()
  const [draft, setDraft] = useState<Partial<Fields> | null>(null)
  const status = state.data?.suggest_status
  const previous = useRef(status)
  const [userOpen, setUserOpen] = useState<boolean | null>(null)
  const initialOpen = useRef<boolean | null>(null)
  useEffect(() => {
    if (previous.current && previous.current !== 'idle' && status === 'idle') {
      client.invalidateQueries({ queryKey: keys.searchHitsRoot(workspaceId) })
      client.invalidateQueries({ queryKey: keys.rankedHits(workspaceId) })
    }
    previous.current = status
  }, [status, client, workspaceId])

  if (!state.data) return null
  const data = state.data
  const saved: Fields = {
    research_question: data.research_question ?? '',
    inclusion_criteria: data.inclusion_criteria ?? '',
    exclusion_criteria: data.exclusion_criteria ?? '',
  }
  const fields: Fields = { ...saved, ...draft }
  // The server refuses a run with only exclusion criteria.
  const hasCriteria = Boolean(saved.research_question || saved.inclusion_criteria)
  // Open on first sight only when nothing is saved; saving must not fold the panel away under the owner.
  initialOpen.current ??= !hasCriteria
  const running = status !== 'idle'
  const noModel = data.model_label === null
  const percent = data.suggest_total > 0 ? Math.round((data.suggest_done / data.suggest_total) * 100) : null

  function onSuggest() {
    const remote = data.model_is_local === false
    if (remote && !window.confirm(`Abstracts will be sent to ${data.model_host} and may cost money. Continue?`)) return
    start.mutate(remote)
  }

  return (
    // shrink-0 + its own scroll: the hit list below always keeps its share of a short window.
    <div className="flex max-h-[40vh] shrink-0 flex-col gap-2 overflow-y-auto border-b px-3 py-2 text-sm">
      <details open={userOpen ?? initialOpen.current} onToggle={(event) => setUserOpen(event.currentTarget.open)}>
        <summary className="cursor-pointer truncate font-medium">
          Screening criteria
          {saved.research_question && (
            <span className="font-normal text-muted-foreground"> · {saved.research_question}</span>
          )}
        </summary>
        <div className="mt-2 flex flex-col gap-2">
        {FIELDS.map(({ key, label, hint }) => (
          <div key={key} className="flex flex-col gap-1">
            <Label htmlFor={`screening-${key}`}>{label}</Label>
            <Textarea
              id={`screening-${key}`}
              value={fields[key]}
              maxLength={CRITERIA_MAX}
              disabled={running}
              placeholder={hint}
              onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
              rows={key === 'research_question' ? 2 : 3}
            />
          </div>
        ))}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={running || draft === null || save.isPending}
            onClick={() => save.mutate(fields, { onSuccess: () => setDraft(null) })}
          >
            Save criteria
          </Button>
        </div>
        <div className="flex flex-col gap-2 border-t pt-2">
          <div className="flex items-center gap-2">
            <Switch
              id="screening-prescreen"
              checked={data.prescreen_after_scheduled_runs}
              disabled={save.isPending}
              onCheckedChange={(checked) => save.mutate({ prescreen_after_scheduled_runs: checked })}
            />
            <Label htmlFor="screening-prescreen">Pre-screen new hits after a scheduled search (local model only)</Label>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Label htmlFor="screening-auto-import">Import free PDFs automatically for hits rated</Label>
            <select
              id="screening-auto-import"
              value={data.auto_import_min_relevance ?? ''}
              disabled={save.isPending}
              onChange={(event) =>
                save.mutate({ auto_import_min_relevance: event.target.value ? (Number(event.target.value) as 4 | 5) : null })
              }
              className="h-8 rounded-lg border bg-background px-2 text-sm"
            >
              <option value="">Off</option>
              <option value="4">4 and up</option>
              <option value="5">5 only</option>
            </select>
          </div>
          <p className="text-xs text-muted-foreground">Off by default. Only free PDFs are imported; your decisions stay yours.</p>
        </div>
        </div>
      </details>
      <div className="flex flex-wrap items-center gap-2">
        {!running && (
          <Button type="button" size="sm" disabled={noModel || !hasCriteria || start.isPending} onClick={onSuggest}>
            Suggest for unscreened hits
          </Button>
        )}
        {noModel && (
          <span className="text-xs text-muted-foreground">Set up a chat model in Settings to get suggestions.</span>
        )}
        {!noModel && !hasCriteria && !running && (
          <span className="text-xs text-muted-foreground">Add a research question or inclusion criteria first.</span>
        )}
      </div>
      {data.capped && (
        <p className="text-xs text-muted-foreground">
          Auto pre-screen scored {data.capped} new hits, best first; {data.capped_left} wait for Suggest.
        </p>
      )}
      {running && (
        <div className="flex flex-col gap-1.5">
          <Progress aria-label="Suggesting" value={percent} />
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs tabular-nums text-muted-foreground">
              Suggesting… {data.suggest_done} / {data.suggest_total}
            </p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={status === 'stopping'}
              onClick={() => stop.mutate()}
            >
              Stop
            </Button>
          </div>
        </div>
      )}
      {(data.suggest_error || start.error || save.error) && (
        <Alert variant="destructive" className="border-glass-border">
          <AlertDescription>
            {data.suggest_error ?? [start.error, save.error].map((e) => e && (errorText(e.message) ?? e.message)).find(Boolean)}
          </AlertDescription>
        </Alert>
      )}
    </div>
  )
}
