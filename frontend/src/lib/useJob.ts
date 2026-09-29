import { useEffect, useState } from "react";
import { api } from "./api";
import type { JobRow } from "./types";

/** Poll a background job until it finishes. */
export function useJob(runId: string | null, intervalMs = 1000): { job: JobRow | null; error: string | null } {
  const [job, setJob] = useState<JobRow | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setJob(null);
    setError(null);
    if (!runId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const j = await api.job(runId);
        if (stopped) return;
        setJob(j);
        if (j.status === "queued" || j.status === "running") timer = setTimeout(poll, intervalMs);
      } catch (e) {
        if (!stopped) {
          setError(e instanceof Error ? e.message : "failed");
          timer = setTimeout(poll, intervalMs * 3);
        }
      }
    };
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [runId, intervalMs]);

  return { job, error };
}

export const isFinished = (j: JobRow | null) => !!j && ["done", "error", "cancelled"].includes(j.status);
