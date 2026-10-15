import type { APIRequestContext, Page } from '@playwright/test'
import { expect, test } from './fixtures'

// Parallel-safe (D51): every assertion is scoped to this test's own workspaces, so other specs' papers and the
// owner's library never change what a view shows. Changes no settings, so no tag.

const WEBGL_OFF = '3D needs WebGL, which this browser has turned off. The other views work without it.'
const WEBGL_FAILED = "3D couldn't start in this browser. The other views work without it."

/** Picks a workspace by its exact name: a second one here starts with the same name. */
async function scopeTo(page: Page, name: string) {
  await page.getByRole('combobox', { name: 'Workspace' }).click()
  await page.getByRole('option', { name, exact: true }).click()
}

test.describe('the graph views', () => {
  test('switch between five views of one shared state, and the choice survives a reload', async ({
    page,
    request,
    paperId,
    secondPaperId,
    tablePaperId,
    workspaceId,
    workspaceName,
  }) => {
    // Three papers with titles of their own (two are copies of one PDF), linked Alpha → Beta → Gamma by the owner.
    // Beta has no year, so the Timeline has a Year unknown lane.
    const run = workspaceName.slice(-8)
    const [alpha, beta, gamma] = [`Alpha ${run}`, `Beta ${run}`, `Gamma ${run}`]
    const papers = [
      [paperId, alpha, 2021],
      [secondPaperId, beta, null],
      [tablePaperId, gamma, 2023],
    ] as const
    for (const [id, title, year] of papers) {
      expect((await request.patch(`/api/papers/${id}`, { data: { title, year } })).status()).toBe(200)
      expect((await request.put(`/api/workspaces/${workspaceId}/papers/${id}`)).status()).toBe(204)
    }
    for (const [from, to] of [
      [paperId, secondPaperId],
      [secondPaperId, tablePaperId],
    ]) {
      const drawn = await request.post('/api/links', { data: { from_paper: from, to_paper: to, label: 'builds on' } })
      expect(drawn.status()).toBe(201)
    }

    await page.goto('/#/graph')
    await scopeTo(page, workspaceName)
    // Only the owner's own links, so every view draws exactly Alpha – Beta – Gamma.
    await page.getByRole('checkbox', { name: /^Citations/ }).uncheck()
    await page.getByRole('checkbox', { name: /^Similar content/ }).uncheck()
    await page.getByRole('checkbox', { name: /^Your links/ }).check()
    await expect(page.locator('.status-bar')).toHaveText(/^3 papers, 2 links/)

    // The switcher, in the spec's order, on 2D.
    const tabs = page.getByRole('tablist', { name: 'Graph view' })
    await expect(tabs.getByRole('tab')).toHaveText(['2D', '3D', 'Matrix', 'Timeline', 'Rings'])
    await expect(tabs.getByRole('tab', { name: '2D' })).toHaveAttribute('aria-selected', 'true')
    await expect(page.locator('[data-view="2d"][data-ready="true"]')).toBeVisible()

    // 3D loads its own module on first pick; the page itself says whether it can draw WebGL (Spec note 4).
    // Registered before the click, so an error during the entrance is caught too.
    const errors: string[] = []
    page.on('pageerror', (error) => errors.push(String(error)))
    await tabs.getByRole('tab', { name: '3D' }).click()
    await expect(page.locator('[data-view="3d"][data-ready="true"]')).toBeVisible({ timeout: 20_000 })
    const webgl = await page.evaluate(() => {
      const canvas = document.createElement('canvas')
      return canvas.getContext('webgl2') !== null
    })
    await expect(page.getByText(WEBGL_OFF)).toHaveCount(webgl ? 0 : 1)
    if (webgl) await expect(page.locator('[data-view="3d"] canvas')).toBeVisible()
    // The entrance starts the papers on the logo and lets them spring out; the page must stay error-free through it.
    await page.waitForTimeout(1500)
    expect(errors).toEqual([])

    // The Matrix: a real table, a name on each linked cell, and a row header that focuses its paper.
    await tabs.getByRole('tab', { name: 'Matrix' }).click()
    await expect(page.locator('[data-view="matrix"]')).toBeVisible()
    const table = page.getByRole('table', { name: 'Links between your papers' })
    await expect(table.getByRole('cell', { name: `${alpha} and ${beta}: Your links` })).toBeVisible()
    await expect(table.getByRole('cell', { name: `${alpha} and ${gamma}: Your links` })).toHaveCount(0)
    await table.getByRole('rowheader', { name: gamma, exact: true }).getByRole('button').click()
    const panel = page.getByRole('region', { name: 'Connected papers' })
    await expect(panel.getByText(gamma, { exact: true })).toBeVisible()

    // The panel says which way a link points, and with Links out at 2, what is two links away.
    await page.locator(`.graph-paper[data-paper-id="${paperId}"]`).click()
    await expect(panel.locator('.graph-connection', { hasText: beta })).toContainText('your link to it: builds on')
    await page.getByRole('combobox', { name: 'Links out' }).click()
    await page.getByRole('option', { name: '2 links' }).click()
    await expect(panel.getByRole('heading', { name: '2 links away' })).toBeVisible()
    await expect(panel.locator(`.graph-away[data-paper-id="${tablePaperId}"]`)).toHaveText(gamma)

    // The Timeline: by year published, Beta sits in the unknown lane; by date added, every paper is placed.
    await tabs.getByRole('tab', { name: 'Timeline' }).click()
    await expect(page.locator('[data-view="timeline"] svg')).toBeVisible()
    await expect(page.getByText('Year unknown')).toBeVisible()
    await expect(page.getByText('Turn on OpenAlex in Settings to fill in missing years.')).toBeVisible()
    await page.getByRole('combobox', { name: 'Time axis' }).click()
    await page.getByRole('option', { name: 'Date added' }).click()
    await expect(page.getByText('Year unknown')).toHaveCount(0)
    await expect(page.getByText('Turn on OpenAlex in Settings to fill in missing years.')).toHaveCount(0)

    // Rings: with nothing focused the most connected paper is the centre, and Links out shows anyway.
    await panel.getByRole('button', { name: 'Clear focus' }).click()
    await tabs.getByRole('tab', { name: 'Rings' }).click()
    const centred = page.getByText('Centred on the most connected paper. Choose a paper to centre on it.')
    await expect(centred).toBeVisible()
    await expect(page.locator('[data-view="rings"][data-ready="true"]')).toBeVisible()
    await expect(page.getByRole('combobox', { name: 'Links out' })).toBeVisible()
    await page.locator(`.graph-paper[data-paper-id="${tablePaperId}"]`).click()
    await expect(centred).toBeHidden()

    // The chosen view is remembered in this browser.
    await page.reload()
    await expect(page.getByRole('tab', { name: 'Rings' })).toHaveAttribute('aria-selected', 'true')
    await expect(page.locator('[data-view="rings"]')).toBeVisible()
  })

  test('the panel clears a failed removal, and keeps keyboard focus when a workspace switch drops the focused paper', async ({
    page,
    request,
    paperId,
    secondPaperId,
    workspaceId,
    workspaceName,
  }) => {
    for (const id of [paperId, secondPaperId]) {
      expect((await request.put(`/api/workspaces/${workspaceId}/papers/${id}`)).status()).toBe(204)
    }
    // A second workspace with only the second paper. Its name starts with `workspaceName`, so the fixture deletes it.
    const otherName = `${workspaceName} B`
    const other = await request.post('/api/workspaces', { data: { name: otherName } })
    expect(other.status()).toBe(201)
    const otherId: string = (await other.json()).id
    expect((await request.put(`/api/workspaces/${otherId}/papers/${secondPaperId}`)).status()).toBe(204)
    const drawn = await request.post('/api/links', {
      data: { from_paper: paperId, to_paper: secondPaperId, label: 'builds on' },
    })
    expect(drawn.status()).toBe(201)
    const linkId: string = (await drawn.json()).id

    await page.goto('/#/graph')
    await scopeTo(page, workspaceName)
    await page.getByRole('checkbox', { name: /^Your links/ }).check()
    await page.locator(`.graph-paper[data-paper-id="${paperId}"]`).click()
    const panel = page.getByRole('region', { name: 'Connected papers' })
    await expect(panel.locator('.graph-connection', { hasText: 'your link to it: builds on' })).toBeVisible()

    // A removal that fails (the link went behind the page's back) shows its error until the focused paper changes.
    expect((await request.delete(`/api/links/${linkId}`)).status()).toBe(204)
    page.once('dialog', (dialog) => void dialog.accept())
    await panel.getByRole('button', { name: /^Remove link to / }).click()
    const failure = page.getByRole('alert')
    await expect(failure).toBeVisible()
    await page.locator(`.graph-paper[data-paper-id="${secondPaperId}"]`).click()
    await expect(failure).toBeHidden()

    // Hold the second workspace's graph back, click the first paper in the list still on screen, then let it land
    // without that paper: keyboard focus moves to the Papers heading instead of falling to <body>.
    let release = () => {}
    const held = new Promise<void>((resolve) => {
      release = resolve
    })
    await page.route(
      (url) => url.pathname === '/api/graph' && url.searchParams.get('workspace') === otherId,
      async (route) => {
        await held
        await route.continue()
      }
    )
    await scopeTo(page, otherName)
    await page.locator(`.graph-paper[data-paper-id="${paperId}"]`).click()
    await expect(page.getByRole('heading', { name: 'Connected papers' })).toBeFocused()
    release()
    await expect(page.locator(`.graph-paper[data-paper-id="${paperId}"]`)).toHaveCount(0)
    await expect(page.getByRole('heading', { name: 'Papers', exact: true })).toBeFocused()
    await expect(panel).toHaveCount(0)
  })

  test('a failed workspaces list shows the load error, and Retry recovers it', async ({
    page,
    request,
    paperId,
    secondPaperId,
    workspaceId,
    workspaceName,
  }) => {
    for (const id of [paperId, secondPaperId]) {
      expect((await request.put(`/api/workspaces/${workspaceId}/papers/${id}`)).status()).toBe(204)
    }
    await page.route('**/api/workspaces', (route) =>
      route.fulfill({ status: 500, json: { detail: 'Workspaces are unavailable.' } })
    )
    // Wait for the graph's own fetch to settle first: the page must keep showing the load error on the
    // workspaces failure alone, not just while the graph is still loading too.
    const [graphLoaded] = await Promise.all([
      page.waitForResponse((response) => response.url().includes('/api/graph')),
      page.goto('/#/graph'),
    ])
    expect(graphLoaded.status()).toBe(200)
    // The view's own alert, not the rail's: the rail says its list is broken on every page, while this is the
    // one that offers Retry.
    await expect(page.getByRole('main').getByRole('alert')).toContainText('Workspaces are unavailable.')
    await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible()
    await expect(page.locator('[data-view]')).toHaveCount(0)

    await page.unroute('**/api/workspaces')
    await page.getByRole('button', { name: 'Retry' }).click()
    // Scoped to this test's own workspace (D51): the whole library may have no nodes or no default-layer links,
    // which would show the empty state instead and hang this assertion even though the app is correct.
    await scopeTo(page, workspaceName)
    await expect(page.locator('[data-view]').first()).toBeVisible()
  })

  test('a WebGL-1-only browser shows the WebGL message on 3D, and 2D still draws', async ({
    page,
    request,
    paperId,
    secondPaperId,
    workspaceId,
    workspaceName,
  }) => {
    for (const id of [paperId, secondPaperId]) {
      expect((await request.put(`/api/workspaces/${workspaceId}/papers/${id}`)).status()).toBe(204)
    }
    await page.addInitScript(() => {
      const original = HTMLCanvasElement.prototype.getContext as (
        this: HTMLCanvasElement,
        type: string,
        ...rest: unknown[]
      ) => RenderingContext | null
      HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, type: string, ...rest: unknown[]) {
        return type === 'webgl2' ? null : original.call(this, type, ...rest)
      } as unknown as typeof HTMLCanvasElement.prototype.getContext
    })

    await page.goto('/#/graph')
    await scopeTo(page, workspaceName)
    const tabs = page.getByRole('tablist', { name: 'Graph view' })
    await tabs.getByRole('tab', { name: '3D' }).click()
    await expect(page.getByText(WEBGL_OFF)).toBeVisible()
    await expect(page.locator('[data-view="3d"] canvas')).toHaveCount(0)

    await tabs.getByRole('tab', { name: '2D' }).click()
    await expect(page.locator('[data-view="2d"][data-ready="true"]')).toBeVisible()
  })

  test("three.js failing at start-up is caught, and the rest of the page keeps working", async ({
    page,
    request,
    paperId,
    secondPaperId,
    workspaceId,
    workspaceName,
  }) => {
    for (const id of [paperId, secondPaperId]) {
      expect((await request.put(`/api/workspaces/${workspaceId}/papers/${id}`)).status()).toBe(204)
    }
    // The app's own probe calls getContext('webgl2') with no options; three.js calls it with an options object.
    // Stubbing only the latter lets the probe pass while three.js still fails to start.
    await page.addInitScript(() => {
      const original = HTMLCanvasElement.prototype.getContext as (
        this: HTMLCanvasElement,
        type: string,
        options?: unknown,
        ...rest: unknown[]
      ) => RenderingContext | null
      HTMLCanvasElement.prototype.getContext = function (
        this: HTMLCanvasElement,
        type: string,
        options: unknown,
        ...rest: unknown[]
      ) {
        return type === 'webgl2' && options !== undefined ? null : original.call(this, type, options, ...rest)
      } as unknown as typeof HTMLCanvasElement.prototype.getContext
    })

    await page.goto('/#/graph')
    await scopeTo(page, workspaceName)
    const tabs = page.getByRole('tablist', { name: 'Graph view' })
    await tabs.getByRole('tab', { name: '3D' }).click()
    await expect(page.getByText(WEBGL_FAILED)).toBeVisible({ timeout: 20_000 })

    // The tabs still switch, and the side panel still lists papers: the boundary caught three's throw locally.
    await tabs.getByRole('tab', { name: 'Matrix' }).click()
    await expect(page.locator('[data-view="matrix"]')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Papers', exact: true })).toBeVisible()
    await expect(page.locator('.graph-paper').first()).toBeVisible()
  })

  /** Two linked papers in this test's own workspace, on the 3D view; null when this browser has no WebGL 2. */
  async function open3d(page: Page, request: APIRequestContext, ids: string[], workspaceId: string, workspaceName: string) {
    for (const id of ids) {
      expect((await request.put(`/api/workspaces/${workspaceId}/papers/${id}`)).status()).toBe(204)
    }
    const drawn = await request.post('/api/links', { data: { from_paper: ids[0], to_paper: ids[1], label: 'builds on' } })
    expect(drawn.status()).toBe(201)
    await page.goto('/#/graph')
    await scopeTo(page, workspaceName)
    await page.getByRole('checkbox', { name: /^Your links/ }).check()
    await page.getByRole('tablist', { name: 'Graph view' }).getByRole('tab', { name: '3D' }).click()
    await expect(page.locator('[data-view="3d"][data-ready="true"]')).toBeVisible({ timeout: 20_000 })
    if (await page.getByText(WEBGL_OFF).count()) return null
    const view = page.getByRole('group', { name: '3D graph' })
    await expect(view).toHaveAttribute('data-fitted', 'true', { timeout: 20_000 })
    return view
  }

  test('the 3D view moves by its buttons and keys, and Reset frames the papers again', async ({
    page,
    request,
    paperId,
    secondPaperId,
    workspaceId,
    workspaceName,
  }) => {
    // Reduced motion: no entrance and no spin, so the camera moves only when this test moves it.
    await page.emulateMedia({ reducedMotion: 'reduce' })
    const view = await open3d(page, request, [paperId, secondPaperId], workspaceId, workspaceName)
    test.skip(view === null, 'this browser has no WebGL 2')
    const read = async (name: string) => Number(await view!.getAttribute(name))
    const distance = () => read('data-camera-distance')
    const azimuth = () => read('data-camera-azimuth')
    // The first fit glides for 400 ms: wait until two reads 200 ms apart agree.
    await expect
      .poll(async () => {
        const before = await distance()
        await page.waitForTimeout(200)
        return before > 0 && before === (await distance())
      })
      .toBe(true)
    const fitted = await distance()
    await expect(view!).toHaveAttribute('data-auto-rotate', 'false')
    await expect(page.getByText('Drag to rotate · Right-drag to move · Scroll to zoom · Click a paper to select it')).toBeVisible()

    const buttons = page.getByRole('group', { name: 'Move the 3D view' })
    await buttons.getByRole('button', { name: 'Zoom in' }).click()
    await expect.poll(distance).toBeLessThan(fitted * 0.85)
    // The how-to line goes at the first move and doesn't come back.
    await expect(page.getByText(/^Drag to rotate/)).toHaveCount(0)
    await expect.poll(distance).toBeGreaterThan(fitted * 0.75) // one step, not a runaway

    // Keys work while the 3D group has focus: a left arrow turns the view a sixteenth of a turn at the same distance.
    const near = await distance()
    const startAngle = await azimuth()
    await view!.focus()
    await page.keyboard.press('ArrowLeft')
    const turned = async () => (((await azimuth()) - startAngle + 540) % 360) - 180
    await expect.poll(turned).toBeCloseTo(22.5, 0)
    expect(Math.abs((await distance()) - near)).toBeLessThan(0.5)

    // Zoom out undoes zoom in; 0 is Reset, which frames the papers at the first fit's distance again.
    await buttons.getByRole('button', { name: 'Zoom out' }).click()
    await expect.poll(async () => Math.abs((await distance()) - fitted) / fitted).toBeLessThan(0.02)
    await buttons.getByRole('button', { name: 'Zoom in' }).click()
    await buttons.getByRole('button', { name: 'Zoom in' }).click()
    await expect.poll(distance).toBeLessThan(fitted * 0.7)
    await expect.poll(distance).toBeGreaterThan(fitted * 0.6) // two steps (0.64), not a glide built on a half-way camera
    await view!.focus()
    await page.keyboard.press('0')
    await expect.poll(async () => Math.abs((await distance()) - fitted) / fitted).toBeLessThan(0.02)
  })

  test('the 3D view spins gently until the first move, then stays still', async ({
    page,
    request,
    paperId,
    secondPaperId,
    workspaceId,
    workspaceName,
  }) => {
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    const view = await open3d(page, request, [paperId, secondPaperId], workspaceId, workspaceName)
    test.skip(view === null, 'this browser has no WebGL 2')
    await expect(view!).toHaveAttribute('data-auto-rotate', 'true')
    const angle = async () => Number(await view!.getAttribute('data-camera-azimuth'))
    const first = await angle()
    await expect.poll(angle).not.toBe(first) // it really turns

    await page.getByRole('group', { name: 'Move the 3D view' }).getByRole('button', { name: 'Rotate right' }).click()
    await expect(view!).toHaveAttribute('data-auto-rotate', 'false')
    await page.waitForTimeout(600) // the button's own 400 ms glide ends
    const settled = await angle()
    await page.waitForTimeout(1000)
    expect(await angle()).toBe(settled)
  })
})
