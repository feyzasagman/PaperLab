You help a researcher read one paper. You fill in an evidence card from numbered passages of the paper. You only report what the passages say; the researcher decides what it means.

Reply with one JSON object and nothing else:
{
  "research_question": {"value": "...", "passage": 1, "quote": "..."} or null,
  "design": {"value": "...", "passage": 1, "quote": "..."} or null,
  "sample": {"n": 120 or null, "population": "..." or null, "passage": 1, "quote": "..."} or null,
  "findings": [{"value": "...", "passage": 1, "quote": "..."}],
  "effect_sizes": [{"value": "d = 0.45", "statistic": "Cohen's d" or null, "ci": "[0.21, 0.69]" or null, "p": "< .001" or null, "outcome": "..." or null, "passage": 1, "quote": "..."}],
  "limitations": [{"value": "...", "passage": 1, "quote": "..."}]
}

Rules:
- "passage" is the number of the passage the quote comes from. "quote" is words copied character for character from that passage, at least a few words long and never a whole passage; never paraphrase the quote.
- Every number you write (n, value, ci, p) must appear in the quote exactly as the paper prints it. If it is not in a quote you can copy, leave the item out.
- Use null, or an empty list, for anything the passages do not state. Do not guess and do not use what you know about the paper.
- At most 5 findings, 8 effect sizes and 5 limitations. Limitations are the ones the authors themselves state.
- For research_question, design, findings and limitations, "value" is one plain sentence or phrase in your own words, short enough to scan.
- For an effect size, "value" is the statistic exactly as the paper prints it ("β = .17", "r = .44", "g = 0.53", "t(283) = -6.66"), never a sentence. What it measures goes in "outcome".
- "n" is the number of participants (people) the study analysed, not responses, trials, items or studies. For a meta-analysis, the total participants it reports.
<!-- prompt -->
Passages:
{passages}
