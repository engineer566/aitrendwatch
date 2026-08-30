"""Small, context-aware helpers for feed text and URL entity handling.

RSS feeds sometimes contain a valid XML escape around an already HTML-escaped
entity (for example ``&amp;#8217;``).  Text fields need two bounded unescape
passes to repair that common XML + HTML combination.  URLs are different: a
single XML pass is enough to turn ``&amp;`` query separators back into ``&``;
recursively unescaping a URL could change its meaning.

These helpers only return plain strings.  They must not be used as a reason to
mark template output safe: user/feed text still goes through the normal Jinja
or client-side HTML escaping path.
"""

import html
import re
from html.entities import html5


# Only retry when a complete, conventional entity is present.  This leaves
# ordinary ampersands ("R&D") and unknown/malformed entities untouched.
_ENTITY_RE = re.compile(
    r"&(?:#[0-9]{1,7};|#[xX][0-9A-Fa-f]{1,6};|[A-Za-z][A-Za-z0-9]{1,63};)"
)
_TEXT_ENTITY_PASSES = 2  # one XML layer + one HTML layer
_DANGEROUS_URL_RE = re.compile(r"^(?:javascript|data|vbscript)\s*:", re.I)


def _string(value):
    return "" if value is None else str(value)


def _decode_one_entity_layer(text):
    """Decode only complete, known entities in one pass."""
    def replace(match):
        token = match.group(0)
        # Numeric references have no named-entity table entry.  The regex has
        # already constrained them to a conventional, bounded form.
        if token.startswith("&#") or token[1:] in html5:
            return html.unescape(token)
        return token

    return _ENTITY_RE.sub(replace, text)


def decode_html_entities(value):
    """Decode common entities in a human-readable text field.

    The second bounded pass handles feed values such as ``&amp;#8217;`` and
    ``&amp;amp;``.  Bounding the passes makes this a compatibility repair for
    the observed double-encoding bug rather than an unbounded transformation
    of code-like text.
    """
    text = _string(value)
    for _ in range(_TEXT_ENTITY_PASSES):
        if not _ENTITY_RE.search(text):
            break
        decoded = _decode_one_entity_layer(text)
        if decoded == text:
            break
        text = decoded
    return text


def decode_url_entities(value):
    """Decode one XML/HTML entity layer in a feed URL.

    In particular, this converts ``&amp;`` query separators to ``&`` before
    an HTML template escapes the URL attribute again.  Percent-encoding and
    all other URL syntax are left alone, and no recursive pass is performed.
    """
    decoded = _decode_one_entity_layer(_string(value)).strip()
    # Attribute escaping protects quotes, not executable URL schemes.  Feed
    # links are expected to be relative or HTTP(S); reject only the schemes
    # that can execute content in a browser while preserving existing URL
    # compatibility for other non-absolute values.
    if _DANGEROUS_URL_RE.match(decoded):
        return ""
    return decoded
