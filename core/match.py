"""Conservative, brand-agnostic matching helpers for source discovery."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse
import unicodedata

MODEL_TOKEN_RE = re.compile(r"(?<!\w)[\w]+(?:[./_-][\w]+)*(?!\w)", re.UNICODE)
NON_VARIANT_SUFFIXES = {"ASP", "ASPX", "HTM", "HTML", "PDF", "PHP"}
MODEL_FAMILY_MODIFIERS = {
    "ABSOLUTE", "BUSINESS", "FE", "FOLD", "LITE", "MAC", "MAX", "MINI",
    "PLUS", "PRO", "SE", "ULTRA", "XL",
}


def normalize_model(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").upper().replace("Ё", "Е")
    return "".join(character for character in text if character.isalnum())


def normalize_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower().replace("ё", "е")
    return " ".join(re.findall(r"[\w]+", text, flags=re.UNICODE))


def _model_parts(value: str | None) -> list[str]:
    return [normalize_model(part) for part in re.findall(r"[^\W_]+", value or "") if normalize_model(part)]


def compound_model_match(model: str | None, text: str | None) -> str | None:
    """Match a multi-word model by its individual word-level parts.

    ``normalize_model`` strips spaces, so a model spanning more than one word
    collapses to a single concatenated key that essentially never occurs
    literally once real text keeps the words apart (an ordinary title, a
    hyphenated URL slug). This checks the model's own parts against the
    text's own word-level tokens instead, so a genuine multi-word match is
    not scored as if the model were entirely absent. Tiering intentionally
    mirrors the relevance-gate's own compound check so the two stay in
    agreement without either depending on the other. Returns None when the
    model is not itself multi-word, or when nothing meaningful matched.
    """
    parts = _model_parts(model)
    if len(parts) < 2:
        return None
    actual = {normalize_model(token) for token in re.findall(r"[^\W_]+", text or "") if normalize_model(token)}
    if not actual:
        return None
    if all(part in actual for part in parts):
        return "exact"
    strong = [
        part for part in parts
        if len(part) >= 5
        and any(character.isalpha() for character in part)
        and any(character.isdigit() for character in part)
    ]
    if any(part in actual for part in strong):
        return "exact"
    matched = [part for part in parts if part in actual]
    if len(matched) >= 2 and any(any(character.isdigit() for character in part) for part in matched):
        return "likely"
    return None


def model_match(model: str | None, text: str | None) -> str:
    """Classify an identifier, keeping explicit regional/variant suffixes distinct."""
    expected = normalize_model(model)
    if not expected or not text:
        return "unknown"

    tokens = [(raw, normalize_model(raw)) for raw in MODEL_TOKEN_RE.findall(text)]

    # Prefer explicit variant evidence over an unsuffixed mention elsewhere in
    # the same snippet (for example a title plus its /LP product URL).
    for raw, candidate in tokens:
        parts = [part for part in re.split(r"[\/_-]+", raw) if part]
        normalized_parts = [normalize_model(part) for part in parts]
        for index, part in enumerate(normalized_parts[:-1]):
            suffix = normalized_parts[index + 1]
            if (part == expected and suffix not in NON_VARIANT_SUFFIXES
                    and re.fullmatch(r"[A-Z0-9]{1,4}", suffix)):
                return "likely_variant"

    if any(candidate == expected for _, candidate in tokens):
        return "exact"
    if any(
        expected in re.split(r"[\/_-]+", raw.upper())
        or (candidate.startswith(expected) and candidate[len(expected):] in NON_VARIANT_SUFFIXES)
        for raw, candidate in tokens
    ):
        return "exact"

    # A multi-token commercial model must be evaluated as a whole before the
    # fuzzy single-token mismatch guard.  Otherwise the shared word in an
    # exact phrase such as "Pixel 9" (the token "Pixel") looks like a nearly
    # identical but shorter identifier and incorrectly wins as a mismatch.
    compound = compound_model_match(model, text)
    if compound:
        return compound

    # A nearly identical identifier which disagrees is evidence of mismatch.
    for _, candidate in tokens:
        if len(candidate) < 4 or abs(len(candidate) - len(expected)) > 2:
            continue
        common = 0
        for left, right in zip(expected, candidate):
            if left != right:
                break
            common += 1
        if common >= max(3, min(len(expected), len(candidate)) - 2):
            return "mismatch"
    return "unknown"


# Words a product URL appends to the model to name a *page of* that product.
PAGE_ROLE_WORDS = frozenset({
    "specs", "spec", "specifications", "specification", "tech", "technical",
    "overview", "features", "details",
})


def candidate_model_match(model: str | None, title: str | None, url: str | None) -> str:
    """Match identity signals without letting an incidental URL override another titled SKU."""
    expected = normalize_model(model)
    requested_parts = _model_parts(model)
    # A base-family phrase is not the same product when the result immediately
    # extends it with a well-known commercial variant modifier.  This is kept
    # generic (rather than naming products) and prevents, for example, a
    # base-model request from inheriting Pro/Plus/Ultra specifications.
    # A URL whose final path segment is exactly the requested model names this
    # product page. A family page's title that lists the model *and* its
    # sibling ("Pixel 9 Pro and Pixel 9 Pro XL") must not veto it; a title that
    # only names the variant ("iPhone 15 Pro Max") still does.
    decoded_path = unquote(urlparse(url or "").path)
    last_segment = decoded_path.rstrip("/").rsplit("/", 1)[-1]
    segment_parts = _model_parts(last_segment)
    while segment_parts and segment_parts[-1].casefold() in PAGE_ROLE_WORDS and segment_parts != requested_parts:
        segment_parts = segment_parts[:-1]  # "<model>-specs" is still that model's page
    url_names_exact_model = bool(requested_parts) and segment_parts == requested_parts
    # An exact-sounding search title cannot override a URL that explicitly
    # names another suffix of the same identifier (9a vs 9, HX9992/21 vs /12).
    # Match adjoining identifier parts, not unrelated numbers elsewhere in a URL.
    path_parts = _model_parts(decoded_path)
    if expected and not url_names_exact_model and any(
        part.startswith(expected)
        and part != expected
        and (
            part[len(expected):] in MODEL_FAMILY_MODIFIERS
            or (expected[-1].isdigit() and len(part) <= len(expected) + 2)
        )
        for part in path_parts
    ):
        return "different_variant"
    for index, expected_part in enumerate(requested_parts):
        if index and expected_part.isdigit() and len(requested_parts[index - 1]) >= 4:
            preceding = requested_parts[index - 1]
            if any(
                path_parts[offset:offset + 1] == [preceding]
                and path_parts[offset + 1] != expected_part
                and path_parts[offset + 1].isdigit()
                for offset in range(len(path_parts) - 1)
            ):
                return "different_variant"
        if expected_part.isdigit() and len(expected_part) <= 2 and index:
            prefix = requested_parts[:index]
            if any(
                path_parts[offset:offset + len(prefix)] == prefix
                and path_parts[offset + len(prefix)].startswith(expected_part)
                and path_parts[offset + len(prefix)] != expected_part
                and len(path_parts[offset + len(prefix)]) <= len(expected_part) + 2
                for offset in range(max(0, len(path_parts) - len(prefix)))
            ):
                return "different_variant"
    for is_title, text in ((True, title or ""), (False, decoded_path)):
        tokens = _model_parts(text)
        width = len(requested_parts)
        plain = modified = False
        for index in range(max(0, len(tokens) - width + 1)):
            if tokens[index:index + width] != requested_parts:
                continue
            following = tokens[index + width] if index + width < len(tokens) else ""
            if following in MODEL_FAMILY_MODIFIERS and following not in requested_parts:
                modified = True
            else:
                plain = True
        if modified and not (is_title and plain and url_names_exact_model):
            return "different_variant"
    # A search snippet may echo the requested phrase while its destination URL
    # names a different regional/model code. Distinctive alphanumeric parts
    # in the request are safer than the provider-generated snippet in this
    # direct disagreement (for example ABC400UK versus ABC400ME).
    expected_identifiers = [
        part
        for part in _model_parts(model)
        if len(part) >= 5
        and any(character.isalpha() for character in part)
        and any(character.isdigit() for character in part)
    ]
    url_parts = re.findall(r"[^\W_]+", decoded_path)
    if any(
        model_match(identifier, part) == "mismatch"
        for identifier in expected_identifiers
        for part in url_parts
    ):
        return "mismatch"
    title_result = model_match(model, title)
    # The base product omits a requested family modifier altogether.  It is
    # not a probable match for the Pro/Plus/Ultra/Max/XL SKU, even if the
    # remaining words and model number coincide exactly.
    variant_positions = [
        index for index, part in enumerate(requested_parts)
        if part in MODEL_FAMILY_MODIFIERS
    ]
    if variant_positions and title_result != "exact":
        for text in (title or "", decoded_path):
            tokens = _model_parts(text)
            for index in variant_positions:
                base_parts = requested_parts[:index]
                if (
                    base_parts
                    and any(tokens[start:start + len(base_parts)] == base_parts
                            for start in range(max(0, len(tokens) - len(base_parts) + 1)))
                    and requested_parts[index] not in tokens
                ):
                    return "different_variant"
    if title_result == "likely" and len(requested_parts) >= 2:
        path_parts = set(_model_parts(decoded_path))
        if all(part in path_parts for part in requested_parts):
            return "exact"
    if title_result in {"exact", "likely_variant", "likely", "mismatch"}:
        return title_result

    own_parts = set(requested_parts)
    explicit_other_model = any(
        candidate != expected
        and candidate not in own_parts
        and len(candidate) >= 5
        and any(character.isalpha() for character in candidate)
        and sum(character.isdigit() for character in candidate) >= 2
        for candidate in (normalize_model(raw) for raw in MODEL_TOKEN_RE.findall(title or ""))
    )
    if explicit_other_model:
        return "unknown"
    return model_match(model, decoded_path)


def article_matches(article: str | None, text: str | None) -> bool:
    expected = normalize_model(article)
    return bool(expected) and any(
        normalize_model(token) == expected for token in MODEL_TOKEN_RE.findall(text or "")
    )
