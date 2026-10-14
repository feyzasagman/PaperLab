import { Upload } from 'lucide-react'
import { useRef } from 'react'
import { useImportNotes } from '@/api/queries'
import { Button } from '@/components/ui/button'
import { ErrorAlert } from '@/features/library/ErrorAlert'
import { importSummary } from './notesPage'

/** "Import Markdown" on the Notes page: pick .md files, each becomes a note linked to papers by their DOIs. */
export function ImportMarkdownButton() {
  const input = useRef<HTMLInputElement>(null)
  const { mutate, data, error, isPending } = useImportNotes()

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-3">
        <Button variant="outline" size="sm" disabled={isPending} onClick={() => input.current?.click()}>
          <Upload aria-hidden /> Import Markdown
        </Button>
        {isPending && <span className="text-sm text-muted-foreground">Importing…</span>}
        {data && <p role="status" className="text-sm text-muted-foreground">{importSummary(data)}</p>}
      </div>
      {error && <ErrorAlert message={error.message} />}
      <input
        ref={input}
        type="file"
        multiple
        accept=".md,text/markdown"
        aria-label="Markdown files"
        className="hidden"
        onChange={(event) => {
          const files = Array.from(event.target.files ?? [])
          event.target.value = '' // the same files can be picked again
          if (files.length > 0) mutate(files)
        }}
      />
    </div>
  )
}
