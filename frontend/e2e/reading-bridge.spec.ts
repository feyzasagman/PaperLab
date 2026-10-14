import type { APIRequestContext } from '@playwright/test'
import { expect, FIXTURE_FILE, openReader, removePaperAndNotes, test } from './fixtures'

// Same helper as workspace-search.spec.ts/workspace-snowball-prisma.spec.ts's own (not exported there, so
// duplicated here rather than reaching into a sibling spec file) — polls the run's own status via the API.
async function waitForRunToFinish(request: APIRequestContext, workspaceId: string, runId: string) {
  await expect
    .poll(async () => (await (await request.get(`/api/workspaces/${workspaceId}/search/runs/${runId}`)).json()).status, {
      timeout: 15_000,
    })
    .toBe('exhausted')
}

async function waitUntilReady(request: APIRequestContext, paperId: string) {
  await expect
    .poll(async () => (await (await request.get(`/api/papers/${paperId}`)).json()).status, { timeout: 30_000 })
    .toBe('ready')
}

// The upload creates a library paper and marks the hit's global external_ref imported; remove it even when the test
// fails, or the next run's import attaches that paper and shows no upload control.
let uploadedPaperId: string | undefined
test.afterEach(async ({ request }) => {
  if (uploadedPaperId) await removePaperAndNotes(request, uploadedPaperId)
  uploadedPaperId = undefined
})

test('shows why a paper is here, its reading queue, and never a not-yet-imported hit', async ({
  page,
  request,
  workspaceId,
  workspaceName,
  secondPaperId,
}) => {
  // 1. Get a hit to a linked paper record the same way workspace-snowball-prisma.spec.ts does (search → mark
  //    relevant → failed auto-import → manual upload): there is no API shortcut to create a workspace_search_hits
  //    row directly (grepped every route in workspace_search.py — hits only ever come from a real run or
  //    snowball), so this part has to go through the real UI. "fixture 2" is used, not "fixture 1", for the same
  //    reason both sibling specs avoid fixture 1: discovery_fake.py derives fixture 1's id as "2609.00001", which
  //    collides with FREE_ARXIV_ID and can import for free if another spec sharing this stack got there first.
  await page.goto(`/#/workspaces/${workspaceId}?tab=search`)
  await page.getByLabel('Search query').fill('paperlab pagination fixture')
  const [startResponse] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/search/runs') && r.request().method() === 'POST'),
    page.getByRole('button', { name: 'Start' }).click(),
  ])
  const { id: runId } = await startResponse.json()
  await waitForRunToFinish(request, workspaceId, runId)
  await page.reload()
  await expect(page.getByText(/[1-9]\d* in pool/)).toBeVisible()

  const title = 'paperlab pagination fixture 2'
  const row = page.locator('[data-slot="context-menu-trigger"]', { hasText: title })
  await row.getByRole('button', { name: 'Hit actions' }).click()
  // exact: true — Playwright's default substring name match makes plain 'Relevant' ambiguous against the
  // "Not relevant…" submenu trigger sharing the same open menu.
  await page.getByRole('menuitem', { name: 'Relevant', exact: true }).click()
  await expect(page.getByRole('menuitem', { name: 'Relevant', exact: true })).toHaveCount(0)

  await row.hover()
  const preview = page.locator('aside[aria-label="Hit preview"]')
  await expect(preview.getByRole('heading', { name: title })).toBeVisible()
  const [importResponse] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/search/hits/import') && r.request().method() === 'POST'),
    preview.getByRole('button', { name: 'Add PDF' }).click(),
  ])
  expect((await importResponse.json()).failed).toBe(1) // fixture 2 has no free PDF — the automatic attempt fails.

  const uploadInput = preview.getByLabel(`Upload PDF for ${title}`)
  await expect(uploadInput).toBeVisible()
  const [uploadResponse] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/upload') && r.request().method() === 'POST'),
    uploadInput.setInputFiles(FIXTURE_FILE),
  ])
  const seedHit: { id: string; paper_id: string } = await uploadResponse.json()
  uploadedPaperId = seedHit.paper_id
  await expect(preview.getByLabel(`Upload PDF for ${title}`)).toHaveCount(0)

  // 2. Priority and note are stage-1 fields with a real PATCH endpoint (HitReviewUpdate) — set directly rather
  //    than driving whatever stage-1 review UI exists, matching the brief's "real API calls to set up... before
  //    the UI assertions" once the hit itself exists.
  await expect(
    await request.patch(`/api/workspaces/${workspaceId}/search/hits/${seedHit.id}`, {
      data: { priority: 2, stage1_note: 'Reads well so far' },
    }),
  ).toBeOK()
  await waitUntilReady(request, seedHit.paper_id)

  // Scenario 1: opening the paper with a real hit shows the banner, with the right workspace, priority and note;
  // a paper with no hits at all (secondPaperId, never touched by any search) shows none.
  await openReader(page, seedHit.paper_id)
  const banner = page.locator('.reading-context-banner')
  await expect(banner).toContainText('Why this paper is here')
  await expect(banner).toContainText(workspaceName)
  await expect(banner).toContainText('Priority 2')
  await expect(banner).toContainText('Reads well so far')

  await openReader(page, secondPaperId)
  await expect(page.locator('.reading-context-banner')).toHaveCount(0)

  // 3. Stage-2 include is a real PATCH endpoint too (EligibilityUpdate) — same direct-API shortcut as priority.
  await expect(
    await request.patch(`/api/workspaces/${workspaceId}/papers/${seedHit.paper_id}/eligibility?run=${runId}`, {
      data: { status: 'include' },
    }),
  ).toBeOK()

  // Scenario 2: the Reading tab lists the included, imported paper with its priority and "Not started".
  await page.goto(`/#/workspaces/${workspaceId}?tab=reading&run=${runId}`)
  const queueRow = page.locator('.reading-queue li').filter({ has: page.locator(`a[href="#/papers/${seedHit.paper_id}"]`) })
  await expect(queueRow).toBeVisible()
  await expect(queueRow).toContainText('Priority 2')
  await expect(queueRow.locator('.reading-chip')).toHaveText('Not started')

  // Scenario 3 (Review Focus #2): a paper included in stage-2 screening for this same run, but never actually a
  // hit of this run (so acquisition never happened here — there is no workspace_search_hits row joining it to
  // this run_id) must never leak into the queue. There is no reachable UI path that produces this state (the
  // Screening tab only offers "Include" for a hit that already has paper_id set), so it's built the same
  // direct-API way: secondPaperId is a real, ready paper, added to the workspace and marked included for this
  // run without ever being searched for.
  await expect(await request.put(`/api/workspaces/${workspaceId}/papers/${secondPaperId}`)).toBeOK()
  await expect(
    await request.patch(`/api/workspaces/${workspaceId}/papers/${secondPaperId}/eligibility?run=${runId}`, {
      data: { status: 'include' },
    }),
  ).toBeOK()
  await page.reload()
  await expect(page.locator('.reading-queue li')).toHaveCount(1) // still only seedHit's paper, not secondPaperId.

  // Scenario 2, second half: set a reading pass via the reader's own Reading control (M21), then confirm the
  // Reading tab shows the updated chip on return.
  await openReader(page, seedHit.paper_id)
  await page.getByRole('button', { name: 'Reading' }).click()
  await page.getByRole('radio', { name: 'Pass 1' }).click()
  await page.keyboard.press('Escape')

  await page.goto(`/#/workspaces/${workspaceId}?tab=reading&run=${runId}`)
  await expect(queueRow.locator('.reading-chip')).toHaveText('Pass 1')
  await expect(page.locator('.reading-queue li')).toHaveCount(1)
})
