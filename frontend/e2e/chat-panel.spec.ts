import type { Page } from '@playwright/test'
import { ask, expect, pickChatModel, test } from './fixtures'

const FAKE_ANSWER = 'Fake answer: the method is described here [C1].'
/** Faked responses follow the API for a paper without notes. */
const NO_NOTES = { notes: [], notes_used: 0, notes_total: 0 }

test.beforeEach(async ({ page, llmConnection }) => {
  await pickChatModel(page, llmConnection.modelId)
})

async function openChat(page: Page, paperId: string) {
  await page.goto(`/#/papers/${paperId}?tab=chat`)
  await expect(page.getByRole('tab', { name: 'Chat' })).toHaveAttribute('aria-selected', 'true')
}

type ChatStreamScript = {
  /** Token text sent one per `pull()`, each waited `delayMs` first. */
  lines: string[]
  delayMs: number
  /** Set: the last pull sends `done` and marks `window.chatSaved` -- standing in for the server saving the answer.
   *  Null: the stream just never produces anything more once `lines` runs out (still "streaming", forever). */
  done: { outputId: string } | null
}

/**
 * Installs a `window.fetch` override that answers the chat POST with a `text/event-stream` body built one event per
 * `pull()` -- not eagerly queued in `start()` -- so the mock only ever gets as far as the app has actually read: a
 * regression that stops reading (e.g. bails out of the loop early after unmount) stalls the mock exactly as it
 * would stall against a real server, instead of the mock finishing regardless of what the app does with it.
 * Records on `window.chatAborted`, and errors the body, when the app cancels the request -- like a real aborted
 * fetch would leave its reader rejecting instead of quietly still delivering queued-up data.
 */
async function installFakeChatStream(page: Page, script: ChatStreamScript) {
  await page.addInitScript((cfg: ChatStreamScript) => {
    const realFetch = window.fetch
    window.fetch = (input, init) => {
      if (init?.method !== 'POST' || !String(input).endsWith('/chat')) return realFetch(input, init)
      const encoder = new TextEncoder()
      const event = (name: string, data: object) => encoder.encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`)
      let controller: ReadableStreamDefaultController<Uint8Array> | null = null
      init.signal?.addEventListener('abort', () => {
        Object.assign(window, { chatAborted: true })
        controller?.error(new DOMException('The user aborted a request.', 'AbortError'))
      })
      let sentSources = false
      let sent = 0
      const body = new ReadableStream<Uint8Array>({
        start(c) {
          controller = c
        },
        async pull(c) {
          if (!sentSources) {
            sentSources = true
            return c.enqueue(event('sources', { whole_paper: true, sources: [], notes: [], notes_used: 0, notes_total: 0 }))
          }
          if (sent < cfg.lines.length) {
            await new Promise((resolve) => setTimeout(resolve, cfg.delayMs))
            c.enqueue(event('token', { text: cfg.lines[sent] }))
            sent += 1
            return
          }
          if (cfg.done) {
            c.enqueue(event('done', { output_id: cfg.done.outputId, model: 'fake', connection_name: null, prompt_version: 1 }))
            c.close()
            Object.assign(window, { chatSaved: true }) // stands in for "the server saved it"
            return
          }
          return new Promise(() => {}) // never resolves: the stream just keeps "streaming", exactly like `start` did
        },
      })
      return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
    }
  }, script)
}

/** A stream that sends its sources, then `tokens` lines 20 ms apart, and never ends. */
async function streamForever(page: Page, tokens: number) {
  const lines = Array.from({ length: tokens }, (_, i) => `Line ${i} of a long answer.\n`)
  await installFakeChatStream(page, { lines, delayMs: 20, done: null })
}

async function askWithoutWaiting(page: Page, question: string) {
  await page.getByRole('textbox', { name: 'Question' }).fill(question)
  await page.getByRole('textbox', { name: 'Question' }).press('Enter')
}

test('the list follows a streaming answer down as its tokens arrive', async ({ page, paperId }) => {
  // A history taller than the panel, so there is something to follow; faked so the test doesn't ask a dozen times.
  const answers = Array.from({ length: 12 }, (_, i) => ({
    id: `00000000-0000-4000-8000-0000000002${String(i).padStart(2, '0')}`,
    question: `Question ${i}?`,
    content: 'An answer long enough to take a few lines in the side panel, so that twelve of them overflow it.',
    model: 'fake',
    connection_name: null,
    prompt_version: 1,
    created_at: '2026-09-14T00:00:00Z',
    whole_paper: true,
    sources: [],
    ...NO_NOTES,
  }))
  await page.route('**/chat', (route) => (route.request().method() === 'GET' ? route.fulfill({ json: answers }) : route.fallback()))
  // The stream never ends, so the status stays `streaming`: only the token updates can keep the list at the bottom.
  await streamForever(page, 40)
  await openChat(page, paperId)

  await askWithoutWaiting(page, 'A long one?')
  const live = page.locator('article.chat-answer', { hasText: 'A long one?' })
  await expect(live.locator('.chat-answer-text')).toContainText('Line 39 of a long answer.')

  const list = live.locator('..')
  // Without this the distance below is 0 on a list that doesn't scroll at all, and the poll passes whatever the app does.
  expect(await list.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true)
  await expect.poll(() => list.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(2)
})

/** A stream that sends its sources, then `lines` 100 ms apart, then a `done` event for `outputId`. */
async function streamThenSave(page: Page, outputId: string, lines: string[]) {
  await installFakeChatStream(page, { lines, delayMs: 100, done: { outputId } })
}

test('leaving the reader while an answer streams still saves the answer', async ({ page, paperId }) => {
  const outputId = '00000000-0000-4000-8000-000000000401'
  const lines = ['Line 0. ', 'Line 1. ', 'Line 2. ', 'Line 3. ', 'Line 4. ']
  const saved = {
    id: outputId,
    question: 'Still saved?',
    content: lines.join(''),
    model: 'fake',
    connection_name: null,
    prompt_version: 1,
    created_at: '2026-09-15T00:00:00Z',
    whole_paper: true,
    sources: [],
    ...NO_NOTES,
  }
  await streamThenSave(page, outputId, lines)
  // Stands in for the real backend's history: empty until the stream (still running above) marks itself saved.
  await page.route('**/chat', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const finished = await page.evaluate(() => (window as unknown as { chatSaved?: boolean }).chatSaved === true)
    await route.fulfill({ json: finished ? [saved] : [] })
  })

  await openChat(page, paperId)
  await askWithoutWaiting(page, 'Still saved?')
  await expect(page.locator('article.chat-answer', { hasText: 'Still saved?' }).locator('.chat-answer-text')).toContainText('Line 0')

  // Leaving before `done`: a hash change, not a reload, so the page (and its in-flight fetch) survives.
  await page.evaluate(() => (window.location.hash = '#/'))
  await expect(page.getByRole('heading', { name: 'PaperLab', level: 1 })).toBeVisible()

  // The background stream keeps running though nothing is listening, and finishes.
  await expect.poll(() => page.evaluate(() => (window as unknown as { chatSaved?: boolean }).chatSaved)).toBe(true)

  await page.evaluate((id) => (window.location.hash = `#/papers/${id}?tab=chat`), paperId)
  const answer = page.locator('article.chat-answer', { hasText: 'Still saved?' })
  await expect(answer.locator('.chat-answer-footer')).toContainText('AI · ')
  await expect(answer.locator('.chat-answer-text')).toContainText('Line 4.')

  expect(await page.evaluate(() => (window as unknown as { chatAborted?: boolean }).chatAborted)).not.toBe(true)
})

test('a malformed event ends the answer with Retry and warns in the console', async ({ page, paperId }) => {
  const warnings: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'warning') warnings.push(message.text())
  })
  await page.route('**/chat', (route) =>
    route.request().method() === 'POST'
      ? route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: `event: sources\ndata: ${JSON.stringify({ whole_paper: true, sources: [], ...NO_NOTES })}\n\nevent: token\ndata: {not json\n\n`,
        })
      : route.fallback(),
  )
  await openChat(page, paperId)
  await askWithoutWaiting(page, 'Malformed?')

  const answer = page.locator('article.chat-answer', { hasText: 'Malformed?' })
  await expect(answer.getByRole('alert')).toContainText('The answer stopped before it finished.')
  await expect.poll(() => warnings.some((text) => text.startsWith('chat stream ended early'))).toBe(true)
})

test('a citation reached with the keyboard shows a focus ring', async ({ page, paperId }) => {
  await openChat(page, paperId)
  const answer = await ask(page, 'Where is the ring?')
  const cite = answer.locator('.chat-cite')
  // Unfocused first, so the poll below can only pass on a ring the focus itself put there.
  expect(await cite.evaluate((element) => getComputedStyle(element).boxShadow)).toBe('none')

  await cite.focus() // after typing the question, so the browser treats this focus as keyboard focus

  await expect(cite).toBeFocused()
  await expect.poll(() => cite.evaluate((element) => getComputedStyle(element).boxShadow)).not.toBe('none')
})

test('selecting part of an answer saves just that part, anchored on its citation', async ({ page, request, paperId }) => {
  await openChat(page, paperId)
  const answer = await ask(page, 'Which part?')
  const text = answer.locator('.chat-answer-text')
  // From "the method" to the end of "[C1]": a selection that starts inside the first text run and ends on the citation.
  await text.evaluate((root) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    const nodes: Text[] = []
    for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node as Text)
    const start = nodes.find((node) => node.data.includes('the method'))!
    // `[C1]` renders as three text nodes ("[", "C1", "]"), each its own JSX child: end on the closing "]".
    const cite = nodes.find((node) => node.data === ']')!
    const range = document.createRange()
    range.setStart(start, start.data.indexOf('the method'))
    range.setEnd(cite, cite.data.length)
    window.getSelection()!.removeAllRanges()
    window.getSelection()!.addRange(range)
  })
  await text.dispatchEvent('mouseup')

  await page.getByRole('button', { name: 'Save as note' }).click()
  await expect.poll(async () => (await (await request.get(`/api/papers/${paperId}/notes`)).json()).length).toBe(1)
  const [note] = await (await request.get(`/api/papers/${paperId}/notes`)).json()
  expect(note.body).toBe('the method is described here [C1]')
  expect(FAKE_ANSWER).toContain(note.body)
})

test('Save as note follows its selection when the panel is resized while the Notes tab is shown', async ({ page, paperId }) => {
  // A long saved answer, so a wider panel rewraps it into fewer lines and moves the selection's bottom edge.
  const long = { ...NO_NOTES, id: '00000000-0000-4000-8000-000000000301', question: 'Long?', model: 'fake', connection_name: null }
  const content = 'A long answer that wraps onto many lines in a narrow panel and onto far fewer in a wide one. '.repeat(6)
  await page.route('**/chat', (route) =>
    route.request().method() === 'GET'
      ? route.fulfill({ json: [{ ...long, content, prompt_version: 1, created_at: '2026-09-15T00:00:00Z', whole_paper: true, sources: [] }] })
      : route.fallback(),
  )
  await openChat(page, paperId)
  const text = page.locator('article.chat-answer .chat-answer-text')
  await text.evaluate((node) => {
    const range = document.createRange()
    range.selectNodeContents(node)
    window.getSelection()!.removeAllRanges()
    window.getSelection()!.addRange(range)
  })
  await text.dispatchEvent('mouseup')
  const save = page.locator('.save-as-note')
  await expect(save).toBeVisible()

  // Clicking a tab keeps the page selection (Chromium), so the button comes back with the Chat tab.
  await page.getByRole('tab', { name: 'Notes' }).click()
  await page.getByRole('separator', { name: 'Resize panel' }).focus()
  for (let i = 0; i < 20; i++) await page.keyboard.press('ArrowLeft') // 16 px each: the panel grows to its 60% cap
  await page.getByRole('tab', { name: 'Chat' }).click()

  await expect
    .poll(async () => {
      const bottom = await page.evaluate(() => window.getSelection()!.getRangeAt(0).getBoundingClientRect().bottom)
      const box = await save.boundingBox()
      return box ? Math.abs(box.y - (bottom + 6)) : Number.POSITIVE_INFINITY
    })
    .toBeLessThan(2)
})

test("a refused promote shows the server's own 422 text", async ({ page, paperId }) => {
  await openChat(page, paperId)
  const answer = await ask(page, 'Refused save?')
  await page.route('**/api/notes/promote', (route) =>
    route.fulfill({
      status: 422,
      json: { detail: [{ type: 'string_too_long', loc: ['body', 'body'], msg: 'String should have at most 50000 characters' }] },
    }),
  )
  const text = answer.locator('.chat-answer-text')
  await text.evaluate((node) => {
    const range = document.createRange()
    range.selectNodeContents(node)
    window.getSelection()!.removeAllRanges()
    window.getSelection()!.addRange(range)
  })
  await text.dispatchEvent('mouseup')

  await page.getByRole('button', { name: 'Save as note' }).click()

  const save = page.locator('.save-as-note')
  const alert = save.getByRole('alert')
  await expect(alert).toHaveText('Check these fields: body.')
  const before = await save.boundingBox()

  // A resize re-measures the button (and re-captures the still-unchanged selection) but must not silently clear
  // an error the user hasn't acted on. Wait for the re-measure to actually land (the button moves) before checking
  // the error is still there, so the assertion can't pass just because it ran before the resize took effect.
  await page.getByRole('separator', { name: 'Resize panel' }).focus()
  await page.keyboard.press('ArrowLeft')
  await expect.poll(async () => (await save.boundingBox())?.x).not.toBe(before?.x)
  await expect(alert).toHaveText('Check these fields: body.')
})

test('a markdown answer shows as a list with bold text, and a selection in it still saves the answer verbatim', async ({ page, paperId }) => {
  const content = 'Key findings:\n\n1. **Code-Level Metrics**: AI code is similar [C1].\n2. **Security**: fewer alerts.'
  const chunkId = '00000000-0000-4000-8000-000000000303'
  const saved = {
    ...NO_NOTES,
    id: '00000000-0000-4000-8000-000000000302',
    question: 'Findings?',
    model: 'fake',
    connection_name: null,
    content,
    prompt_version: 1,
    created_at: '2026-09-15T00:00:00Z',
    whole_paper: false,
    sources: [{ label: 'C1', chunk_id: chunkId, paper_id: paperId, page: 1, section: null, bbox: [] }],
  }
  await page.route('**/chat', (route) => (route.request().method() === 'GET' ? route.fulfill({ json: [saved] }) : route.fallback()))
  await openChat(page, paperId)

  const text = page.locator('article.chat-answer .chat-answer-text')
  await expect(text.locator('ol > li')).toHaveCount(2)
  await expect(text.locator('strong')).toHaveText(['Code-Level Metrics', 'Security'], { useInnerText: true })
  expect(await text.innerText()).not.toContain('**')
  // The syntax is only hidden, so selection offsets in the element are still offsets into the saved answer.
  expect(await text.evaluate((node) => node.textContent)).toBe(content)

  // From inside the bold words to the end of "[C1]".
  await text.evaluate((root) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    const nodes: Text[] = []
    for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node as Text)
    const start = nodes.find((node) => node.data.includes('Metrics'))!
    const cite = nodes.find((node) => node.data === ']')!
    const range = document.createRange()
    range.setStart(start, start.data.indexOf('Metrics'))
    range.setEnd(cite, cite.data.length)
    window.getSelection()!.removeAllRanges()
    window.getSelection()!.addRange(range)
  })
  await text.dispatchEvent('mouseup')

  await page.route('**/api/notes/promote', (route) => route.fulfill({ status: 422, json: { detail: 'not saved in this test' } }))
  const promote = page.waitForRequest('**/api/notes/promote')
  await page.getByRole('button', { name: 'Save as note' }).click()
  expect((await promote).postDataJSON()).toEqual({ output_id: saved.id, body: 'Metrics**: AI code is similar [C1]', chunk_ids: [chunkId] })
})

test('a small 3D page shows while the answer has no words yet, and goes when the first words arrive', async ({ page, paperId }) => {
  // Sources at once, the first token 3 s later: long enough to see the waiting state.
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  // A paper with no evidence card answers 404 by design (queries.ts reads it as "no card"); the browser logs
  // that as a console error, which is not a page error.
  page.on('console', (m) => m.type() === 'error' && !m.location().url.endsWith('/evidence-card') && errors.push(m.text()))
  await installFakeChatStream(page, { lines: ['The first words.'], delayMs: 3000, done: null })
  await openChat(page, paperId)
  await askWithoutWaiting(page, 'Still waiting?')
  const live = page.locator('article.chat-answer', { hasText: 'Still waiting?' })
  await expect(live.getByRole('status')).toHaveText('Writing the answer…') // the words screen readers hear, unchanged
  const webgl = await page.evaluate(() => document.createElement('canvas').getContext('webgl2') !== null)
  const canvas = live.locator('canvas[aria-hidden="true"]')
  if (webgl) await expect(canvas).toBeVisible() // hidden until the director runs, so this proves it drew
  else await expect(canvas).toHaveCount(0)
  await expect(live.locator('.chat-answer-text')).toContainText('The first words.')
  await expect(live.locator('canvas')).toHaveCount(0)
  expect(errors).toEqual([])
})
