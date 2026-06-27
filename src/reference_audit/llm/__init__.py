"""LLM adjudication: structured-output filtering of the ambiguous candidate zone.

The LLM is authoritative only in the danger/ambiguous zones (the `adjudicate` bucket and, in M5,
the SAME_WORK tie-break). Both prompts require the model to *affirmatively assert* sameness, so a
near-miss neighbour is never force-accepted (the README's hallucination-screening guarantee).
"""
