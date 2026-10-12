import { expect, test } from './fixtures'

// Read-only: the page reads the folder Compose was started in, and the server check only reads the library.
const TOOLS_LINE = 'The server answers with its tools, from add_paper to stop_search_run.'

test('the library header opens Connect Claude with the folder filled in, and Copy puts the config on the clipboard', async ({
  page,
}) => {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.goto('/')
  await page.getByRole('link', { name: 'Connect Claude' }).click()

  await expect(page).toHaveURL(/#\/connect-claude$/)
  await expect(page.getByRole('heading', { level: 1, name: 'Connect Claude' })).toBeVisible()
  const folder = page.getByRole('textbox', { name: 'PaperLab folder' })
  await expect(folder).toHaveValue(/^\/./) // the folder `docker compose up` ran in
  await page.getByRole('tab', { name: 'macOS' }).click()
  const desktop = page.getByRole('region', { name: 'Claude Desktop' })
  const config = desktop.locator('pre')
  await expect(config).toContainText(`"command": "${await folder.inputValue()}/scripts/paperlab-mcp"`)

  await desktop.getByRole('button', { name: 'Copy' }).click()

  await expect(desktop.getByRole('button', { name: 'Copied' })).toBeVisible()
  const copied = await page.evaluate(() => navigator.clipboard.readText())
  expect(JSON.parse(copied)).toEqual({
    mcpServers: { paperlab: { command: `${await folder.inputValue()}/scripts/paperlab-mcp` } },
  })
})

test('the Windows tab calls docker compose directly', async ({ page }) => {
  await page.goto('/#/connect-claude')
  await page.getByRole('textbox', { name: 'PaperLab folder' }).fill('C:\\Users\\you\\PaperLab')
  await page.getByRole('tab', { name: 'Windows', exact: true }).click()

  await expect(page.getByRole('region', { name: 'Claude Desktop' }).locator('pre')).toContainText('"command": "docker"')
  await expect(page.getByRole('region', { name: 'Claude Code' }).locator('pre')).toHaveText(
    'claude mcp add -s user paperlab -- docker compose -f "C:\\Users\\you\\PaperLab\\docker-compose.yml" exec -T api python -m mcp_server',
  )
})

test('Check the server starts the MCP server and lists its tools', async ({ page }) => {
  await page.goto('/#/connect-claude')
  const section = page.getByRole('region', { name: "Check PaperLab's side" })

  await section.getByRole('button', { name: 'Check the server' }).click()

  // A real `python -m mcp_server` in the api container: a second or two, more under a parallel run. A cold check
  // measured 29 s once, and the first run after a container recreate is a coin flip.
  await expect(section.getByRole('status')).toHaveText(TOOLS_LINE, { timeout: 45_000 })
})

test('a failed check shows why', async ({ page }) => {
  const detail = "The MCP server couldn't start. Check the api logs: docker compose logs api."
  await page.route('**/api/mcp/check', (route) => route.fulfill({ json: { ok: false, tools: [], detail } }))
  await page.goto('/#/connect-claude')
  const section = page.getByRole('region', { name: "Check PaperLab's side" })

  await section.getByRole('button', { name: 'Check the server' }).click()

  await expect(section.getByRole('alert')).toHaveText(detail)
  await expect(section.getByRole('status')).toHaveCount(0)
})

test('a second check clears the previous error while it runs', async ({ page }) => {
  const detail = "The MCP server couldn't start. Check the api logs: docker compose logs api."
  let calls = 0
  await page.route('**/api/mcp/check', async (route) => {
    calls += 1
    if (calls === 2) await new Promise((resolve) => setTimeout(resolve, 500))
    await route.fulfill({ json: { ok: false, tools: [], detail } })
  })
  await page.goto('/#/connect-claude')
  const section = page.getByRole('region', { name: "Check PaperLab's side" })
  const button = section.getByRole('button', { name: 'Check the server' })

  await button.click()
  await expect(section.getByRole('alert')).toHaveText(detail)

  await button.click()

  await expect(section.getByRole('button', { name: 'Checking…' })).toBeVisible()
  await expect(section.getByRole('alert')).toHaveCount(0)
})

test('Settings links to Connect Claude', async ({ page }) => {
  await page.goto('/#/settings/claude')

  await page.getByRole('region', { name: 'Connect Claude' }).getByRole('link', { name: 'Open Connect Claude' }).click()

  await expect(page).toHaveURL(/#\/connect-claude$/)
  await expect(page.getByRole('heading', { level: 1, name: 'Connect Claude' })).toBeVisible()
})
