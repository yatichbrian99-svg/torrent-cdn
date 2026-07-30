export default function PrivacyPage() {
  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-bold tracking-[-0.02em] text-white">Privacy Policy</h1>
      <p className="mt-2 text-sm" style={{ color: '#555' }}>Last updated: July 2026</p>

      <div className="mt-10 space-y-8 text-sm leading-relaxed" style={{ color: '#999' }}>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">1. Information We Collect</h2>
          <p>We collect your email address when you register. We collect analytics data including peer connection events, bytes transferred, and file access logs to provide our service.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">2. How We Use Your Information</h2>
          <p>We use your email to authenticate your account and send service-related communications. We use analytics data to calculate your bandwidth usage and display statistics in your dashboard.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">3. Data Storage</h2>
          <p>Your files are stored on Cloudflare R2 infrastructure. Your account data is stored on Supabase (PostgreSQL). We do not sell your data to third parties.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">4. P2P Technology</h2>
          <p>When viewers watch your embedded content, their IP address may be visible to other peers in the WebTorrent swarm. This is inherent to peer-to-peer technology. Viewers should be made aware of this in your own privacy policy.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">5. Cookies</h2>
          <p>We use session storage to maintain your login state. We do not use tracking cookies or third-party advertising cookies.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">6. Data Deletion</h2>
          <p>You may delete your account and all associated data at any time from your dashboard settings. Deleted data is permanently removed within 30 days.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">7. Contact</h2>
          <p>For privacy-related requests, contact us at privacy@torrentcdn.com</p>
        </section>
      </div>
    </div>
  )
}
