"""HTML character references in bibliographic text.

Metadata scraped from the web reaches `.bib` files with its HTML escaping intact: `d&apos;Amore`,
`Allocation &amp; Regularization`. Left encoded, a cited author `d&apos;Amore` fails the author check
against the source's `d'Amore`, and a title gains a phantom word "amp". Only complete, `;`-terminated
references are decoded. `html.unescape` alone would also expand legacy forms without the `;`
(`&copy` in "R&D &copy" → "©"), which ordinary text can contain.
"""

from __future__ import annotations

import html
import re

_REFERENCE_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


def decode_html_entities(text: str) -> str:
    """`text` with every `;`-terminated HTML character reference decoded; anything else unchanged."""
    if "&" not in text:
        return text
    return _REFERENCE_RE.sub(lambda m: html.unescape(m.group(0)), text)
