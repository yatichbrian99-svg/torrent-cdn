import type { ReactNode } from 'react'

/**
 * Site footer. Extracted verbatim from app/page.tsx (same links, same copy,
 * same Maidensail badge) so inner pages can share it; only the presentation
 * changed. The badge is the one pre-existing third-party image on the page and
 * is kept as-is — the redesign adds no new external requests.
 */
export default function SiteFooter({ children }: { children?: ReactNode }) {
  return (
    <footer className="site-footer">
      <div className="wrap site-footer-inner">
        <p className="site-footer-copy">TorrentCDN © 2026</p>
        <nav className="site-footer-links" aria-label="Footer">
          <a href="https://github.com/yatichbrian99-svg/torrent-cdn" target="_blank" rel="noopener noreferrer" className="footer-link">GitHub</a>
          <a href="/terms" className="footer-link">Terms</a>
          <a href="/privacy" className="footer-link">Privacy</a>
          <a href="/cookies" className="footer-link">Cookies</a>
          <a href="/aup" className="footer-link">AUP</a>
          <a href="/dmca" className="footer-link">DMCA</a>
          <a href="/docs" className="footer-link">Docs</a>
          <a href="/architecture" className="footer-link">Architecture</a>
          <a href="/tcdn-ai" className="footer-link">TCDN-AI</a>
        </nav>
        <a href="https://maidensail.com/startup/torrentcdn" rel="dofollow" className="site-footer-badge">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="https://maidensail.com/badge/torrentcdn.svg" alt="Listed on Maidensail" height="36" />
        </a>
        {children}
      </div>
    </footer>
  )
}
