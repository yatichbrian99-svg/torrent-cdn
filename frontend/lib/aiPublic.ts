/**
 * Shared constants for the public TCDN-AI page.
 *
 * Kept out of the client components so a server component can import them
 * without a client reference, and in one place so the base URL, the model id
 * and the contact address cannot drift between the page and its buttons.
 *
 * Values are the real ones:
 *  - the app proxies the OpenAI surface at `/v1` on its own origin, which is the
 *    base `lib/aiConsole.ts` builds for /ai/quickstart;
 *  - `llama-3.1-8b-instruct` is the model the API docs and this alpha serve;
 *  - `support@torrentcdn.store` is the address the terms page already gives for
 *    support — no new mailbox was invented for this page.
 */
export const AI_PUBLIC_BASE_URL = 'https://www.torrentcdn.store/v1'
export const AI_PUBLIC_MODEL = 'llama-3.1-8b-instruct'
export const AI_PUBLIC_CONTACT_EMAIL = 'support@torrentcdn.store'
export const AI_PUBLIC_MAIL_SUBJECT = 'TCDN-AI early access request'
export const AI_EARLY_ACCESS_MAILTO = `mailto:${AI_PUBLIC_CONTACT_EMAIL}?subject=${encodeURIComponent(AI_PUBLIC_MAIL_SUBJECT)}`
