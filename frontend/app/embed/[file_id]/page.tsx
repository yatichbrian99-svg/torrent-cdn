'use client'

import { Suspense } from 'react'
import { Loader2 } from 'lucide-react'
import VideoPlayer from '@/components/VideoPlayer'

export default function EmbedPage({ params }: { params: { file_id: string } }) {
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
          <VideoPlayer fileId={params.file_id} />
        </Suspense>
      </div>
    </div>
  )
}
