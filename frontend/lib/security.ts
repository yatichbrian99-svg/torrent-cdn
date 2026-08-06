/**
 * Zero-trust input guards for the client. Every value that can be influenced
 * by the URL bar, an embed attribute, or a server response is validated here
 * before it becomes a request path, a redirect target, or stored state.
 *
 * These are defense-in-depth — the backend is always the real gatekeeper. But
 * validating at the boundary keeps crafted values out of request URLs (path
 * confusion), out of window.location (scheme abuse), and out of storage.
 */

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** True when `id` is a well-formed UUID — the format every backend file/upload id uses. */
export function isUuid(id: string): boolean {
  return UUID_RE.test(id || '')
}

/**
 * True when `url` is an absolute https:// URL. Used before assigning anything
 * from a server response (e.g. the Paystack checkout URL) to window.location —
 * rejects javascript:, data:, http:, and relative values outright.
 */
export function isSafeHttpsUrl(url: string): boolean {
  try {
    return new URL(url).protocol === 'https:'
  } catch {
    return false
  }
}

/**
 * Strip a ?ref= referral code down to the safe alphabet: alphanumeric only,
 * capped at the backend's 20-char column. The backend uppercases and matches
 * on UPPER(referral_code), so case is preserved-but-irrelevant here.
 */
export function sanitizeRefCode(ref: string): string {
  return (ref || '').replace(/[^a-zA-Z0-9]/g, '').slice(0, 20)
}
