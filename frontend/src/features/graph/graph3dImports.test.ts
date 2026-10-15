import { expect, it } from 'vitest'
import source from './Graph3DView.tsx?raw'

// The live dev server serves this file before its image has the hand tracker installed: a static import of hands
// code would break the whole 3D view there, not just hands mode.
const HANDS_IMPORT = /(?:from|import)\s+['"][^'"]*(?:\/hands\/|tasks-vision)/

it('loads hands mode only when it is turned on', () => {
  expect(source).not.toMatch(HANDS_IMPORT)
  expect("import {\n  X,\n} from './hands/HandsMode'").toMatch(HANDS_IMPORT) // the guard sees a wrapped import
  expect(source).toMatch(/lazy\(\s*\(\)\s*=>\s*\n?\s*import\('\.\/hands\/HandsMode'\)/)
})
