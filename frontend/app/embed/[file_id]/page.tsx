'use client';

import { Suspense } from 'react';
import { Loader2 } from 'lucide-react';
import VideoPlayer from '@/components/VideoPlayer';

export default function EmbedPage({ params }: { params: { file_id: string } }) {
  return (
    <div className="mx-auto max-w-4xl px-6 py-10 lg:px-8">
      <Suspense fallback={<div className="flex aspect-video items-center justify-center rounded-xl border border-surface-border bg-surface-card"><Loader2 size={24} className="animate-spin text-gray-500" /></div>}>
        <VideoPlayer fileId={params.file_id} />
      </Suspense>

      {params.file_id !== 'demo' && (
        <div className="mt-4 text-center">
          <p className="text-sm text-gray-600">
            Powered by <a href="/" className="font-medium text-brand-400 transition-colors hover:text-brand-300">TorrentCDN</a>
          </p>
        </div>
      )}
    </div>
  );
}
