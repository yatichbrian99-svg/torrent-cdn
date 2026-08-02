import { NextResponse } from 'next/server'

/**
 * RFC 9116 security contact file — served at /.well-known/security.txt.
 *
 * (A route handler rather than a static public/ file because Next.js does not
 * reliably serve dotfiles from the public directory. The backend serves the
 * same file on the API origin via GET /.well-known/security.txt.)
 */
const SECURITY_TXT = `Contact: https://www.torrentcdn.store
Contact: https://github.com/yatichbrian99-svg/torrent-cdn/security
Expires: 2027-08-02T00:00:00.000Z
Preferred-Languages: en
Acknowledgments: https://github.com/yatichbrian99-svg/torrent-cdn
`

export function GET() {
  return new NextResponse(SECURITY_TXT, {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  })
}
