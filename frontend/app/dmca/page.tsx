import LegalPage, { LegalSection, LegalList } from '@/components/LegalPage'

export default function DmcaPage() {
  return (
    <LegalPage title="DMCA / Copyright Policy" updated="August 9, 2026">
      <LegalSection title="1. Our Commitment">
        <p>
          TorrentCDN respects the intellectual property rights of others and expects users of the Service to do
          the same. We will respond to clear and valid notices of alleged copyright infringement, and will remove
          or disable access to content we are notified about, in accordance with this policy and applicable law.
        </p>
      </LegalSection>

      <LegalSection title="2. Filing a Takedown Notice">
        <p>
          If you believe content hosted on TorrentCDN infringes your copyright, send a written notice to our
          designated agent at <a href="mailto:dmca@torrentcdn.store" className="font-mono underline transition-colors hover:text-white">dmca@torrentcdn.store</a> with the
          following information:
        </p>
        <LegalList
          items={[
            <>Your full legal name and contact information (email, and mailing address or phone number).</>,
            <>Identification of the copyrighted work(s) you claim are infringed.</>,
            <>Identification of the material on the Service that you claim is infringing, with enough detail for us to locate it (for example, the file ID, embed URL, or magnet link).</>,
            <>A statement that you have a good-faith belief that the use of the material is not authorized by the copyright owner, its agent, or the law.</>,
            <>A statement, made under penalty of perjury, that the information in your notice is accurate and that you are the copyright owner or authorized to act on the owner&rsquo;s behalf.</>,
            <>Your physical or electronic signature.</>,
          ]}
        />
        <p className="mt-3">
          Please be specific. Notices that omit required information may be slower to process. Before submitting a
          notice, consider whether the use is a fair use or otherwise authorized — knowingly submitting a false
          notice may expose you to liability.
        </p>
      </LegalSection>

      <LegalSection title="3. What Happens Next">
        <p>
          Upon receiving a notice that appears valid, we will promptly remove or disable access to the identified
          material and notify the uploader. We keep records of takedown notices and may share them as required by
          law.
        </p>
      </LegalSection>

      <LegalSection title="4. Counter-Notice">
        <p>
          If you believe your content was removed by mistake or misidentification, you may send a counter-notice
          to <a href="mailto:dmca@torrentcdn.store" className="font-mono underline transition-colors hover:text-white">dmca@torrentcdn.store</a> including:
        </p>
        <LegalList
          items={[
            <>Your full legal name and contact information.</>,
            <>Identification of the material removed and where it appeared before removal.</>,
            <>A statement, under penalty of perjury, that you have a good-faith belief the material was removed as a result of mistake or misidentification.</>,
            <>A statement consenting to the jurisdiction of the courts in your location (or, for users outside Kenya, any judicial district in which TorrentCDN may be found) and that you will accept service of process from the party who filed the original notice.</>,
            <>Your physical or electronic signature.</>,
          ]}
        />
        <p className="mt-3">
          Upon a valid counter-notice, we may restore the material within a reasonable period unless the original
          claimant initiates legal proceedings against you.
        </p>
      </LegalSection>

      <LegalSection title="5. Repeat Infringers">
        <p>
          We maintain a policy of terminating, in appropriate circumstances, the accounts of users who repeatedly
          infringe copyright or who are repeatedly subject to valid takedown notices. A single serious violation
          (for example, distributing clearly pirated commercial content) may result in immediate termination.
        </p>
      </LegalSection>

      <LegalSection title="6. Content You Distribute">
        <p>
          If you embed or share TorrentCDN content, you remain responsible for your own use of others&rsquo;
          copyrighted material. Magnet links and embed URLs function like keys: anyone with them can access the
          content, so do not share links to content you do not have the right to distribute.
        </p>
      </LegalSection>

      <LegalSection title="7. Contact">
        <p>
          All takedown and counter-notice correspondence:{' '}
          <a href="mailto:dmca@torrentcdn.store" className="font-mono underline transition-colors hover:text-white">dmca@torrentcdn.store</a>.
        </p>
      </LegalSection>
    </LegalPage>
  )
}
