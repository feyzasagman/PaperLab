import type { APIRequestContext, Locator, Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { expect } from './fixtures'

const WEBGL_OFF = '3D needs WebGL, which this browser has turned off. The other views work without it.'

/** A browser whose camera plays `video` from fixtures/hands, with the camera prompt already allowed. */
export const fakeCamera = (video: string) => ({
  launchOptions: {
    args: [
      '--use-fake-ui-for-media-stream',
      '--use-fake-device-for-media-stream',
      `--use-file-for-fake-video-capture=${fileURLToPath(new URL(`./fixtures/hands/${video}`, import.meta.url))}`,
    ],
  },
})

/**
 * These papers in this test's own workspace (linked in order when `link`), on the 3D view, the first fit done; null
 * when this browser has no WebGL 2.
 */
export async function open3d(
  page: Page,
  request: APIRequestContext,
  papers: string[],
  link: boolean,
  workspaceId: string,
  workspaceName: string,
): Promise<Locator | null> {
  for (const id of papers) {
    expect((await request.put(`/api/workspaces/${workspaceId}/papers/${id}`)).status()).toBe(204)
  }
  if (link) {
    const drawn = await request.post('/api/links', { data: { from_paper: papers[0], to_paper: papers[1], label: 'builds on' } })
    expect(drawn.status()).toBe(201)
  }
  await page.goto('/#/graph')
  await page.getByRole('combobox', { name: 'Workspace' }).click()
  await page.getByRole('option', { name: workspaceName, exact: true }).click()
  await page.getByRole('checkbox', { name: /^Your links/ }).check()
  await page.getByRole('tablist', { name: 'Graph view' }).getByRole('tab', { name: '3D' }).click()
  await expect(page.locator('[data-view="3d"][data-ready="true"]')).toBeVisible({ timeout: 20_000 })
  if (await page.getByText(WEBGL_OFF).count()) return null
  const view = page.getByRole('group', { name: '3D graph' })
  await expect(view).toHaveAttribute('data-fitted', 'true', { timeout: 20_000 })
  await page.waitForTimeout(600) // the first fit's 400 ms glide ends
  return view
}

export const handsToggle = (page: Page) =>
  page.getByRole('group', { name: 'Move the 3D view' }).getByRole('button', { name: 'Hands mode' })
