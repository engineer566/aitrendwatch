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


def normalize_url_key(url):
    """Return the canonical identity key of a feed URL.

    The same article can surface with string-different URLs: an XML-escaped
    ``&amp;`` vs a plain ``&`` query separator, with/without a ``#fragment``,
    or with/without ``utm_*`` campaign parameters.  Since ``news_cards``
    deduplicates by the raw URL, those variants used to be stored as separate
    rows ("twin rows") that double count and double display the article.
    This key decodes one entity layer, drops the fragment and any ``utm_*``
    query parameter so the variants collapse to one identity.

    Only URLs that look like real URLs (contain ``://``) get fragment/utm
    treatment; pseudo keys (title fallbacks, hand-made test keys such as
    ``old-gpt5``, ``C#``-looking text) stay byte-identical so ordinary text
    is never corrupted.
    """
    decoded = decode_url_entities(url)
    if "://" not in decoded:
        return decoded
    base = decoded.split("#", 1)[0]
    head, sep, query = base.partition("?")
    if not sep:
        return base
    kept = [part for part in query.split("&")
            if part and not part.lower().startswith("utm_")]
    if not kept:
        return head
    return head + "?" + "&".join(kept)


# 常见全角/半角标点与引号变体；镜像/转载标题常只差在这些写法上。ASCII
# 连字符、斜杠等有语义的字符刻意保留（GPT-5 vs GPT5 不得因去重键误压）。
# 「」『』・· 在代码里显式处理：·/・ 是分隔符变体，先替换成空白再折叠，
# 让 "AI·Agent" 与 "AI Agent"、全角/半角空格形态归一到同一键。
_PUNCTUATION_STRIP_RE = re.compile(
    r"[!！?？,，.。;；:：、()（）\[\]【】\"“”'‘’…~～「」『』]+")
_MIDDLE_DOT_CHARS = ("·", "・")


def normalized_title_key(title):
    """Return the dedup key for mirror/syndicated titles, or ``None``.

    Mirrors of one article often differ only in punctuation spelling:
    trailing punctuation present or not ("…！" vs "…!"), full-width vs
    half-width marks ("，" vs ","), middle-dot/space separators or quote
    variants.  The key folds those variants (after strip + casefold) so
    such mirrors collapse to one identity, while CJK letters, digits,
    hyphens and the remaining text stay meaningful — two genuinely
    different titles are not merged.  Empty/whitespace/punctuation-only
    titles return ``None`` so callers keep their previous no-dedup
    behaviour.

    Whitespace is removed entirely after the punctuation pass: punctuation
    with and without surrounding spaces ("A，B" vs "A, B") would otherwise
    leave asymmetric spaces that keep one mirror alive.  Removing it is
    safe for a *dedup key* — real headlines never differ only in where the
    spaces sit, and the hyphen (semantic in ``GPT-5`` vs ``GPT5``) is kept.
    """
    if title is None:
        return None
    norm = str(title).strip().casefold()
    for dot in _MIDDLE_DOT_CHARS:
        norm = norm.replace(dot, " ")
    norm = re.sub(r"\s+", " ", norm)
    norm = _PUNCTUATION_STRIP_RE.sub("", norm)
    norm = norm.replace(" ", "")
    return norm or None
