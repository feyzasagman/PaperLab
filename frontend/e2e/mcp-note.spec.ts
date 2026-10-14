import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { expect, openReader, test } from './fixtures'

// The repository root, where `docker compose -p paperlab-e2e` finds the test stack, and the stdio client that runs inside the api container.
const E2E_PROJECT = process.env.E2E_COMPOSE_PROJECT ?? 'paperlab-e2e'
const REPO = fileURLToPath(new URL('../..', import.meta.url))
const MCP_CALL = readFileSync(fileURLToPath(new URL('./mcp_call.py', import.meta.url)), 'utf8')
/** In the fixture paper's first paragraph, from the end of line 2 into line 3: two line rects, not the paragraph. */
const QUOTE = 'This paragraph exists so the end-to-end test has real, selectable text'

type Rect = [number, number, number, number]
type McpNote = { id: string; provenance: string; anchors: { page: number; bbox: Rect[]; quoted_text: string }[] }

/** Calls a tool through the real MCP server over stdio, inside the api container, as Claude Desktop launches it (D89). */
function callTool<T>(tool: string, args: Record<string, unknown>): { is_error: boolean; result: T } {
  const out = execFileSync('docker', ['compose', '-p', E2E_PROJECT, 'exec', '-T', 'api', 'python', '-', tool, JSON.stringify(args)], {
    cwd: REPO,
    input: MCP_CALL,
    encoding: 'utf8',
    timeout: 60_000,
  })
  return JSON.parse(out.trim().split('\n').at(-1)!)
}

test('a note written through MCP shows the AI badge, highlighted on the quoted lines', async ({ page, paperId }) => {
  const created = callTool<McpNote>('create_note', {
    body: 'The fixture paragraph is there to be selected.',
    paper_id: paperId,
    quoted_text: QUOTE,
  })
  expect(created.is_error, JSON.stringify(created.result)).toBe(false)
  const note = created.result
  expect(note.provenance).toBe('llm')
  expect(note.anchors).toHaveLength(1)
  expect(note.anchors[0].page).toBe(1)
  expect(note.anchors[0].bbox).toHaveLength(2)

  await openReader(page, paperId)
  const highlights = page.locator(`.pdf-page[data-page="1"] .highlight[data-note-id="${note.id}"]`)
  await expect(highlights).toHaveCount(note.anchors[0].bbox.length)

  // The first rect starts mid-line on "This paragraph exists so the", the second covers the start of the next line.
  const lineTwo = page.locator('.pdf-page[data-page="1"] .textLayer span', { hasText: 'This paragraph exists so the' })
  const lineThree = page.locator('.pdf-page[data-page="1"] .textLayer span', { hasText: 'end-to-end test has real' })
  const boxes = await Promise.all([highlights.nth(0), highlights.nth(1), lineTwo, lineThree].map((l) => l.boundingBox()))
  const [first, second, two, three] = boxes.map((box) => {
    if (!box) throw new Error('a highlight or its line is not rendered')
    return box
  })
  const [upper, lower] = first.y < second.y ? [first, second] : [second, first]
  const overlapsVertically = (a: typeof first, b: typeof first) => a.y < b.y + b.height && b.y < a.y + a.height
  expect(overlapsVertically(upper, two)).toBe(true)
  expect(upper.x).toBeGreaterThan(two.x + two.width / 2) // the quoted words at the end of the line, not the paragraph
  expect(overlapsVertically(lower, three)).toBe(true)
  expect(lower.x).toBeLessThan(three.x + three.width / 2)

  await expect(page.locator(`article.note[data-note-id="${note.id}"] .provenance-badge`)).toHaveText('AI')
})
