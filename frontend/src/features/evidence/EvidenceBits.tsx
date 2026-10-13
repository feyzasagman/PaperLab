import { LoaderCircle } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { leftOutText } from './evidenceFields'

export const SourceBadge = ({ edited }: { edited: boolean }) => (
  <Badge variant="secondary">{edited ? 'AI · edited' : 'AI'}</Badge>
)

export const LeftOut = ({ count }: { count: number }) =>
  count > 0 ? <span className="text-xs text-muted-foreground">{leftOutText(count)}</span> : null

export const Making = ({ title }: { title: string }) => (
  <span role="status" aria-label={`Making the card for ${title}`} className="inline-flex items-center gap-2 text-muted-foreground">
    <LoaderCircle aria-hidden className="size-4 motion-safe:animate-spin" />
    Making the card…
  </span>
)
