import LegalPage, { LegalSection, LegalList } from '@/components/LegalPage'

export default function TermsPage() {
  return (
    <LegalPage title="Terms of Service" updated="August 9, 2026">
      <LegalSection title="1. Acceptance of Terms">
        <p>
          By accessing, registering for, or using TorrentCDN (the &ldquo;Service&rdquo;), you agree to be bound by
          these Terms of Service (the &ldquo;Terms&rdquo;). If you do not agree to these Terms, you may not use the
          Service. The Service is operated by TorrentCDN (&ldquo;we,&rdquo; &ldquo;us,&rdquo; or &ldquo;our&rdquo;).
        </p>
      </LegalSection>

      <LegalSection title="2. Description of Service">
        <p>
          TorrentCDN is a peer-to-peer content delivery service. Users upload video and audio files, and the
          Service stores them, generates a WebTorrent magnet link, and provides an embeddable player that streams
          the content to viewers. Where supported by the viewer&rsquo;s browser, viewers exchange file chunks
          directly with one another over WebRTC (P2P), reducing the bandwidth served from our origin. Viewers
          without P2P support automatically fall back to direct delivery from our servers.
        </p>
        <p className="mt-3">
          The Service also provides a REST API, API keys, per-file analytics, and paid subscription plans billed
          through Paystack. Some features are only available on paid plans, as described on the pricing page.
        </p>
      </LegalSection>

      <LegalSection title="3. Eligibility">
        <p>
          You must be at least 18 years old, or the age of majority in your jurisdiction, to use the Service. By
          creating an account you represent that you meet this requirement and that the information you provide
          during registration (including your email address) is accurate and current.
        </p>
      </LegalSection>

      <LegalSection title="4. Accounts and Credentials">
        <p>You are responsible for maintaining the confidentiality of your account credentials and all activity that occurs under your account. This includes:</p>
        <LegalList
          items={[
            <>Keeping your password strong and not sharing it with anyone.</>,
            <>Treating your API keys (format: <span className="font-mono text-[color:var(--text-primary)]">tcdn_&hellip;</span>) as credentials — they are shown once at creation and can access your account programmatically.</>,
            <>Enabling two-factor authentication (TOTP) where you want additional protection for your account.</>,
            <>Notifying us promptly at <a href="mailto:support@torrentcdn.store" className="font-mono underline transition-colors hover:text-[color:var(--text-primary)]">support@torrentcdn.store</a> if you suspect unauthorized use of your account.</>,
          ]}
        />
        <p className="mt-3">
          You must verify your email address before you can log in. We may send you service-related emails (for
          example, verification, password reset, and billing notices). You are responsible for keeping your email
          address up to date.
        </p>
      </LegalSection>

      <LegalSection title="5. Your Content and License to Us">
        <p>
          You retain all ownership rights in the content you upload. By uploading content, you grant us a
          non-exclusive, worldwide, royalty-free license to store, process, generate thumbnails and magnet
          links, and deliver your content to viewers as required to provide the Service. This license ends when
          you delete the content or terminate your account, subject to a reasonable processing period and any
          legal retention obligations.
        </p>
        <p className="mt-3">
          You are solely responsible for the content you upload and for ensuring you have all necessary rights to
          upload and distribute it, including any music, footage, or other material owned by third parties. You
          agree not to upload content that you do not have the right to distribute.
        </p>
      </LegalSection>

      <LegalSection title="6. Acceptable Use">
        <p>
          You agree to comply with our <a href="/aup" className="underline transition-colors hover:text-[color:var(--text-primary)]">Acceptable Use Policy</a>, incorporated into these Terms by
          reference. In summary, you may not use the Service to store or distribute content that is illegal,
          infringes the intellectual property or other rights of others, contains malware, or violates
          applicable law. We may remove content and suspend or terminate accounts that violate these Terms.
        </p>
        <p className="mt-3">
          Uploaded files are scanned against known-malware hashes, and files up to 32&nbsp;MB may be submitted to a
          third-party malware scanner (VirusTotal). Files that are definitively identified as malicious are
          rejected and are not stored. Scanning is a best-effort safeguard and is not a warranty that any file is
          free of malware.
        </p>
      </LegalSection>

      <LegalSection title="7. Copyright and DMCA Notices">
        <p>
          We respect the intellectual property of others and expect our users to do the same. If you believe
          content hosted on the Service infringes your copyright, you may submit a takedown notice to
          <a href="mailto:dmca@torrentcdn.store" className="font-mono underline transition-colors hover:text-[color:var(--text-primary)]">dmca@torrentcdn.store</a> as described in our{' '}
          <a href="/dmca" className="underline transition-colors hover:text-[color:var(--text-primary)]">DMCA/Copyright Policy</a>. We will respond to valid notices and may remove infringing content
          and terminate repeat infringers.
        </p>
      </LegalSection>

      <LegalSection title="8. Fees, Billing, and Payment">
        <p>Paid plans are billed monthly in advance through Paystack. Current plans and prices are shown on the pricing page:</p>
        <LegalList
          items={[
            <><span className="font-bold text-[color:var(--text-primary)]">Free</span> — KES 0/month, 100GB of origin bandwidth per month.</>,
            <><span className="font-bold text-[color:var(--text-primary)]">Pro</span> — KES 4,000/month, 1TB of origin bandwidth per month.</>,
            <><span className="font-bold text-[color:var(--text-primary)]">Business</span> — KES 12,000/month, unlimited origin bandwidth.</>,
          ]}
        />
        <p className="mt-3">
          P2P bandwidth exchanged between viewers is not counted toward your origin bandwidth limit. Your plan&rsquo;s
          bandwidth allowance resets each billing cycle; unused bandwidth does not carry over.
        </p>
        <p className="mt-3">
          By subscribing you authorize Paystack to charge your payment method on a recurring monthly basis. Fees
          are exclusive of any applicable taxes, which may be added where required by law. We do not store your
          card details — payment information is processed and held by Paystack in accordance with their privacy
          policy.
        </p>
        <p className="mt-3">
          You may cancel your subscription at any time from your dashboard. Cancellation takes effect at the end
          of the then-current billing period, and you retain access to paid features until that date. Fees already
          paid are non-refundable, including for partial months. Downgrades take effect at the end of the current
          billing period.
        </p>
      </LegalSection>

      <LegalSection title="9. Payouts (Administrative)">
        <p>
          Where we operate payout programs (for example, transfers to M-PESA wallets processed through Paystack),
          eligibility, amounts, and timing are determined by us in our sole discretion. Payouts may be subject to
          verification requirements and applicable taxes. We are not liable for delays or failures caused by
          payment processors or recipient mobile-money providers.
        </p>
      </LegalSection>

      <LegalSection title="10. Referral Program">
        <p>
          Where we offer a referral program, referral bonuses (for example, bonus bandwidth) are credited when a
          referred user completes qualifying actions. We may modify or withdraw the referral program at any time.
          Referral bonuses are non-transferable and have no cash value.
        </p>
      </LegalSection>

      <LegalSection title="11. Storage and File Limits">
        <p>
          The Service imposes limits on file size and storage based on your plan, as displayed in the application.
          We may change these limits from time to time; for paid plans, reductions affecting your plan will be
          communicated to you in advance. Files that exceed limits at upload time are rejected.
        </p>
      </LegalSection>

      <LegalSection title="12. Termination">
        <p>
          You may delete your account at any time, which deletes your stored files and personal data in accordance
          with our Privacy Policy. We may suspend or terminate your account, and remove your content, if you
          violate these Terms or the Acceptable Use Policy, if required by law, or if your use of the Service
          poses a security risk to the Service or others. Termination for cause does not entitle you to a refund
          of prepaid fees.
        </p>
      </LegalSection>

      <LegalSection title="13. Disclaimer of Warranties">
        <p>
          The Service is provided &ldquo;as is&rdquo; and &ldquo;as available&rdquo; without warranties of any
          kind, whether express or implied, including implied warranties of merchantability, fitness for a
          particular purpose, and non-infringement. We do not warrant that the Service will be uninterrupted,
          error-free, or secure, or that content will not be lost or corrupted.
        </p>
      </LegalSection>

      <LegalSection title="14. Limitation of Liability">
        <p>
          To the maximum extent permitted by law, TorrentCDN shall not be liable for any indirect, incidental,
          special, consequential, or punitive damages, or for any loss of profits, data, goodwill, or other
          intangible losses, arising out of or relating to your use of the Service. Our total aggregate liability
          arising out of or relating to the Service shall not exceed the greater of KES&nbsp;10,000 or the amount
          you paid us in the three months preceding the claim.
        </p>
      </LegalSection>

      <LegalSection title="15. Indemnification">
        <p>
          You agree to indemnify and hold harmless TorrentCDN and its operators, officers, and agents from any
          claims, damages, losses, and expenses (including reasonable legal fees) arising out of your content,
          your use of the Service, or your violation of these Terms or applicable law.
        </p>
      </LegalSection>

      <LegalSection title="16. Governing Law and Dispute Resolution">
        <p>
          These Terms are governed by the laws of the Republic of Kenya, without regard to conflict-of-law
          principles. Any dispute arising out of or relating to these Terms or the Service shall be subject to the
          exclusive jurisdiction of the courts of Kenya. Where applicable law in your jurisdiction provides
          mandatory consumer protections that cannot be waived, those protections remain available to you.
        </p>
      </LegalSection>

      <LegalSection title="17. Changes to These Terms">
        <p>
          We may update these Terms from time to time. When we make material changes, we will update the
          &ldquo;Last updated&rdquo; date above and, where practical, notify you by email or within the Service.
          Continued use of the Service after changes take effect constitutes acceptance of the revised Terms. If
          you do not agree to the revised Terms, you should stop using the Service.
        </p>
      </LegalSection>

      <LegalSection title="18. Contact">
        <p>
          Questions about these Terms may be directed to us at{' '}
          <a href="mailto:legal@torrentcdn.store" className="font-mono underline transition-colors hover:text-[color:var(--text-primary)]">legal@torrentcdn.store</a>. For support, contact{' '}
          <a href="mailto:support@torrentcdn.store" className="font-mono underline transition-colors hover:text-[color:var(--text-primary)]">support@torrentcdn.store</a>.
        </p>
      </LegalSection>
    </LegalPage>
  )
}
