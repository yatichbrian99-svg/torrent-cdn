import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import FileCard from '../FileCard'

const file = {
  id: 'f1',
  filename: 'movie.mp4',
  minio_url: 'https://r2.example.com/x',
  torrent_magnet: 'magnet:?xt=urn:btih:abc',
  file_size_mb: 123.45,
  created_at: '2026-01-01T00:00:00Z',
  views: 42,
  p2p_offload_percent: 87.6,
  bandwidth_saved_gb: 5.432,
  active_peers: 3,
}

describe('FileCard', () => {
  it('renders the file metadata', () => {
    render(<FileCard file={file} onDelete={vi.fn()} />)
    expect(screen.getByText('movie.mp4')).toBeInTheDocument()
    expect(screen.getByText('123.5 MB')).toBeInTheDocument()
    expect(screen.getByText('42')).toBeInTheDocument()
    expect(screen.getByText('88%')).toBeInTheDocument()
  })

  it('copies the embed div snippet', async () => {
    const user = userEvent.setup()
    render(<FileCard file={file} onDelete={vi.fn()} />)
    // user-event installs its own clipboard stub on setup() — spy on it.
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    await user.click(screen.getByRole('button', { name: 'Copy embed code' }))
    expect(writeText).toHaveBeenCalledWith('<div data-torrent-cdn="f1"></div>')
  })

  it('requires a confirmation click before deleting', async () => {
    const onDelete = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(<FileCard file={file} onDelete={onDelete} />)

    // First click arms the confirmation — delete must NOT fire yet.
    await user.click(screen.getByRole('button', { name: 'Delete file' }))
    expect(onDelete).not.toHaveBeenCalled()

    // Cancelling leaves the file alone.
    await user.click(screen.getByRole('button', { name: 'Cancel delete' }))
    expect(onDelete).not.toHaveBeenCalled()

    // Confirming fires it with the file id.
    await user.click(screen.getByRole('button', { name: 'Delete file' }))
    await user.click(screen.getByRole('button', { name: 'Confirm delete' }))
    expect(onDelete).toHaveBeenCalledTimes(1)
    expect(onDelete).toHaveBeenCalledWith('f1')
  })
})
