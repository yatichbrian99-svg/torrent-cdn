import LegalPage, { LegalSection, LegalList } from '@/components/LegalPage'

export default function CookiesPage() {
  return (
    <LegalPage title="Cookie Policy" updated="August 9, 2026">
      <LegalSection title="1. The Short Version">
        <p>
          TorrentCDN does not use tracking cookies, advertising cookies, or third-party analytics cookies. We use
          your browser&rsquo;s <span className="font-bold text-[color:var(--text-primary)]">session storage</span> (not cookies) for the
          minimum needed to keep you signed in and to carry referral codes between pages. Nothing we store is used
          to track you across the web.
        </p>
      </LegalSection>

      <LegalSection title="2. What We Store and Why">
        <p>We use session storage for the following purposes:</p>
        <LegalList
          items={[
            <><span className="font-mono text-[color:var(--text-primary)]">torrent_cdn_token</span> — keeps you signed in. Set on login, cleared on logout or when you close the tab.</>,
            <><span className="font-mono text-[color:var(--text-primary)]">torrent_cdn_ref</span> — carries a referral code from a link you clicked to your registration, so the person who referred you gets credit. Cleared after registration.</>,
          ]}
        />
        <p className="mt-3">
          Session storage is scoped to a single browser tab and is automatically cleared when the tab or browser
          closes. We do not use persistent localStorage for authentication or tracking.
        </p>
      </LegalSection>

      <LegalSection title="3. What We Do Not Use">
        <LegalList
          items={[
            <>No advertising or targeting cookies.</>,
            <>No cross-site tracking pixels or beacons.</>,
            <>No third-party analytics cookies (our delivery analytics are first-party and pseudonymous).</>,
            <>No cookies that persist after you close your browser.</>,
          ]}
        />
      </LegalSection>

      <LegalSection title="4. Managing Session Storage">
        <p>
          Because we use session storage rather than cookies, there are no cookie banners to accept or reject.
          You can clear session storage at any time by closing the browser tab, or via your browser&rsquo;s
          site-data settings (this will sign you out). Blocking all site data for torrentcdn.store may prevent
          sign-in from working.
        </p>
      </LegalSection>

      <LegalSection title="5. Changes and Contact">
        <p>
          We will update this policy if our use of storage changes. Questions may be directed to{' '}
          <a href="mailto:privacy@torrentcdn.store" className="font-mono underline transition-colors hover:text-[color:var(--text-primary)]">privacy@torrentcdn.store</a>.
        </p>
      </LegalSection>
    </LegalPage>
  )
}
