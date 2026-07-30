export default function TermsPage() {
  return (
    <div className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-2xl font-bold tracking-[-0.02em] text-white">Terms of Service</h1>
      <p className="mt-2 text-sm" style={{ color: '#555' }}>Last updated: July 2026</p>

      <div className="mt-10 space-y-8 text-sm leading-relaxed" style={{ color: '#999' }}>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">1. Acceptance of Terms</h2>
          <p>By accessing or using TorrentCDN, you agree to be bound by these Terms of Service. If you do not agree, do not use the service.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">2. Description of Service</h2>
          <p>TorrentCDN provides peer-to-peer content delivery network services using WebTorrent technology. Users can upload video and audio files and embed them on third-party websites.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">3. Acceptable Use</h2>
          <p>You agree not to upload content that is illegal, infringes copyright, contains malware, or violates the rights of others. TorrentCDN reserves the right to remove content and terminate accounts that violate these terms.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">4. Account Responsibilities</h2>
          <p>You are responsible for maintaining the security of your account credentials. You are responsible for all activity that occurs under your account.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">5. Bandwidth and Storage</h2>
          <p>Free accounts are limited to 100GB of origin bandwidth per month. Exceeding this limit will require an upgrade. P2P bandwidth is not counted toward your limit.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">6. Payment</h2>
          <p>Paid plans are billed monthly. Refunds are not provided for partial months. You may cancel at any time and retain access until the end of your billing period.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">7. Limitation of Liability</h2>
          <p>TorrentCDN is provided "as is" without warranties of any kind. We are not liable for any damages arising from use of the service, including data loss or service interruption.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">8. Changes to Terms</h2>
          <p>We reserve the right to modify these terms at any time. Continued use of the service after changes constitutes acceptance of the new terms.</p>
        </section>
        <section>
          <h2 className="mb-3 text-base font-semibold text-white">9. Contact</h2>
          <p>For questions about these terms, contact us at support@torrentcdn.com</p>
        </section>
      </div>
    </div>
  )
}
