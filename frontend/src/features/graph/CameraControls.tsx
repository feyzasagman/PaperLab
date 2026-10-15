import { Hand, Minus, Plus, RotateCcw, RotateCw, Scan, type LucideIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import type { CameraAction } from './cameraMoves'

const BUTTONS: [CameraAction, string, LucideIcon][] = [
  ['zoom-in', 'Zoom in', Plus],
  ['zoom-out', 'Zoom out', Minus],
  ['rotate-left', 'Rotate left', RotateCcw],
  ['rotate-right', 'Rotate right', RotateCw],
  ['reset', 'Reset view', Scan],
]

export const HINT = 'Drag to rotate · Right-drag to move · Scroll to zoom · Click a paper to select it'

/**
 * The 3D view's on-screen moves, and a one-line how-to until the owner first moves the view. It only says which
 * move was asked for; the view decides what that does to the camera.
 */
type Props = {
  onAction: (action: CameraAction) => void
  showHint: boolean
  /** Hands mode's on/off switch, when the view offers it. */
  hands?: { on: boolean; onToggle: () => void }
}

export function CameraControls({ onAction, showHint, hands }: Props) {
  return (
    <>
      {showHint && (
        <p className="pointer-events-none absolute bottom-3 left-3 max-w-[calc(100%-4.5rem)] rounded-md bg-background/80 px-2 py-1 text-xs text-muted-foreground">
          {HINT}
        </p>
      )}
      {/* A group, not a toolbar: a toolbar promises arrow keys move between its buttons, but here they move the view. */}
      <div
        role="group"
        aria-label="Move the 3D view"
        className="absolute right-3 bottom-3 flex flex-col gap-1 rounded-lg bg-background/80 p-1 shadow-sm"
      >
        {BUTTONS.map(([action, label, Icon]) => (
          <Button key={action} variant="ghost" size="icon-sm" aria-label={label} title={label} onClick={() => onAction(action)}>
            <Icon aria-hidden />
          </Button>
        ))}
        {hands && (
          <Button
            variant={hands.on ? 'secondary' : 'ghost'}
            size="icon-sm"
            aria-label="Hands mode"
            aria-pressed={hands.on}
            title="Hands mode"
            onClick={hands.onToggle}
          >
            <Hand aria-hidden />
          </Button>
        )}
      </div>
    </>
  )
}
