import { useQuery } from "@tanstack/react-query";
import { Quote } from "lucide-react";
import { useState } from "react";
import { api, citationsUrl } from "@/api/client";
import { glass } from "@/components/glass";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";

const DOWNLOADS = [
  ["BibTeX", "bibtex"],
  ["RIS", "ris"],
  ["CSL-JSON", "csljson"],
] as const;

/** The workspace header's "Export citations" menu: three downloads, and an APA 7 preview to read and copy. */
const NO_PAPERS = "Add papers to this workspace first.";

/** The API's snake_case refusal codes, in words for the reader. */
function plainError(error: Error): string {
  return error.message === "no_papers"
    ? "This workspace has no papers to cite yet."
    : "Couldn't build the reference list. Try again.";
}

export function ExportCitationsMenu({
  workspaceId,
  paperCount,
}: {
  workspaceId: string;
  paperCount: number;
}) {
  const empty = paperCount === 0;
  const [apaOpen, setApaOpen] = useState(false);
  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="sm">
            <Quote aria-hidden /> Export citations
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          className={cn(glass, "bg-glass-strong ring-glass-border")}
        >
          {DOWNLOADS.map(([label, format]) => (
            <DropdownMenuItem
              key={format}
              asChild={!empty}
              disabled={empty}
              title={empty ? NO_PAPERS : undefined}
            >
              {empty ? (
                label
              ) : (
                <a href={citationsUrl(workspaceId, format)} download>
                  {label}
                </a>
              )}
            </DropdownMenuItem>
          ))}
          <DropdownMenuItem
            disabled={empty}
            title={empty ? NO_PAPERS : undefined}
            onSelect={() => setApaOpen(true)}
          >
            APA 7…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {apaOpen && (
        <ApaDialog
          workspaceId={workspaceId}
          onClose={() => setApaOpen(false)}
        />
      )}
    </>
  );
}

function ApaDialog({
  workspaceId,
  onClose,
}: {
  workspaceId: string;
  onClose: () => void;
}) {
  const preview = useQuery({
    queryKey: ["citations", workspaceId, "apa-html"],
    queryFn: () => api.citationsText(workspaceId, "apa-html"),
    retry: false,
    gcTime: 0,
  });
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function copy() {
    setError(null);
    let plain: string;
    try {
      plain = await api.citationsText(workspaceId, "apa");
    } catch (e) {
      return setError(plainError(e as Error));
    }
    try {
      await copyText(plain);
      setCopied(true);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const failure = error ?? (preview.error ? plainError(preview.error) : null);
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>APA 7 references</DialogTitle>
          <DialogDescription>
            Your workspace's papers, in reference-list order.
          </DialogDescription>
        </DialogHeader>
        {failure && (
          <Alert variant="destructive">
            <AlertDescription>{failure}</AlertDescription>
          </Alert>
        )}
        {/* The backend escapes every field and adds only its own <i> markup, so this HTML is safe to render. */}
        {preview.isPending && (
          <p role="status" className="text-sm text-muted-foreground">
            Loading…
          </p>
        )}
        {preview.data !== undefined && (
          <div
            className="max-h-[50vh] overflow-auto text-sm leading-relaxed whitespace-pre-wrap"
            dangerouslySetInnerHTML={{ __html: preview.data }}
          />
        )}
        <Button
          className="self-start"
          onClick={copy}
          disabled={preview.data === undefined}
        >
          {copied ? "Copied" : "Copy"}
        </Button>
      </DialogContent>
    </Dialog>
  );
}
