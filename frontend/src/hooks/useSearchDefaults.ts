import { useEffect, useState } from "react";
import { api } from "../api/client";

/** Hard-coded fallback — matches the backend `storage.s3_vectors.default_top_k` default. */
const FALLBACK_TOP_K = 10;

// Module-scoped cache so multiple components only trigger one HTTP call per
// app lifetime. The backend value is static per process.
let cached: number | null = null;
let inflight: Promise<number> | null = null;

function fetchDefaultTopK(): Promise<number> {
  if (cached !== null) return Promise.resolve(cached);
  if (inflight) return inflight;
  inflight = api
    .getConfig()
    .then((d) => {
      cached = d.default_top_k ?? FALLBACK_TOP_K;
      return cached;
    })
    .catch(() => {
      cached = FALLBACK_TOP_K;
      return cached;
    })
    .finally(() => {
      inflight = null;
    }) as Promise<number>;
  return inflight;
}

/**
 * Returns the backend-configured default top-K for semantic search. While the
 * value is being fetched, returns the fallback so inputs have something to
 * render. Updates once the real value arrives.
 */
export function useDefaultTopK(): number {
  const [topK, setTopK] = useState<number>(cached ?? FALLBACK_TOP_K);

  useEffect(() => {
    if (cached !== null) return;
    let alive = true;
    fetchDefaultTopK().then((v) => {
      if (alive) setTopK(v);
    });
    return () => {
      alive = false;
    };
  }, []);

  return topK;
}
