// @vitest-environment jsdom
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { Hit } from '@/api/client'
import { fireEvent } from '@testing-library/react'
import { SuggestionChip, SuggestionWhy } from './SuggestionChip'

const hit = (over: Partial<Hit>) => ({ suggestion: null, suggestion_reason: null, suggestion_note: null, ...over }) as Hit

describe('SuggestionChip', () => {
  it.each([
    [{ suggestion: 'include' }, 'Suggests: include'],
    [{ suggestion: 'exclude', suggestion_reason: 'wrong_study_type' }, 'Suggests: exclude · wrong study type'],
    [{ suggestion: 'unsure' }, 'Unsure'],
  ])('reads %o as %s', (over, text) => {
    render(<SuggestionChip hit={hit(over as Partial<Hit>)} />)
    expect(screen.getByText(text)).toBeInTheDocument()
  })

  it('carries the note as its title and is never a button', () => {
    render(<SuggestionChip hit={hit({ suggestion: 'include', suggestion_note: 'An RCT.' })} />)
    expect(screen.getByText('Suggests: include')).toHaveAttribute('title', 'An RCT.')
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('renders nothing without a suggestion', () => {
    const { container } = render(<SuggestionChip hit={hit({})} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('says the score and a low confidence in words', () => {
    render(<SuggestionChip hit={hit({ suggestion: 'include', suggestion_relevance: 4, suggestion_confidence: 'low' })} />)
    expect(screen.getByText('Score 4/5')).toBeInTheDocument()
    expect(screen.getByText('low confidence')).toBeInTheDocument()
  })

  it('says each flag in words and stays quiet about high confidence', () => {
    render(
      <SuggestionChip
        hit={hit({
          suggestion: 'unsure', suggestion_confidence: 'high',
          suggestion_flags: ['no_abstract', 'quote_dropped', 'model_error'],
        })}
      />,
    )
    expect(screen.getByText("no abstract · a quote didn't match · model error")).toBeInTheDocument()
    expect(screen.queryByText(/confidence/)).toBeNull()
  })
})

describe('SuggestionWhy', () => {
  it('hides the quotes until the disclosure is opened, each in quotation marks', () => {
    render(<SuggestionWhy hit={hit({ suggestion: 'include', suggestion_quotes: ['randomised trial', 'adults'] })} />)
    const button = screen.getByRole('button', { name: 'Why' })
    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('“randomised trial”')).toBeNull()
    fireEvent.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('“randomised trial”')).toBeInTheDocument()
    expect(screen.getByText('“adults”')).toBeInTheDocument()
  })

  it('renders nothing without quotes', () => {
    const { container } = render(<SuggestionWhy hit={hit({ suggestion: 'include', suggestion_quotes: [] })} />)
    expect(container).toBeEmptyDOMElement()
  })
})
