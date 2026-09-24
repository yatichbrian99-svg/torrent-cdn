import LegalPage, { LegalSection, LegalList } from '@/components/LegalPage'

export default function AupPage() {
  return (
    <LegalPage title="Acceptable Use Policy" updated="August 9, 2026">
      <LegalSection title="1. Purpose">
        <p>
          This Acceptable Use Policy (&ldquo;AUP&rdquo;) sets out what you may and may not do with the TorrentCDN
          service (the &ldquo;Service&rdquo;). It is incorporated into our Terms of Service. By using the Service,
          you agree to this policy. We may update it from time to time.
        </p>
      </LegalSection>

      <LegalSection title="2. Prohibited Content">
        <p>You may not use the Service to store, distribute, or link to content that:</p>
        <LegalList
          items={[
            <>Infringes the copyright, trademark, or other intellectual property rights of others.</>,
            <>Is illegal under applicable law, including content that is obscene, defamatory, or fraudulent.</>,
            <>Depicts or exploits children in a sexual manner, or content the possession or distribution of which is illegal in your jurisdiction.</>,
            <>Contains malware, viruses, worms, ransomware, or code designed to harm or gain unauthorized access to systems.</>,
            <>Harasses, threatens, or incites violence against individuals or groups.</>,
            <>Exposes another person&rsquo;s private information (doxxing) without consent.</>,
            <>Promotes illegal activity, weapons, or controlled substances where prohibited.</>,
          ]}
        />
      </LegalSection>

      <LegalSection title="3. Prohibited Conduct">
        <p>You may not use the Service to:</p>
        <LegalList
          items={[
            <>Attempt to gain unauthorized access to the Service, our infrastructure, or other users&rsquo; accounts or content.</>,
            <>Probe, scan, or test the vulnerability of the Service or its infrastructure without authorization.</>,
            <>Interfere with, disrupt, or degrade the Service, including denial-of-service attacks or bandwidth abuse.</>,
            <>Circumvent rate limits, access controls, or abuse-prevention measures.</>,
            <>Use the Service to send spam or phishing communications.</>,
            <>Resell the Service or its capacity without our written permission.</>,
            <>Upload content you do not have the right to distribute.</>,
          ]}
        />
      </LegalSection>

      <LegalSection title="4. Malware Scanning and Automated Checks">
        <p>
          Uploads are subject to automated checks. Files are compared against known-malware hashes, and files up
          to 32&nbsp;MB may be submitted to a third-party scanner (VirusTotal). Files that are definitively
          identified as malicious are rejected and are not stored. These checks are best-effort safeguards, not a
          guarantee that content is safe.
        </p>
      </LegalSection>

      <LegalSection title="5. Enforcement">
        <p>
          We may investigate suspected violations of this policy, remove or disable access to content, suspend or
          terminate accounts, and cooperate with law enforcement where appropriate. We may also take action where
          required by law or to protect the Service and its users. We are not obliged to give prior notice before
          enforcement.
        </p>
        <p className="mt-3">
          If you believe content on the Service violates this policy, report it to{' '}
          <a href="mailto:abuse@torrentcdn.store" className="font-mono underline transition-colors hover:text-[color:var(--text-primary)]">abuse@torrentcdn.store</a>.
        </p>
      </LegalSection>

      <LegalSection title="6. Consequences of Violation">
        <p>
          Violations of this policy may result in content removal, account suspension or termination, forfeiture
          of prepaid fees, and referral to law enforcement for serious violations. Repeat or severe violations
          result in permanent termination.
        </p>
      </LegalSection>
    </LegalPage>
  )
}
