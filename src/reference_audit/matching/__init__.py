"""Matching / disambiguation — the heart.

Formal code produces interpretable *features/evidence* (`features.py`); buckets and the
SAME-OBJECT rule turn evidence into verdicts. The LLM (M4+) adjudicates only the danger/ambiguous
zones. `_best_match`'s single scalar is demoted to one advisory `composite` feature.
"""
