import LegalPage, { LegalSection, LegalList } from '@/components/LegalPage'

export default function PrivacyPage() {
  return (
    <LegalPage title="Privacy Policy" updated="August 9, 2026">
      <LegalSection title="1. Introduction">
        <p>
          This Privacy Policy explains how TorrentCDN (&ldquo;we,&rdquo; &ldquo;us,&rdquo; or
          &ldquo;our&rdquo;) collects, uses, and protects personal data when you use the TorrentCDN service
          (the &ldquo;Service&rdquo;). We are committed to protecting your privacy and processing personal data
          in accordance with the Kenya Data Protection Act, 2019, and, where applicable, the EU General Data
          Protection Regulation (GDPR) for users in the European Economic Area.
        </p>
      </LegalSection>

      <LegalSection title="2. Information We Collect">
        <p className="font-semibold text-white">Account information</p>
        <p>
          When you register, we collect your email address and a hashed password. If you choose to enable
          two-factor authentication, we store a TOTP secret and hashes of your one-time backup codes. We also
          store your plan, verification status, referral code, and bandwidth usage so we can provide the Service.
        </p>
        <p className="mt-3 font-semibold text-white">Content you upload</p>
        <p>
          We store the files you upload (or their storage references), filenames, sizes, and the magnet links we
          generate for them. Uploaded files are scanned for known malware; files up to 32&nbsp;MB may be submitted
          to VirusTotal for scanning.
        </p>
        <p className="mt-3 font-semibold text-white">Usage and analytics data</p>
        <p>
          We collect data about how your content is delivered: view counts, bytes transferred from our origin,
          bytes exchanged between peers (P2P), and delivery performance. This powers the analytics shown in your
          dashboard and is used to operate and improve the Service.
        </p>
        <p className="mt-3 font-semibold text-white">Technical data</p>
        <p>
          We collect IP addresses, browser type, and device information for security, rate limiting, and abuse
          prevention. We do not use this data to identify or profile you for advertising.
        </p>
        <p className="mt-3 font-semibold text-white">Payment data</p>
        <p>
          When you subscribe to a paid plan, payment is processed by Paystack. We do not receive or store your
          card details; Paystack handles them in accordance with its own privacy policy.
        </p>
      </LegalSection>

      <LegalSection title="3. How We Use Your Information">
        <LegalList
          items={[
            <>To create and manage your account, authenticate you, and secure the Service.</>,
            <>To store and deliver the content you upload, and to generate embed codes and magnet links.</>,
            <>To measure your bandwidth usage and display analytics in your dashboard.</>,
            <>To bill you for paid plans and manage subscriptions through Paystack.</>,
            <>To send service-related emails: verification, password reset, billing, and policy notices.</>,
            <>To scan uploads for malware, prevent abuse, enforce our Acceptable Use Policy, and respond to legal requests.</>,
            <>To operate, maintain, and improve the Service.</>,
          ]}
        />
      </LegalSection>

      <LegalSection title="4. Legal Bases for Processing">
        <p>We process personal data on the following bases:</p>
        <LegalList
          items={[
            <><span className="font-bold text-white">Performance of a contract</span> — providing the Service you requested, including storage, delivery, and billing.</>,
            <><span className="font-bold text-white">Legitimate interests</span> — security, abuse prevention, and service improvement, balanced against your rights.</>,
            <><span className="font-bold text-white">Legal obligation</span> — compliance with applicable law, such as lawful requests from authorities.</>,
            <><span className="font-bold text-white">Consent</span> — where we ask for it (for example, optional marketing), which you may withdraw at any time.</>,
          ]}
        />
      </LegalSection>

      <LegalSection title="5. Sharing With Third Parties">
        <p>We share personal data only with the processors needed to operate the Service, and never sell your data. Current processors include:</p>
        <LegalList
          items={[
            <><span className="font-bold text-white">Cloudflare R2</span> — object storage for uploaded files.</>,
            <><span className="font-bold text-white">Supabase</span> — PostgreSQL database for account and usage records.</>,
            <><span className="font-bold text-white">Paystack</span> — payment processing for subscriptions (and transfers where payouts apply).</>,
            <><span className="font-bold text-white">Resend</span> — transactional email delivery.</>,
            <><span className="font-bold text-white">VirusTotal</span> — malware scanning of uploads (hash lookups and, for files up to 32&nbsp;MB, file submission).</>,
            <><span className="font-bold text-white">Render and Vercel</span> — hosting of the backend API, Redis, and frontend.</>,
          ]}
        />
        <p className="mt-3">
          We may also disclose personal data where required by law, regulation, or a valid legal request, or to
          protect the rights, property, or safety of TorrentCDN, our users, or the public.
        </p>
      </LegalSection>

      <LegalSection title="6. Peer-to-Peer Disclosure">
        <p>
          TorrentCDN uses WebTorrent, a peer-to-peer technology. When a viewer plays embedded content, their IP
          address may be visible to other viewers in the WebTorrent swarm, and their browser may upload chunks of
          the file to other peers. This is inherent to P2P delivery and is not controlled by us. If you embed
          TorrentCDN content on your own site, you should disclose this to your viewers, as their IP addresses and
          participation in the swarm may be visible to other peers.
        </p>
      </LegalSection>

      <LegalSection title="7. Cookies and Local Storage">
        <p>
          We do not use tracking cookies or third-party advertising cookies. We use your browser&rsquo;s session
          storage to keep you logged in and to carry a referral code between pages. Session storage is cleared
          when you close your browser tab. See our <a href="/cookies" className="underline transition-colors hover:text-white">Cookie Policy</a> for details.
        </p>
      </LegalSection>

      <LegalSection title="8. Data Retention">
        <p>
          We retain your account and personal data for as long as your account is active, and for a reasonable
          period afterwards where needed to comply with legal or accounting obligations (for example, billing
          records). Files you delete are removed from storage; deleted objects are also subject to our
          orphaned-object cleanup that removes uploaded-but-unfinalized files on a schedule.
        </p>
      </LegalSection>

      <LegalSection title="9. Data Security">
        <p>We take reasonable measures to protect your data, including:</p>
        <LegalList
          items={[
            <>Passwords stored only as salted hashes (never in plaintext).</>,
            <>API keys stored only as hashes, with a one-active-key-per-user policy and instant revocation.</>,
            <>TLS encryption for data in transit, and encryption at rest on our storage providers.</>,
            <>Two-factor authentication (TOTP) support for your account.</>,
            <>Email verification required before login, and session revocation on password or security changes.</>,
            <>Security audits, secret scanning, and malware scanning of uploads.</>,
          ]}
        />
        <p className="mt-3">
          No method of transmission or storage is completely secure. While we work hard to protect your data, we
          cannot guarantee absolute security.
        </p>
      </LegalSection>

      <LegalSection title="10. International Transfers">
        <p>
          Your data may be processed in countries other than your own, including where our hosting providers
          (Cloudflare, Supabase, Render, Vercel) operate. Where we transfer personal data internationally, we rely
          on appropriate safeguards to protect it, consistent with applicable law.
        </p>
      </LegalSection>

      <LegalSection title="11. Your Rights">
        <p>
          Depending on your jurisdiction, you may have rights to request access to, correction of, or deletion of
          your personal data; to object to or restrict certain processing; and to data portability. Under the
          Kenya Data Protection Act, 2019, you may also lodge a complaint with the Office of the Data Protection
          Commissioner. You may delete your account and its data at any time from your dashboard. To exercise any
          of these rights, contact us at <a href="mailto:privacy@torrentcdn.store" className="font-mono underline transition-colors hover:text-white">privacy@torrentcdn.store</a>.
        </p>
      </LegalSection>

      <LegalSection title="12. Children&rsquo;s Privacy">
        <p>
          The Service is not directed to children under 16, and we do not knowingly collect personal data from
          children. If you believe a child has provided us personal data, contact us at{' '}
          <a href="mailto:privacy@torrentcdn.store" className="font-mono underline transition-colors hover:text-white">privacy@torrentcdn.store</a> and we will delete it.
        </p>
      </LegalSection>

      <LegalSection title="13. Changes to This Policy">
        <p>
          We may update this Privacy Policy from time to time. Material changes will be reflected by updating the
          &ldquo;Last updated&rdquo; date above and, where practical, notifying you by email or within the
          Service.
        </p>
      </LegalSection>

      <LegalSection title="14. Contact">
        <p>
          For privacy-related questions or requests, contact us at{' '}
          <a href="mailto:privacy@torrentcdn.store" className="font-mono underline transition-colors hover:text-white">privacy@torrentcdn.store</a>. Our services are operated from
          Kenya.
        </p>
      </LegalSection>
    </LegalPage>
  )
}
