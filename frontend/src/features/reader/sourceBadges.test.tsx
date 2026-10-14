// @vitest-environment jsdom
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { Paper } from '@/api/client'
import { SourceBadges } from './SourceBadges'

const paper = (over: Partial<Paper>) =>
  ({ source_type: null, peer_reviewed: 'unknown', evidence_access: 'metadata', ...over }) as Paper

describe('SourceBadges', () => {
  it('shows the type, peer review and evidence access', () => {
    render(<SourceBadges paper={paper({ source_type: 'journal_article', peer_reviewed: 'true', evidence_access: 'full_text' })} />)
    expect(screen.getByText('Journal article')).toBeInTheDocument()
    expect(screen.getByText('Peer-reviewed')).toBeInTheDocument()
    expect(screen.getByText('Full text')).toBeInTheDocument()
  })

  it('calls an unreviewed preprint what it is', () => {
    render(<SourceBadges paper={paper({ source_type: 'preprint', peer_reviewed: 'false', evidence_access: 'abstract' })} />)
    expect(screen.getByText('Preprint')).toBeInTheDocument()
    expect(screen.getByText('Preprint · not peer-reviewed')).toBeInTheDocument()
    expect(screen.getByText('Abstract only')).toBeInTheDocument()
  })

  it('shows no type or peer-review chip when both are unknown', () => {
    render(<SourceBadges paper={paper({})} />)
    expect(screen.getByText('Metadata only')).toBeInTheDocument()
    expect(screen.queryByText(/peer-reviewed/i)).toBeNull()
    expect(document.querySelectorAll('[data-slot="badge"]')).toHaveLength(1)
  })
})
