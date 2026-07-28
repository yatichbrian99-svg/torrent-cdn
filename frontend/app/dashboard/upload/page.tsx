'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowLeft } from 'lucide-react';
import { isLoggedIn } from '@/lib/auth';
import UploadForm from '@/components/UploadForm';

export default function UploadPage() {
  const router = useRouter();

  useEffect(() => {
    if (!isLoggedIn()) router.push('/');
  }, [router]);

  return (
    <div className="mx-auto max-w-3xl px-4 py-8 sm:px-6">
      <div className="mb-6">
        <a href="/dashboard" className="mb-4 inline-flex items-center gap-1 text-xs text-gray-500 hover:text-white">
          <ArrowLeft size={13} /> Back to Dashboard
        </a>
        <h1 className="text-lg font-semibold text-white">Upload File</h1>
        <p className="mt-0.5 text-xs text-gray-500">Drag and drop or click to select a video or audio file</p>
      </div>
      <UploadForm />
    </div>
  );
}
