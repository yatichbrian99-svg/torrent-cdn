import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// Mock the api client + seeder so import behavior is controllable per test and
// no real network / WebTorrent work ever runs in jsdom.
const mocks = vi.hoisted(() => ({
  importUrl: vi.fn(),
  uploadFile: vi.fn(),
  startSeeding: vi.fn(),
}))

vi.mock('@/lib/api', () => ({
  default: {
    importUrl: mocks.importUrl,
    uploadFile: mocks.uploadFile,
  },
  // Stub of the real apiErrorMessage (pinned in lib/__tests__/api.test.ts):
  // this file only exercises the import tab's wiring, so the detail-string +
  // fallback branches are enough for the scenarios tested here.
  apiErrorMessage: (err: any, fallback = 'Something went wrong') =>
    err?.response?.data?.detail || fallback,
}))

vi.mock('@/lib/seeder', () => ({
  startSeeding: mocks.startSeeding,
}))

import UploadForm from '../UploadForm'

const SUCCESS = {
  file_id: '11111111-1111-4111-8111-111111111111',
  filename: 'imported-clip.mp4',
  minio_url: 'https://storage.example/bucket/u/f/imported-clip.mp4',
  magnet_link: 'magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01&dn=imported-clip.mp4',
  file_size_mb: 2.5,
  embed_url: 'https://www.torrentcdn.store/embed/11111111-1111-4111-8111-111111111111',
}

const URL = 'https://example.com/videos/clip.mp4'

describe('UploadForm — Import from link tab', () => {
  beforeEach(() => {
    mocks.importUrl.mockReset()
    mocks.uploadFile.mockReset()
    mocks.startSeeding.mockReset()
  })

  const switchToLinkTab = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(screen.getByRole('button', { name: /import from link/i }))
  }

  const fillAndSubmit = async (user: ReturnType<typeof userEvent.setup>, url = URL, filename?: string) => {
    await user.type(screen.getByPlaceholderText('https://example.com/video.mp4'), url)
    if (filename) {
      await user.type(screen.getByPlaceholderText(/inferred from the URL/), filename)
    }
    await user.click(screen.getByRole('button', { name: /import & store a copy/i }))
  }

  // ── Tab switching ──────────────────────────────────────────────────────────
  it('renders the file-upload dropzone by default', () => {
    render(<UploadForm />)
    expect(screen.getByText(/drop your video or audio file here/i)).toBeInTheDocument()
    expect(screen.queryByText(/import a media file by url/i)).not.toBeInTheDocument()
  })

  it('switches to the import form and back', async () => {
    const user = userEvent.setup()
    render(<UploadForm />)

    await switchToLinkTab(user)
    expect(screen.getByText(/import a media file by url/i)).toBeInTheDocument()
    expect(screen.getByPlaceholderText('https://example.com/video.mp4')).toBeInTheDocument()
    expect(screen.queryByText(/drop your video or audio file here/i)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /upload file/i }))
    expect(screen.getByText(/drop your video or audio file here/i)).toBeInTheDocument()
    expect(screen.queryByPlaceholderText('https://example.com/video.mp4')).not.toBeInTheDocument()
  })

  it('shows the security tip on the import tab', async () => {
    const user = userEvent.setup()
    render(<UploadForm />)
    await switchToLinkTab(user)
    expect(screen.getByText(/private\/local network addresses are blocked/i)).toBeInTheDocument()
  })

  // ── URL validation ─────────────────────────────────────────────────────────
  it('disables the import button until a URL is typed', async () => {
    const user = userEvent.setup()
    render(<UploadForm />)
    await switchToLinkTab(user)

    const submit = screen.getByRole('button', { name: /import & store a copy/i })
    expect(submit).toBeDisabled()

    await user.type(screen.getByPlaceholderText('https://example.com/video.mp4'), URL)
    expect(submit).toBeEnabled()
  })

  it('does not submit when the URL field is only whitespace', async () => {
    const user = userEvent.setup()
    render(<UploadForm />)
    await switchToLinkTab(user)

    await user.type(screen.getByPlaceholderText('https://example.com/video.mp4'), '   ')
    await user.keyboard('{Enter}') // Enter on the URL field

    expect(mocks.importUrl).not.toHaveBeenCalled()
  })

  // ── Import success ─────────────────────────────────────────────────────────
  it('imports with URL + filename and renders the success view', async () => {
    const user = userEvent.setup()
    mocks.importUrl.mockResolvedValue(SUCCESS)
    render(<UploadForm />)
    await switchToLinkTab(user)

    await fillAndSubmit(user, URL, 'my video.mp4')

    expect(mocks.importUrl).toHaveBeenCalledWith(URL, 'my video.mp4')
    expect(await screen.findByText('Upload successful')).toBeInTheDocument()
    expect(screen.getByText(/imported-clip\.mp4/)).toBeInTheDocument()
    // Embed code for the returned file_id.
    expect(
      screen.getByText('<div data-torrent-cdn="11111111-1111-4111-8111-111111111111"></div>')
    ).toBeInTheDocument()
  })

  it('imports without a filename (undefined is passed through)', async () => {
    const user = userEvent.setup()
    mocks.importUrl.mockResolvedValue(SUCCESS)
    render(<UploadForm />)
    await switchToLinkTab(user)

    await fillAndSubmit(user, URL)

    expect(mocks.importUrl).toHaveBeenCalledWith(URL, undefined)
    expect(await screen.findByText('Upload successful')).toBeInTheDocument()
  })

  it('submits on Enter in the URL field', async () => {
    const user = userEvent.setup()
    mocks.importUrl.mockResolvedValue(SUCCESS)
    render(<UploadForm />)
    await switchToLinkTab(user)

    await user.type(screen.getByPlaceholderText('https://example.com/video.mp4'), URL)
    await user.keyboard('{Enter}')

    expect(mocks.importUrl).toHaveBeenCalledWith(URL, undefined)
    expect(await screen.findByText('Upload successful')).toBeInTheDocument()
  })

  it('shows the in-progress state while the server downloads', async () => {
    const user = userEvent.setup()
    mocks.importUrl.mockReturnValue(new Promise(() => {})) // never resolves during the test
    render(<UploadForm />)
    await switchToLinkTab(user)

    await fillAndSubmit(user)

    // Button flips to the importing label and is disabled (no double submit).
    expect(await screen.findByRole('button', { name: /importing — server is downloading/i })).toBeDisabled()
    expect(mocks.importUrl).toHaveBeenCalledTimes(1)
  })

  it('does NOT seed the swarm after an import (no local File object)', async () => {
    const user = userEvent.setup()
    mocks.importUrl.mockResolvedValue(SUCCESS)
    render(<UploadForm />)
    await switchToLinkTab(user)

    await fillAndSubmit(user, URL, 'clip.mp4')
    await screen.findByText('Upload successful')

    // Imports skip local WebTorrent seeding entirely — no seeder started, no
    // seeding status line (a direct file upload would show one).
    expect(mocks.startSeeding).not.toHaveBeenCalled()
    expect(screen.queryByText(/seeding this file to peers/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/seeding to/i)).not.toBeInTheDocument()
  })

  // ── Import error ───────────────────────────────────────────────────────────
  it('shows the API detail message on a rejected import', async () => {
    const user = userEvent.setup()
    mocks.importUrl.mockRejectedValue({
      response: { status: 400, data: { detail: 'Import URL returned HTTP 404' } },
    })
    render(<UploadForm />)
    await switchToLinkTab(user)

    await fillAndSubmit(user)

    expect(await screen.findByText('Import URL returned HTTP 404')).toBeInTheDocument()
    // No success view was rendered, and the failed import never seeded a swarm.
    expect(screen.queryByText('Upload successful')).not.toBeInTheDocument()
    expect(mocks.startSeeding).not.toHaveBeenCalled()
    // The form is still usable for another attempt.
    expect(screen.getByRole('button', { name: /import & store a copy/i })).toBeEnabled()
  })

  it('uses the fallback message when the rejection has no detail', async () => {
    const user = userEvent.setup()
    mocks.importUrl.mockRejectedValue(new Error('network down'))
    render(<UploadForm />)
    await switchToLinkTab(user)

    await fillAndSubmit(user)

    expect(await screen.findByText('Import failed. Please try again.')).toBeInTheDocument()
    expect(mocks.startSeeding).not.toHaveBeenCalled()
  })

  it('preserves the typed URL when switching tabs mid-import', async () => {
    const user = userEvent.setup()
    render(<UploadForm />)
    await switchToLinkTab(user)
    await user.type(screen.getByPlaceholderText('https://example.com/video.mp4'), URL)

    // Flip to the file tab (the URL input unmounts) and back — React state
    // persists, so the typed URL survives.
    await user.click(screen.getByRole('button', { name: /upload file/i }))
    await user.click(screen.getByRole('button', { name: /import from link/i }))

    expect(screen.getByPlaceholderText('https://example.com/video.mp4')).toHaveValue(URL)
  })

  // ── Reset ──────────────────────────────────────────────────────────────────
  it('clears the URL field when starting another import', async () => {
    const user = userEvent.setup()
    mocks.importUrl.mockResolvedValue(SUCCESS)
    render(<UploadForm />)
    await switchToLinkTab(user)
    await fillAndSubmit(user, URL, 'clip.mp4')
    await screen.findByText('Upload successful')

    await user.click(screen.getByRole('button', { name: /upload or import another file/i }))

    const urlInput = screen.getByPlaceholderText('https://example.com/video.mp4')
    expect(urlInput).toHaveValue('')
    expect(screen.getByPlaceholderText(/inferred from the URL/)).toHaveValue('')
    expect(mocks.startSeeding).not.toHaveBeenCalled()
  })
})
