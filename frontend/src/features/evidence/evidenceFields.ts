import type { EvidenceCard, EvidenceChange } from '@/api/client'

export type FieldKey = 'research_question' | 'design' | 'sample' | 'findings' | 'effect_sizes' | 'limitations'

export const FIELDS: { key: FieldKey; label: string; list: boolean }[] = [
  { key: 'research_question', label: 'Research question', list: false },
  { key: 'design', label: 'Design', list: false },
  { key: 'sample', label: 'Sample', list: false },
  { key: 'findings', label: 'Findings', list: true },
  { key: 'effect_sizes', label: 'Effect sizes', list: true },
  { key: 'limitations', label: 'Limitations', list: true },
]

type Item = Record<string, unknown> & { quote: string | null; page: number | null; chunk_id: string | null; flags: string[] }
export type ShownItem = { index: number; text: string; editText: string; quote: string; page: number | null; chunkId: string | null }
export type CellView = { shown: ShownItem[]; leftOut: number; edited: boolean }

const present = (v: unknown) => v !== null && v !== undefined && v !== ''

/** The item as one line of text: a sample is size and population, an effect size its value, interval and p. */
function itemText(key: FieldKey, item: Item): string {
  const parts = key === 'sample' ? [item.n, item.population] : key === 'effect_sizes' ? [item.value, item.ci, item.p] : [item.value]
  return parts.filter(present).join(key === 'sample' ? ' · ' : '; ')
}

/** What the edit box starts with: a sample's size is part of the evidence, so only its population is editable. */
const editableText = (key: FieldKey, item: Item) => String((key === 'sample' ? item.population : item.value) ?? '')

/** What a cell shows. An item with no quote was dropped: it is counted, its text is never read. */
export function cellView(card: EvidenceCard, key: FieldKey): CellView {
  const raw = card.fields[key]
  const items = (Array.isArray(raw) ? raw : raw ? [raw] : []) as Item[]
  const shown: ShownItem[] = []
  let leftOut = 0
  items.forEach((item, index) => {
    if (!item.quote) {
      if (item.flags?.some((f) => f === 'unsupported' || f === 'number_not_in_source' || f === 'no_statistic')) leftOut += 1
      return
    }
    const text = itemText(key, item)
    if (text) shown.push({ index, text, editText: editableText(key, item), quote: item.quote, page: item.page, chunkId: item.chunk_id })
  })
  const edited = items.some((item) => item.quote && item.flags?.includes('edited'))
  return { shown, leftOut, edited }
}

export const editChange = (key: FieldKey, index: number, text: string): EvidenceChange => ({
  field: key,
  ...(FIELDS.find((f) => f.key === key)?.list ? { index } : {}),
  values: { [key === 'sample' ? 'population' : 'value']: text },
})

export const leftOutText = (n: number) => `${n} left out: no supporting quote`

const REASONS: Record<string, string> = {
  not_ready: "This paper's text isn't ready yet. Try again once it has finished importing.",
  no_text: 'This paper has no readable text, so there is nothing to build a card from.',
}
export const failureReason = (error: string | null) =>
  (error && REASONS[error]) || "The model couldn't make this card. Please try again."
