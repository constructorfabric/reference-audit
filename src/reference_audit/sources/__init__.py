"""Modular database-adapter layer.

Each adapter exposes two sharply different methods (see `base.SourceAdapter`):
`lookup_by_id` (privileged, ≤1 high-precision record) and `search_by_metadata` (recall, top-k).
Disambiguation happens centrally in `reference_audit.matching`, never inside an adapter.
"""
