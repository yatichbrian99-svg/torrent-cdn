'use client'

import { useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { isLoggedIn } from '@/lib/auth'
import UploadForm from '@/components/UploadForm'

export default function UploadPage() {
  const router = useRouter()
  useEffect(() => { if (!isLoggedIn()) router.push('/') }, [router])

  return (
    <div className="mx-auto max-w-3xl px-6 py-10">
      <a href="/dashboard" className="mb-6 inline-block text-sm transition-colors hover:text-white" style={{ color: '#666' }}>
        &larr; Dashboard
      </a>
      <h1 className="text-xl font-bold tracking-[-0.02em] text-white">Upload a file</h1>
      <div className="mt-8">
        <UploadForm />
      </div>
    </div>
  )
}
