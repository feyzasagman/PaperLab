import type { APIRequestContext } from '@playwright/test'
import { expect, FIXTURE_FILE, removePaperAndNotes, test } from './fixtures'

// Same helper as workspace-search.spec.ts's own (not exported there, so duplicated here rather than reaching into
// a sibling spec file) — polls the run's own status via the API instead of sleeping.
async function waitForRunToFinish(request: APIRequestContext, workspaceId: string, runId: string) {
  await expect
    .poll(async () => (await (await request.get(`/api/workspaces/${workspaceId}/search/runs/${runId}`)).json()).status, {
      timeout: 15_000,
    })
    .toBe('exhausted')
}

test('snowball from an imported paper, screen it stage-2, see it in the PRISMA export', async ({ page, request, workspaceId }) => {
  // 1. Get a hit to 'manual' acquisition status with a linked paper record: the seed for both screening and
  //    snowball. ScreeningTab only lists hits whose acquisition_status is 'imported' or 'manual' (see its own
  //    docstring) — a paper uploaded straight through the papers API is never a hit and would never show up
  //    there, so this follows workspace-search.spec.ts's same underlying idea (search → mark relevant → failed
  //    auto-import → manual upload), not the brief outline's plain "import a paper via the papers API".
  //
  //    The concrete UI steps had to be adapted, though, not just copied: HitTable.tsx/HitPreview.tsx have been
  //    refactored since that sibling spec was written. There is no more bulk "Import all with PDF in this filter"
  //    button (HitPreview.tsx's own docstring: "there is no 'import everything in this filter' action anywhere in
  //    this feature" — acquisition is deliberately per-hit now), and acquisition (both the automatic "Add PDF"
  //    attempt and the manual upload fallback) happens in the hovered/focused hit's own HitPreview side panel,
  //    without ever needing the separate "Manual acquisition" tab. Confirmed by running workspace-search.spec.ts
  //    itself on this same stack: it fails with the identical "no such button" timeout, so this is a real,
  //    pre-existing regression in that sibling spec (and, separately, the "Relevant" menuitem below is now
  //    ambiguous against "Not relevant…"'s submenu trigger) — unrelated to this task, and out of scope to fix
  //    there per the brief's file list, so this spec works around it (see the `exact: true` below) rather than
  //    editing that file.
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

  // "fixture 2" (not "fixture 1"): deterministically has no free PDF, so it always needs manual acquisition —
  // see workspace-search.spec.ts's own comment on why fixture 1 is avoided.
  const title = 'paperlab pagination fixture 2'
  const row = page.locator('[data-slot="context-menu-trigger"]', { hasText: title })
  await row.getByRole('button', { name: 'Hit actions' }).click()
  // exact: true — Playwright's default substring name match makes plain 'Relevant' ambiguous against the
  // "Not relevant…" submenu trigger (HitMenu.tsx) sharing the same open menu.
  await page.getByRole('menuitem', { name: 'Relevant', exact: true }).click()
  await expect(page.getByRole('menuitem', { name: 'Relevant', exact: true })).toHaveCount(0)

  // Hover the row to bring its own preview into the side panel (HitTable.tsx: hover/focus-driven, 150ms debounced)
  // — asserting on the preview's heading waits out that debounce instead of a sleep. Scoped to the "Hit preview"
  // aside throughout: ManualAcquisitionTab (also force-mounted by WorkspacePage) independently lists this same
  // hit once it's 'failed' below, with its own identically-labelled upload input, so an unscoped getByLabel would
  // be ambiguous between the two.
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
  const seedHit = await uploadResponse.json()
  await expect(preview.getByLabel(`Upload PDF for ${title}`)).toHaveCount(0)

  // On a shared dev DB every source's fake can already have put the snowball target in the pool; then a clean hop
  // legitimately adds nothing, and only "no skipped seed, no error" can be checked.
  type Hits = { items: { title: string; source_method: string }[] }
  const SNOWBALL_TARGET = 'PaperLab Find Papers Fixture'
  const hitsBefore: Hits = await (await request.get(`/api/workspaces/${workspaceId}/search/hits?limit=50`)).json()
  const targetAlreadyPooled = hitsBefore.items.some((hit) => hit.title === SNOWBALL_TARGET)

  // 2/3. Screening tab: click Snowball on the seed paper's row, wait for the response.
  await page.getByRole('tab', { name: 'Screening' }).click()
  const [snowballResponse] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/search/snowball') && r.request().method() === 'POST'),
    page.getByRole('button', { name: 'Snowball' }).click(),
  ])
  const snowballResult = await snowballResponse.json()
  expect(snowballResult.skipped_seeds).toEqual([])
  expect(snowballResult.errors).toEqual({})

  // 4. Snowball hits land in the same pool as database-search hits, but by design nothing invalidates the full
  //    hit pool on snowball (useSnowball's docstring in queries.ts) — Search/Screening never auto-refresh. Confirm
  //    the new hit(s) landed via the API directly, rather than sleeping or asserting on a UI list that was never
  //    told to refetch.
  const hitsAfter: Hits = await (await request.get(`/api/workspaces/${workspaceId}/search/hits?limit=50`)).json()
  expect(hitsAfter.items.some((hit) => hit.title === SNOWBALL_TARGET)).toBe(true)
  if (!targetAlreadyPooled) {
    expect(snowballResult.new_hits).toBeGreaterThan(0)
    expect(hitsAfter.items.some((hit) => hit.source_method.startsWith('snowball'))).toBe(true)
  }

  // 5. Mark the seed paper Include.
  await Promise.all([
    page.waitForResponse((r) => r.url().includes('/eligibility') && r.request().method() === 'PATCH'),
    page.getByRole('button', { name: 'Include' }).click(),
  ])

  // 6. PRISMA tab: the eligibility mutation invalidates its query (Task 10's cache invalidation), so the funnel
  //    should update with no reload — reloading here would hide a real invalidation bug instead of catching it.
  await page.getByRole('tab', { name: 'PRISMA' }).click()
  const includedRow = page.locator('.contents', { hasText: 'Included' })
  await expect(includedRow.locator('dd')).toHaveText(/[1-9]\d*/)

  // 7. Clean up what this test created — the workspace fixture's own teardown removes the workspace (and,
  //    through cascading FKs, its search runs, hits and eligibility rows), but papers and notes survive that, so
  //    the seed paper needs its own cleanup, same as workspace-search.spec.ts's sibling flow.
  await removePaperAndNotes(request, seedHit.paper_id)
})
