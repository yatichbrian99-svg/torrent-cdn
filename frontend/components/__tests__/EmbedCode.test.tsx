import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import EmbedCode from '../EmbedCode'

describe('EmbedCode', () => {
  beforeEach(() => {
    vi.stubEnv('NEXT_PUBLIC_APP_URL', 'https://cdn.example.com')
  })

  it('renders the sdk script tag and the data attribute with the file id', () => {
    render(<EmbedCode fileId="abc-123" />)
    const code = screen.getByText((_, el) => el?.tagName === 'CODE')
    expect(code.textContent).toContain('https://cdn.example.com/sdk.js')
    expect(code.textContent).toContain('data-torrent-cdn="abc-123"')
  })

  it('copies the full embed snippet on click', async () => {
    const user = userEvent.setup()
    render(<EmbedCode fileId="abc-123" />)
    // user-event installs its own clipboard stub on setup() — spy on it.
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    await user.click(screen.getByRole('button', { name: /copy/i }))
    expect(writeText).toHaveBeenCalledWith(
      expect.stringContaining('data-torrent-cdn="abc-123"')
    )
  })
})
