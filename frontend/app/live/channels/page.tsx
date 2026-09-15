'use client'

import { useEffect } from 'react'
import { useRouter } from 'next/navigation'

/** The spec's /live/channels URL — the channel list lives at /live (the
 * dashboard's Live section home). Redirect so both URLs work. */
export default function LiveChannelsRedirect() {
  const router = useRouter()
  useEffect(() => {
    router.replace('/live')
  }, [router])
  return null
}
