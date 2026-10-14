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
export function ExportCitationsMenu({ workspaceId }: { workspaceId: string }) {
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
            <DropdownMenuItem key={format} asChild>
              <a href={citationsUrl(workspaceId, format)} download>
                {label}
              </a>
            </DropdownMenuItem>
          ))}
          <DropdownMenuItem onSelect={() => setApaOpen(true)}>
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
    try {
      await copyText(await api.citationsText(workspaceId, "apa"));
      setCopied(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not copy.");
    }
  }

  const failure = error ?? (preview.error ? preview.error.message : null);
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
