import { Check, CircleCheck, Copy } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { useCheckMcpServer, useMcpSetup } from '@/api/queries'
import { AppShell } from '@/components/AppShell'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ErrorAlert, LoadError } from '@/features/library/ErrorAlert'
import { useDesktop } from '@/features/settings/desktop'
import { copyText } from '@/lib/clipboard'
import { claudeCodeCommand, configFileHint, desktopConfig, detectOs, type SetupOs } from './connectClaude'

const COPIED_MS = 2000
const SYSTEMS: { os: SetupOs; label: string }[] = [
  { os: 'macos', label: 'macOS' },
  { os: 'windows', label: 'Windows' },
  { os: 'wsl', label: 'Windows + WSL' },
  { os: 'linux', label: 'Linux' },
]
const code = 'rounded bg-muted px-1 py-0.5 font-mono text-[0.9em]'

/** `navigator.userAgentData` (Chromium) isn't in TypeScript's DOM types yet. */
const platform = () =>
  (navigator as Navigator & { userAgentData?: { platform: string } }).userAgentData?.platform ?? navigator.platform

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3">
      <h2 id={id} className="font-heading text-xl font-semibold">
        {title}
      </h2>
      {children}
    </section>
  )
}

/** A config or command to paste, with a Copy button that reads "Copied" for 2 s. */
function CopyBlock({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function copy() {
    copyText(text).then(
      () => {
        setError(null)
        setCopied(true)
        window.setTimeout(() => setCopied(false), COPIED_MS)
      },
      (reason: Error) => setError(reason.message),
    )
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="relative">
        <pre className="overflow-x-auto rounded-lg bg-muted p-3 pr-24 font-mono text-xs leading-relaxed">{text}</pre>
        <Button variant="outline" size="sm" className="absolute top-2 right-2" onClick={copy}>
          {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
      {error && <ErrorAlert message={error} />}
    </div>
  )
}

/** `#/connect-claude`: what Claude gets, the config or command for this system, and a check of PaperLab's side. */
export function ConnectClaudePage() {
  const setup = useMcpSetup()
  const check = useCheckMcpServer()
  const desktop = useDesktop()
  const [os, setOs] = useState<SetupOs>(() => detectOs(platform()))
  const [typed, setTyped] = useState<string | null>(null)
  const folder = typed ?? setup.data?.folder ?? ''
  const ready = folder.trim() !== ''

  return (
    <AppShell title="Connect Claude">
      {/* Wider than Settings' reading column: this page is mostly config to copy, and a JSON snippet wrapped at
          prose width is harder to read. Still capped, at the width Charts and a chart use, so content panes line up
          and nothing stretches across a wide monitor. */}
      <div className="flex max-w-5xl flex-col gap-8">
      <Section id="claude-can-heading" title="What Claude can do">
        <ul className="list-disc space-y-1 pl-5 text-sm">
          <li>Search your library for the passages closest to a question, in every paper or one workspace.</li>
          <li>
            Read a paper's details, your reading pass and decision, section outline, workspaces and notes, with who
            wrote each note.
          </li>
          <li>Find the papers in your library connected to one, up to three links away.</li>
          <li>Save a note on a passage it quotes exactly.</li>
          <li>Add papers by DOI, arXiv ID or PDF link. It only fetches free copies.</li>
          <li>Create workspaces, file papers in them, or take them out.</li>
          <li>
            Run searches across your paper sources, screen and import the results, and follow citations to find
            more papers, or set a search to run by itself overnight or weekly.
          </li>
          <li>Record full-text eligibility and get your PRISMA counts.</li>
          <li>Read your notes, in one workspace or on one paper.</li>
        </ul>
        <p className="text-sm text-muted-foreground">
          Notes Claude saves are marked AI and highlighted on the lines they quote.
        </p>
      </Section>

      <Section id="claude-receives-heading" title="What Claude receives">
        <p className="text-sm">
          Passages from your papers, paper details (with your reading pass and decision) and workspace names, and
          every note on a paper, marked as yours or AI. It also receives search results (titles and abstracts), your
          screening decisions and AI suggestions on them, workspace paper lists and PRISMA counts. Claude Desktop and Claude Code send what the tools return to
          Anthropic.
        </p>
      </Section>

      <Section id="your-system-heading" title="Your system">
        <Tabs value={os} onValueChange={(value) => setOs(value as SetupOs)}>
          <TabsList aria-label="Your system">
            {SYSTEMS.map((system) => (
              <TabsTrigger key={system.os} value={system.os}>
                {system.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </Section>

      <Section id="paperlab-folder-heading" title="PaperLab folder">
        <div className="grid gap-1.5">
          <Input
            aria-labelledby="paperlab-folder-heading"
            className="font-mono"
            spellCheck={false}
            value={folder}
            aria-describedby={ready ? undefined : 'paperlab-folder-help'}
            onChange={(event) => setTyped(event.target.value)}
          />
          {!ready && (
            <p id="paperlab-folder-help" className="text-xs text-muted-foreground">
              Paste the full path of your PaperLab folder.{os === 'windows' && ' For example C:\\Users\\you\\PaperLab.'}
              {os === 'wsl' && ' For example /home/you/PaperLab, the path inside WSL.'}
            </p>
          )}
        </div>
        {setup.isError && <LoadError message={setup.error.message} onRetry={() => void setup.refetch()} />}
      </Section>

      <Section id="claude-desktop-heading" title="Claude Desktop">
        <p className="text-sm">Edit Claude Desktop's config file:</p>
        <ol className="list-decimal space-y-1 pl-5 text-sm">
          <li>
            {os === 'linux' ? (
              configFileHint(os)
            ) : (
              <>
                Open Settings → Developer → Edit Config. The file is <code className={code}>{configFileHint(os)}</code>.
              </>
            )}
          </li>
          <li>
            Add the <code className={code}>paperlab</code> entry inside <code className={code}>mcpServers</code>, keeping
            any servers already there. If the file already has servers, copy just the "paperlab" block.
          </li>
          <li>Quit Claude Desktop completely and reopen it.</li>
        </ol>
        {ready ? (
          <CopyBlock text={desktopConfig(folder, os)} />
        ) : (
          <p className="text-sm text-muted-foreground">The config appears here once the PaperLab folder is filled in.</p>
        )}
        {os === 'linux' && (
          <p className="text-sm text-muted-foreground">Claude Desktop on Linux is a beta for Ubuntu 22.04+ and Debian 12+.</p>
        )}
      </Section>

      <Section id="claude-code-heading" title="Claude Code">
        <p className="text-sm">Run this in a terminal{os === 'wsl' ? ' inside WSL' : ''}:</p>
        {ready ? (
          <CopyBlock text={claudeCodeCommand(folder, os)} />
        ) : (
          <p className="text-sm text-muted-foreground">The command appears here once the PaperLab folder is filled in.</p>
        )}
        <p className="text-sm text-muted-foreground">This registers PaperLab for every project you use Claude Code in.</p>
      </Section>

      <Section id="check-server-heading" title="Check PaperLab's side">
        <p className="text-sm text-muted-foreground">
          This starts PaperLab's MCP server and reads your library through it. It checks PaperLab, not the launcher or
          Claude's config.
        </p>
        <Button
          className="self-start"
          disabled={check.isPending}
          onClick={() => {
            check.reset()
            check.mutate()
          }}
        >
          {check.isPending ? 'Checking…' : 'Check the server'}
        </Button>
        {check.data?.ok && (
          <p role="status" className="flex items-center gap-1.5 text-sm">
            <CircleCheck aria-hidden className="size-4 shrink-0 text-primary" />
            The server answers with all 31 of its tools.
          </p>
        )}
        {check.data?.detail && <ErrorAlert message={check.data.detail} />}
        {check.error && <ErrorAlert message={check.error.message} />}
      </Section>

      <Section id="not-working-heading" title="If it doesn't work">
        <ul className="list-disc space-y-1 pl-5 text-sm">
          <li>
            Claude Desktop writes what the launcher says to its own log: on macOS{' '}
            <code className={code}>~/Library/Logs/Claude/mcp-server-paperlab.log</code>, on Windows{' '}
            <code className={code}>%APPDATA%\Claude\logs\mcp-server-paperlab.log</code>, and on Linux in Claude
            Desktop's logs folder. Open it to see why the server didn't start.
          </li>
          <li>
            {desktop !== null ? (
              'Keep the PaperLab app open (or turn on Keep running in Settings).'
            ) : (
              <>
                PaperLab must be running: <code className={code}>docker compose up -d</code> in the PaperLab folder.
              </>
            )}
          </li>
          <li>After restarting PaperLab's api, restart Claude Desktop too: the connection ends with it.</li>
          <li>The first search takes about 30 seconds while the embedding model loads.</li>
          <li>
            Linux: if Docker refuses access, add your user to the docker group with{' '}
            <code className={code}>sudo usermod -aG docker $USER</code>, then log out and back in.
          </li>
          <li>Windows + WSL: use the folder's path inside WSL, like /home/you/PaperLab.</li>
          <li>
            macOS and Linux: if docker is installed somewhere unusual, add{' '}
            <code className={code}>{'"env": { "PAPERLAB_DOCKER": "/full/path/to/docker" }'}</code> beside{' '}
            <code className={code}>command</code>. Inside WSL, export it in ~/.profile: a value set in Claude
            Desktop's config doesn't reach the distro.
          </li>
          <li>Connect Claude has been tested on macOS.</li>
        </ul>
      </Section>
      </div>
    </AppShell>
  )
}
