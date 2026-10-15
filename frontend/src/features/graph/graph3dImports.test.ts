import { expect, it } from 'vitest'
import source from './Graph3DView.tsx?raw'

// The live dev server serves this file before its image has the hand tracker installed: a static import of hands
// code would break the whole 3D view there, not just hands mode.
it('loads hands mode only when it is turned on', () => {
  expect(source).not.toMatch(/^import[^\n]*(\.\/hands\/|tasks-vision)/m)
  expect(source).toMatch(/lazy\(\s*\(\)\s*=>\s*\n?\s*import\('\.\/hands\/HandsMode'\)/)
})
