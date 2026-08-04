import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import VideoPlayer from '../VideoPlayer'

// Mock the API client: these tests exercise the file_id guard + render states,
// NOT the WebTorrent/MediaSource path (that stays covered by the embed e2e).
const mocks = vi.hoisted(() => ({
  getPublicFile: vi.fn(),
  trackEvent: vi.fn(),
}))

vi.mock('@/lib/api', () => ({
  default: { getPublicFile: mocks.getPublicFile, trackEvent: mocks.trackEvent },
}))

const VALID_UUID = '3fa85f64-5717-4562-b3fc-2c963f66afa6'

const magnetlessFile = {
  id: VALID_UUID,
  filename: 'clip.mp4',
  minio_url: 'https://storage.example.com/clip.mp4',
  magnet_link: '',
  file_size_mb: 1,
}

describe('VideoPlayer file_id guard', () => {
  beforeEach(() => {
    mocks.getPublicFile.mockReset()
    mocks.trackEvent.mockReset()
  })

  it('rejects a non-UUID file_id before any API call', () => {
    render(<VideoPlayer fileId="../../admin/stats" />)
    // The guard short-circuits — getPublicFile must never be invoked with a
    // crafted path that URL normalization could redirect.
    expect(mocks.getPublicFile).not.toHaveBeenCalled()
    expect(screen.getByText('File not found')).toBeInTheDocument()
  })

  it('rejects an empty/missing file_id without an API call', () => {
    render(<VideoPlayer fileId="" />)
    expect(mocks.getPublicFile).not.toHaveBeenCalled()
    expect(screen.getByText('File not found')).toBeInTheDocument()
  })

  it('keeps the demo player working without an API call', () => {
    render(<VideoPlayer fileId="demo" />)
    expect(mocks.getPublicFile).not.toHaveBeenCalled()
    expect(screen.getByText('Demo Player')).toBeInTheDocument()
    expect(screen.getByText('preview mode')).toBeInTheDocument()
  })

  it('renders a real file for a valid UUID', async () => {
    mocks.getPublicFile.mockResolvedValue(magnetlessFile)
    render(<VideoPlayer fileId={VALID_UUID} />)
    expect(await screen.findByText('clip.mp4')).toBeInTheDocument()
    expect(mocks.getPublicFile).toHaveBeenCalledWith(VALID_UUID)
    // The media element mounts with the direct source ready for fallback.
    expect(document.querySelector('video')).toBeInTheDocument()
  })

  it('shows File not found when the API rejects a valid UUID', async () => {
    mocks.getPublicFile.mockRejectedValue(new Error('404'))
    render(<VideoPlayer fileId={VALID_UUID} />)
    expect(await screen.findByText('File not found')).toBeInTheDocument()
    expect(mocks.getPublicFile).toHaveBeenCalledWith(VALID_UUID)
  })
})
