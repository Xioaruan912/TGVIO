/**
 * A successful byte-range probe proves reachability only; it cannot confirm
 * that the browser can decode the media or that the original request succeeded.
 * Retry a failed media element twice for transient or reachable responses before
 * counting the clip as unplayable.
 */
export function shouldRetryMediaError(probeStatus: number, attempts: number): boolean {
  if (attempts >= 2) return false;
  return probeStatus === 0 || probeStatus === 206 || probeStatus === 429 || probeStatus >= 500;
}
