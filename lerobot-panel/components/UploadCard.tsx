"use client";

import { useState } from "react";
import { api, type CollectionState, type UploadState } from "@/lib/api";
import { fmtAgo } from "@/lib/format";
import { useLocalStorage } from "@/lib/hooks";
import { useTopic } from "@/lib/live";
import { Button, Card, ErrorBox, Modal, Spinner, StatusIcon } from "./ui";

type Method = "hub" | "rsync";

/** Send the shared dataset off this machine: a private Hugging Face repo, or rsync to the GPU server. */
export function UploadCard({ collection, recording }: { collection: CollectionState | null; recording: boolean }) {
  const upload = useTopic("upload").data;
  const [operator] = useLocalStorage("panel.operator", "");
  const [asking, setAsking] = useState<Method | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const cfg = collection?.config;
  const total = collection?.total_episodes ?? 0;
  const targets: { method: Method; label: string; dest: string }[] = [];
  if (cfg?.upload.hub) targets.push({ method: "hub", label: "Upload to Hugging Face (private)", dest: cfg.upload.hub_repo_id });
  if (cfg?.upload.rsync) targets.push({ method: "rsync", label: "Copy to GPU server (rsync)", dest: cfg.upload.rsync_dest });

  const blocked = recording ? "Finish the recording session first" : total === 0 ? "Nothing recorded yet" : collection?.problems.length ? "The dataset is damaged" : upload?.running ? "An upload is running" : null;
  const last = upload?.last_success ?? null;
  const since = last?.episodes != null ? Math.max(0, total - last.episodes) : null;

  const go = async (dryRun: boolean) => {
    if (!asking) return;
    setError(null);
    setStarting(true);
    try {
      await api.uploadStart(asking, dryRun, operator);
      setAsking(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  };

  const ask = targets.find((t) => t.method === asking);
  return (
    <Card title="Upload dataset">
      <div className="grid gap-3 text-sm">
        {targets.length === 0 ? (
          <p className="text-ink-2">
            Upload is not set up yet. Add a Hugging Face repo (<code className="font-mono text-xs">upload.hub_repo_id</code>) or an rsync destination (
            <code className="font-mono text-xs">upload.rsync_dest</code>) to <code className="font-mono text-xs">collection.yaml</code> and restart the panel.
          </p>
        ) : (
          <>
            <p className="text-ink-2">
              {total} episode{total === 1 ? "" : "s"} in <span className="font-mono text-xs">{cfg?.dataset}</span>
              {last ? (
                <>
                  . Last upload {fmtAgo(last.at)}
                  {last.by && ` by ${last.by}`}
                  {since != null && since > 0 ? `, ${since} new episode${since === 1 ? "" : "s"} since.` : since === 0 ? ", nothing new since." : "."}
                </>
              ) : (
                ". Never uploaded."
              )}
            </p>
            <div className="flex flex-wrap gap-2">
              {targets.map((t) => (
                <Button key={t.method} variant={t.method === targets[0].method ? "primary" : "secondary"} disabled={!!blocked} title={blocked ?? t.dest} onClick={() => setAsking(t.method)}>
                  {t.label}
                </Button>
              ))}
            </div>
            {blocked && <p className="text-xs text-muted">{blocked}.</p>}
          </>
        )}
        <UploadProgress upload={upload} />
      </div>

      <Modal open={!!asking} onClose={() => setAsking(null)} title="Upload the dataset?">
        <div className="grid gap-3 text-sm text-ink-2">
          <p>
            Send <b className="text-ink">{total} episodes</b> of <span className="font-mono text-xs">{cfg?.dataset}</span> to{" "}
            <b className="break-all font-mono text-xs text-ink">{ask?.dest}</b>
            {asking === "hub" ? " as a private dataset." : "."}
          </p>
          {asking === "hub" && <p className="text-xs">It is created private; an existing public repo is refused. You must be logged in to Hugging Face on this machine.</p>}
          <p className="text-xs">A test run checks the connection and what would be sent, without sending anything.</p>
          {error && <ErrorBox>{error}</ErrorBox>}
        </div>
        <div className="mt-5 flex flex-wrap justify-end gap-2">
          <Button onClick={() => setAsking(null)}>Cancel</Button>
          <Button onClick={() => go(true)} disabled={starting}>
            Test run
          </Button>
          <Button variant="primary" onClick={() => go(false)} loading={starting} loadingText="Starting…">
            Upload
          </Button>
        </div>
      </Modal>
    </Card>
  );
}

function UploadProgress({ upload }: { upload: UploadState | null }) {
  if (!upload || upload.status === "idle") return null;
  const label = `${upload.dry_run ? "Test run" : "Upload"} by ${upload.method}`;
  return (
    <div className="grid gap-2 border-t border-line pt-3">
      <div className="flex items-center gap-2 text-sm font-medium">
        {upload.status === "running" ? <Spinner /> : <StatusIcon status={upload.status === "done" ? "ok" : "fail"} />}
        {label} {upload.status === "running" ? "is running…" : upload.status === "done" ? "finished" : "failed"}
      </div>
      <pre className="max-h-48 overflow-auto rounded-lg bg-surface-2 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap">{upload.output.join("\n") || "…"}</pre>
    </div>
  );
}
