import { Pause, Play } from 'lucide-react'
import { Button } from '@/components/ui/button'
import type { TourOrder } from './paperWalk'

export const TOUR_ORDERS: Record<TourOrder, string> = { links: 'Follow links', years: 'Oldest first' }

type Props = {
  running: boolean
  order: TourOrder
  onOrder: (order: TourOrder) => void
  onToggle: () => void
}

/** The guided tour's order and its switch, top right of the 3D view. The order can change only while it is stopped. */
export function TourControls({ running, order, onOrder, onToggle }: Props) {
  return (
    <div
      role="group"
      aria-label="Guided tour"
      className="absolute top-3 right-3 flex items-center gap-1 rounded-lg bg-background/80 p-1 shadow-sm"
    >
      <select
        aria-label="Tour order"
        value={order}
        disabled={running}
        // the picker's own keys (arrows choose an option) must not reach the 3D view's key handler
        onKeyDown={(event) => event.stopPropagation()}
        onChange={(event) => {
          const value = event.target.value
          if (value === 'links' || value === 'years') onOrder(value)
        }}
        className="h-7 rounded-md border border-input bg-transparent px-1 text-xs"
      >
        {(Object.keys(TOUR_ORDERS) as TourOrder[]).map((key) => (
          <option key={key} value={key}>
            {TOUR_ORDERS[key]}
          </option>
        ))}
      </select>
      <Button variant={running ? 'secondary' : 'ghost'} size="sm" aria-pressed={running} onClick={onToggle}>
        {running ? <Pause aria-hidden /> : <Play aria-hidden />}
        Tour
      </Button>
    </div>
  )
}
