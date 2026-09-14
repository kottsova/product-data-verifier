"""Conservative, brand-agnostic matching helpers for source discovery."""

from __future__ import annotations

import re
import unicodedata

MODEL_TOKEN_RE = re.compile(r"(?<!\w)[\w]+(?:[./_-][\w]+)*(?!\w)", re.UNICODE)
NON_VARIANT_SUFFIXES = {"ASP", "ASPX", "HTM", "HTML", "PDF", "PHP"}


def normalize_model(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").upper().replace("Ё", "Е")
    return "".join(character for character in text if character.isalnum())


def normalize_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower().replace("ё", "е")
    return " ".join(re.findall(r"[\w]+", text, flags=re.UNICODE))


def _model_parts(value: str | None) -> list[str]:
    return [normalize_model(part) for part in re.findall(r"[\w]+", value or "") if normalize_model(part)]


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
    actual = {normalize_model(token) for token in re.findall(r"[\w]+", text or "") if normalize_model(token)}
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
    compound = compound_model_match(model, text)
    if compound:
        return compound
    return "unknown"


def candidate_model_match(model: str | None, title: str | None, url: str | None) -> str:
    """Match identity signals without letting an incidental URL override another titled SKU."""
    expected = normalize_model(model)
    title_result = model_match(model, title)
    if title_result in {"exact", "likely_variant", "likely", "mismatch"}:
        return title_result

    own_parts = set(_model_parts(model))
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
    return model_match(model, url)


def article_matches(article: str | None, text: str | None) -> bool:
    expected = normalize_model(article)
    return bool(expected) and any(
        normalize_model(token) == expected for token in MODEL_TOKEN_RE.findall(text or "")
    )
