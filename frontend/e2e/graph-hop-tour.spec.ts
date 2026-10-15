import type { APIRequestContext, Page } from '@playwright/test'
import { expect, test } from './fixtures'

// Parallel-safe (D51): its own workspace, papers and links.
test.use({ reducedMotion: 'reduce' })

const WEBGL_OFF = '3D needs WebGL, which this browser has turned off. The other views work without it.'

/** Titles the three papers, puts them in this workspace and draws the owner's links between the given pairs. */
async function setUp(request: APIRequestContext, ids: string[], titles: string[], pairs: [number, number][], workspaceId: string) {
  for (const [i, id] of ids.entries()) {
    expect((await request.patch(`/api/papers/${id}`, { data: { title: titles[i] } })).status()).toBe(200)
    expect((await request.put(`/api/workspaces/${workspaceId}/papers/${id}`)).status()).toBe(204)
  }
  for (const [from, to] of pairs) {
    const drawn = await request.post('/api/links', { data: { from_paper: ids[from], to_paper: ids[to], label: 'builds on' } })
    expect(drawn.status()).toBe(201)
  }
}

async function openGraph(page: Page, workspaceName: string) {
  await page.goto('/#/graph')
  await page.getByRole('combobox', { name: 'Workspace' }).click()
  await page.getByRole('option', { name: workspaceName, exact: true }).click()
  // Only the owner's links, so every view draws exactly the chain the test drew (the test papers are copies of one PDF, so Similar content links them all).
  await page.getByRole('checkbox', { name: /^Citations/ }).uncheck()
  await page.getByRole('checkbox', { name: /^Similar content/ }).uncheck()
  await page.getByRole('checkbox', { name: /^Your links/ }).check()
}

test.describe('stepping and the tour', () => {
  test("Next and Previous step round a paper's linked papers, wrapping, and ] does it in 3D", async ({
    page,
    request,
    paperId,
    secondPaperId,
    tablePaperId,
    workspaceId,
    workspaceName,
  }) => {
    const run = workspaceName.slice(-8)
    const [alpha, beta, gamma] = [`Alpha ${run}`, `Beta ${run}`, `Gamma ${run}`]
    await setUp(request, [paperId, secondPaperId, tablePaperId], [alpha, beta, gamma], [[0, 1], [0, 2]], workspaceId)
    await openGraph(page, workspaceName)
    await page.locator(`.graph-paper[data-paper-id="${paperId}"]`).click()
    const panel = page.getByRole('region', { name: 'Connected papers' })
    const title = panel.locator('#graph-connections + p')
    await expect(title).toHaveText(alpha)

    await panel.getByRole('button', { name: 'Next linked paper' }).click()
    await expect(panel.getByText(`Linked to ${alpha}: 1 of 2`)).toBeVisible()
    const first = await title.textContent()
    expect([beta, gamma]).toContain(first)
    await panel.getByRole('button', { name: 'Next linked paper' }).click()
    await expect(panel.getByText(`Linked to ${alpha}: 2 of 2`)).toBeVisible()
    await expect(title).toHaveText(first === beta ? gamma : beta)
    await panel.getByRole('button', { name: 'Next linked paper' }).click() // round to the first again
    await expect(panel.getByText(`Linked to ${alpha}: 1 of 2`)).toBeVisible()
    await expect(title).toHaveText(first!)
    await panel.getByRole('button', { name: 'Previous linked paper' }).click()
    await expect(panel.getByText(`Linked to ${alpha}: 2 of 2`)).toBeVisible()

    // Opening a paper the ordinary way ends the stepping.
    await page.locator(`.graph-paper[data-paper-id="${paperId}"]`).click()
    await expect(panel.getByText(/^Linked to /)).toHaveCount(0)

    await page.getByRole('tablist', { name: 'Graph view' }).getByRole('tab', { name: '3D' }).click()
    await expect(page.locator('[data-view="3d"][data-ready="true"]')).toBeVisible({ timeout: 20_000 })
    test.skip((await page.getByText(WEBGL_OFF).count()) > 0, 'this browser has no WebGL 2')
    const view = page.getByRole('group', { name: '3D graph' })
    await view.focus()
    await page.keyboard.press(']')
    await expect(panel.getByText(`Linked to ${alpha}: 1 of 2`)).toBeVisible()
  })

  test('the tour opens linked papers one after another and stops when the owner picks a paper', async ({
    page,
    request,
    paperId,
    secondPaperId,
    tablePaperId,
    workspaceId,
    workspaceName,
  }) => {
    const run = workspaceName.slice(-8)
    const [alpha, beta, gamma] = [`Alpha ${run}`, `Beta ${run}`, `Gamma ${run}`]
    // A chain: Alpha – Beta – Gamma. From Alpha, following links visits Beta, then Gamma.
    await setUp(request, [paperId, secondPaperId, tablePaperId], [alpha, beta, gamma], [[0, 1], [1, 2]], workspaceId)
    await openGraph(page, workspaceName)
    await page.locator(`.graph-paper[data-paper-id="${paperId}"]`).click()
    await page.getByRole('tablist', { name: 'Graph view' }).getByRole('tab', { name: '3D' }).click()
    await expect(page.locator('[data-view="3d"][data-ready="true"]')).toBeVisible({ timeout: 20_000 })
    test.skip((await page.getByText(WEBGL_OFF).count()) > 0, 'this browser has no WebGL 2')
    await expect(page.getByRole('group', { name: '3D graph' })).toHaveAttribute('data-fitted', 'true', { timeout: 20_000 })

    const panel = page.getByRole('region', { name: 'Connected papers' })
    const title = panel.locator('#graph-connections + p')
    const tour = page.getByRole('group', { name: 'Guided tour' })
    await expect(tour.getByRole('combobox', { name: 'Tour order' })).toHaveValue('links')
    await tour.getByRole('button', { name: 'Tour' }).click()
    await expect(tour.getByRole('button', { name: 'Tour' })).toHaveAttribute('aria-pressed', 'true')
    await expect(title).toHaveText(alpha)
    await expect(title).toHaveText(beta, { timeout: 5_000 })
    await expect(title).toHaveText(gamma, { timeout: 5_000 })
    await expect(tour.getByRole('button', { name: 'Tour' })).toHaveAttribute('aria-pressed', 'false', { timeout: 5_000 })

    // Again, but the owner picks a paper mid-tour: the tour stops and leaves the camera there.
    await tour.getByRole('button', { name: 'Tour' }).click()
    await page.locator(`.graph-paper[data-paper-id="${paperId}"]`).click()
    await expect(tour.getByRole('button', { name: 'Tour' })).toHaveAttribute('aria-pressed', 'false')
    await page.waitForTimeout(3_500) // longer than one tour stop
    await expect(title).toHaveText(alpha)
  })
})
