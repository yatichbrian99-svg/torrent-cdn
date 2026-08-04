'use client'

import { Suspense } from 'react'
import { useParams } from 'next/navigation'
import { Loader2 } from 'lucide-react'
import VideoPlayer from '@/components/VideoPlayer'

export default function EmbedPage() {
  // Next 15: route params are async — useParams() is the canonical client-side
  // hook (params props are Promises in pages, which client components can't await).
  const params = useParams<{ file_id: string }>()
  const fileId = params?.file_id

  return (
    <div className="min-h-screen" style={{ backgroundColor: '#000' }}>
      <div className="mx-auto max-w-5xl px-6 py-4">
        <a href="/" className="font-mono text-sm font-medium text-white">TorrentCDN</a>
      </div>
      <div className="mx-auto max-w-[900px] px-6">
        <Suspense
          fallback={
            <div className="flex aspect-video items-center justify-center border" style={{ borderColor: '#222', backgroundColor: '#111' }}>
              <Loader2 size={24} className="animate-spin" style={{ color: '#666' }} />
            </div>
          }
        >
          <VideoPlayer fileId={fileId} />
        </Suspense>
      </div>
    </div>
  )
}
