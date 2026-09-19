"""Stage 35: normalisation of raw attributes into a clean, provenance-preserving layer.

Input is a sequence of :class:`core.raw_extraction.RawAttribute` (Stage 34).  Nothing here
fetches, searches or imports a network client; it is a pure function of the raw records.

What it does (all generic -- no per-site or per-product knowledge):

* text hygiene: HTML entities, ``\\uXXXX`` escapes, mojibake, Unicode space/dash/quote variants,
  repeated punctuation artefacts;
* values: ``50-60 HzHz`` -> ``50-60 Hz`` (en dash), ``2000-rpm`` -> ``2000 rpm``, decimal/thousands
  separators, dimensions (``a x b x c``), dual measures (``3.32 in (84.3 mm)``), yes/no/true/false in
  several languages, unresolved i18n booleans (``specifications.translatedBoolean.no``), placeholders;
* labels: bullets/colons/footnote asterisks, UPPER_SNAKE / camelCase keys, units written in the label
  (``Power (W)``), nested ``- Steel`` children re-attached to their group path;
* a four-way classification: ``identity_metadata`` | ``product_spec`` | ``marketing_content`` | ``unknown``;
* de-duplication of *obvious* duplicates (same label + same value + compatible unit; sibling locales of
  the same page aligned by value order) while every raw record stays attached as evidence.

Not done here (later stages): canonical schema mapping, conflict resolution, translation, scoring.
Values that really differ are never merged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
import html
import re
import unicodedata
from typing import Iterable, Sequence
from urllib.parse import urlparse

from core.raw_extraction import RawAttribute, _SPEC_HINT

IDENTITY = "identity_metadata"
SPEC = "product_spec"
CONTENT = "marketing_content"
UNKNOWN = "unknown"
CLASSES = (IDENTITY, SPEC, CONTENT, UNKNOWN)
_CLASS_RANK = {IDENTITY: 0, SPEC: 1, CONTENT: 2, UNKNOWN: 3}

EN_DASH = "–"
TIMES = "×"


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in items if item))


# ---------------------------------------------------------------------------
# Text hygiene
# ---------------------------------------------------------------------------

_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_MOJIBAKE = re.compile(r"[ÃÂâ][\u0080-¿]")
_SPACES = {ord(c): " " for c in "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"}
_INVISIBLE = {ord(c): None for c in "\u200b\u200c\u200d\u2060\ufeff\u00ad"}
_QUOTES = {ord(c): "'" for c in "‘’‚′ʼ"}
_QUOTES.update({ord(c): '"' for c in "“”„″"})
_DASHES = {ord(c): "-" for c in "‐‑‒–—―−"}
_WS = re.compile(r"\s+")
_FULLWIDTH = re.compile("[！-～]")
_BULLET = re.compile(r"^[•·▪■\-*]+\s+")


def _repair_mojibake(text: str) -> str:
    if not _MOJIBAKE.search(text):
        return text
    for encoding in ("cp1252", "latin-1"):
        try:
            return text.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return text


def normalize_text(value: object, fixes: list[str] | None = None) -> str:
    """Whitespace/entity/Unicode hygiene.  Never changes the meaning of a string."""
    text = str(value if value is not None else "")
    before = text
    text = _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    for _ in range(2):  # double-encoded entities (``&amp;amp;``)
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    if fixes is not None and text != before:
        fixes.append("html_entity")
    repaired = _repair_mojibake(text)
    if repaired != text and fixes is not None:
        fixes.append("mojibake")
    text = _FULLWIDTH.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), repaired)
    text = unicodedata.normalize("NFC", text)
    translated = text.translate(_SPACES).translate(_INVISIBLE).translate(_QUOTES).translate(_DASHES)
    if fixes is not None and translated != text:
        fixes.append("unicode_variant")
    return _WS.sub(" ", translated).strip()


def _tidy_punctuation(text: str, fixes: list[str] | None = None) -> str:
    out = re.sub(r"\s+([,;:!?])", r"\1", text)
    out = re.sub(r"\s+\.(?=\s|$)", ".", out)
    out = re.sub(r"([,;:!?])\1+", r"\1", out)
    out = re.sub(r"(?<!\.)\.\.(?!\.)", ".", out)
    out = out.strip(" ,;:")
    if fixes is not None and out != text:
        fixes.append("punctuation")
    return out


def _fold(text: str) -> str:
    """Case-, accent- and punctuation-insensitive comparison form."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).replace("'", "")
    return _WS.sub(" ", re.sub(r"[^\w\s]|_", " ", stripped)).strip()


# ---------------------------------------------------------------------------
# Units and measures
# ---------------------------------------------------------------------------

_UNITS: dict[str, str] = {}


def _unit(canonical: str, *variants: str) -> None:
    for name in (canonical, *variants):
        _UNITS[name] = canonical


for _spec in (
    ("mm",), ("cm", "CM"), ("m",), ("km",), ("µm", "um", "μm"),
    ("in", '"', "″", "inch", "inches"), ("ft", "feet"),
    ("kg", "KG", "Kg"), ("g",), ("mg",), ("lb", "lbs"), ("oz",),
    ("L", "l", "liter", "litre", "liters", "litres"), ("ml", "mL", "ML"), ("cl",), ("dl",),
    ("V", "v", "volt", "volts", "VAC", "VDC"), ("mV",), ("kV",),
    ("W", "w", "watt", "watts", "Watt", "WATT"), ("kW", "kw", "KW"), ("VA", "va"),
    ("A",), ("mA", "ma"), ("Ah", "ah", "AH"), ("mAh", "mah", "MAH"), ("Wh", "wh"), ("kWh", "kwh", "KWH"),
    ("Hz", "hz", "HZ"), ("kHz", "khz", "KHZ"), ("MHz", "mhz", "MHZ"), ("GHz", "ghz", "GHZ"),
    ("rpm", "RPM", "U/min", "1/min", "min-1"), ("ipm", "IPM"), ("bpm", "BPM"),
    ("bar", "Bar", "BAR"), ("psi", "PSI"), ("Pa",), ("kPa", "kpa", "KPA"), ("MPa",),
    ("Nm", "N·m", "N.m"), ("N",), ("dB",), ("dB(A)", "dBA", "db(a)", "dB (A)", "DB(A)"),
    ("°C", "℃", "degC", "ºC", "°С", "° C"), ("°F", "℉", "° F"),
    ("%",), ("s", "sec", "secs", "seconds"), ("ms",), ("min", "mins", "minutes"), ("h", "hr", "hrs", "hours"),
    ("DPI", "dpi"), ("lm",), ("cd",), ("lx",), ("kbps",), ("Mbps",), ("Gbps",),
    ("MB",), ("GB", "gb"), ("TB",), ("KB",), ("px",),
    # Cyrillic units keep their language; only their case is normalised
    ("мм",), ("см",), ("м",), ("кг",), ("г",), ("л",), ("мл",),
    ("Вт", "вт", "ВТ"), ("кВт", "квт"), ("В",), ("А",),
    ("Гц", "гц", "ГЦ"), ("МБ", "мб", "Мб"),
    ("КБ", "кб", "Кб"), ("бар",), ("об/мин",),
    ("мин",), ("ч",),
):
    _unit(*_spec)

_NO_SPACE_UNITS = frozenset({"in", "s", "m", "h", "N", "A", "м", "ч", "А", "В"})
_SPACEABLE = sorted(
    (
        name for name in _UNITS
        if name not in _NO_SPACE_UNITS and re.fullmatch(r"[A-Za-z°µμЀ-ӿ]+(?:/[A-Za-zЀ-ӿ]+)?", name)
    ),
    key=len, reverse=True,
)
_SPACE_UNIT = re.compile(r"(?<![\w.,/\-])(\d+(?:[.,]\d+)?)(" + "|".join(re.escape(n) for n in _SPACEABLE) + r")(?![\w])")
_UNIT_TOKEN = r"(?:dB\s?\(A\)|°\s?[A-Za-zЀ-ӿ]|[%℃℉\"″]|[^\W\d_]{1,9}(?:/[^\W\d_]{1,5})?)"
_NUM = r"\d+(?:[.,]\d+)*"
_MEASURE = re.compile(
    rf"^(?P<a>[+-]?{_NUM})(?:\s*(?P<sep>[-~])\s*(?P<b>{_NUM}))?"
    rf"(?:\s*-?\s*(?P<u1>{_UNIT_TOKEN}))?(?:\s+(?P<u2>{_UNIT_TOKEN}))?(?:\s*\(\s*(?P<alt>[^()]+?)\s*\))?$"
)
_DIMENSIONS = re.compile(rf"^(?P<n>{_NUM}(?:\s*[x×*]\s*{_NUM}){{1,3}})(?:\s*-?\s*(?P<u>{_UNIT_TOKEN}))?$", re.I)
_UNIT_DOUBLED = re.compile(r"(?<=\d)(\s?)([^\W\d_]{2,8})(?![^\W\d_])")


def canonical_unit(token: str) -> tuple[str, bool]:
    """(canonical unit, was a duplicated token such as ``HzHz``); ``("", False)`` when not a unit."""
    token = token.strip()
    if token in _UNITS:
        return _UNITS[token], False
    half = len(token) // 2
    if len(token) % 2 == 0 and token[:half] == token[half:] and token[:half] in _UNITS:
        return _UNITS[token[:half]], True
    return "", False


def _number(token: str) -> tuple[str, float]:
    sign = "-" if token.startswith("-") else ""
    body = token.lstrip("+-")
    if "," in body and "." in body:
        decimal = "," if body.rfind(",") > body.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        body = body.replace(thousands, "").replace(decimal, ".")
    elif "," in body:
        body = body.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", body) else body.replace(",", ".")
    elif body.count(".") > 1:
        body = body.replace(".", "") if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", body) else body
    return sign + body, float(sign + body)


@dataclass(frozen=True, slots=True)
class Measure:
    kind: str                  # number | range | dimensions
    value: str                 # "50-60" (en dash), "149 x 330 x 305", "4.2"
    unit: str
    minimum: float | None
    maximum: float | None
    alternates: tuple[str, ...] = ()
    fixes: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        text = f"{self.value} {self.unit}".strip()
        return f"{text} ({'; '.join(self.alternates)})" if self.alternates else text


def parse_measure(text: str) -> Measure | None:
    """Parse a *whole* value that is a number/range/dimension with an optional unit."""
    fixes: list[str] = []
    dimensions = _DIMENSIONS.match(text)
    if dimensions:
        unit = ""
        if dimensions.group("u"):
            unit, doubled = canonical_unit(dimensions.group("u"))
            if not unit:
                return None
            if doubled:
                fixes.append("duplicated_unit")
        parts = [_number(part) for part in re.split(r"\s*[x×*]\s*", dimensions.group("n"), flags=re.I)]
        return Measure(
            "dimensions", f" {TIMES} ".join(p[0] for p in parts), unit,
            min(p[1] for p in parts), max(p[1] for p in parts), (), tuple(fixes),
        )
    match = _MEASURE.match(text)
    if not match:
        return None
    second_raw = match.group("b") or ""
    if re.match(r"0\d", second_raw):
        return None  # "910-007500": an identifier, not a range
    first, first_value = _number(match.group("a"))
    unit = ""
    if match.group("u1"):
        unit, doubled = canonical_unit(match.group("u1"))
        if not unit:
            return None
        if doubled:
            fixes.append("duplicated_unit")
        if match.group("u2"):
            if canonical_unit(match.group("u2"))[0] != unit:
                return None
            fixes.append("duplicated_unit")
    elif match.group("u2"):
        return None
    alternates: tuple[str, ...] = ()
    if match.group("alt"):
        inner = parse_measure(match.group("alt"))
        if inner is None or not inner.unit or inner.alternates or not unit:
            return None
        alternates = (inner.display,)
    if second_raw:
        second, second_value = _number(second_raw)
        if second_value < first_value:
            return None
        value, kind, low, high = f"{first}{EN_DASH}{second}", "range", first_value, second_value
        if match.group("sep") == "-":
            fixes.append("range_dash")
    else:
        value, kind, low, high = first, "number", first_value, first_value
    if re.search(r"\d,\d", match.group("a") + second_raw):
        fixes.append("decimal_separator")
    if re.search(r"\d-[^\W\d]", text):
        fixes.append("unit_separator")
    return Measure(kind, value, unit, low, high, alternates, tuple(fixes))


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

_TRUE = frozenset({"yes", "true", "ja", "oui", "si", "да", "ano", "tak", "evet", "是"})
_FALSE = frozenset({"no", "false", "nein", "non", "нет", "ne", "nie", "hayir", "否"})
_PLACEHOLDER = re.compile(
    r"^(?:[-_.*/?]+|n/?a|n\.a\.?|na|nan|null|nil|undefined|tbd|tba|not (?:available|applicable|specified)"
    r"|nicht (?:zutreffend|verfügbar|angegeben)|non (?:applicable|disponible)|non applicabile"
    r"|н/д|нет данных|не указано|не применимо)$",
    re.I,
)
_LOREM = re.compile(r"lorem ipsum|dolor sit amet", re.I)
_I18N_KEY = re.compile(r"^[A-Za-z][\w-]*(?:\.[A-Za-z][\w-]*){2,}$")
_I18N_ROOTS = frozenset({"specifications", "specs", "labels", "translations", "translated", "common", "messages", "i18n", "strings"})
_LOCALE_PREFIX = re.compile(r"(?:^|(?<=,\s))[a-z]{2}(?:-[A-Z]{2})?:\s+")


def _i18n_key(text: str) -> bool:
    if not _I18N_KEY.match(text):
        return False
    parts = text.split(".")
    return parts[0].casefold() in _I18N_ROOTS or any(re.search(r"[a-z][A-Z]", part) for part in parts)


def boolean_of(text: str) -> bool | None:
    folded = _fold(text)
    if folded in _TRUE:
        return True
    if folded in _FALSE:
        return False
    return None


@dataclass(slots=True)
class NormalizedValue:
    text: str                        # cleaned text (used for identity/content/unknown)
    kind: str = "text"               # text | number | range | dimensions | boolean | placeholder | i18n_key
    measure: Measure | None = None
    boolean: bool | None = None
    fixes: list[str] = field(default_factory=list)


def normalize_value(raw_value: str) -> NormalizedValue:
    fixes: list[str] = []
    text = normalize_text(raw_value, fixes)
    if _LOREM.search(text):
        return NormalizedValue(text, "template_text", fixes=fixes)
    if text.endswith(":") and len(text) > 1:
        return NormalizedValue(text.rstrip(": "), "heading", fixes=fixes)
    if not text or _PLACEHOLDER.match(text):
        return NormalizedValue(text, "placeholder", fixes=fixes)
    if _i18n_key(text):
        flag = text.rsplit(".", 1)[-1].casefold()
        if flag in {"yes", "no", "true", "false"}:
            fixes.append("i18n_boolean")
            return NormalizedValue(flag, "boolean", boolean=flag in {"yes", "true"}, fixes=fixes)
        return NormalizedValue(text, "i18n_key", fixes=fixes)
    flag = boolean_of(text.rstrip(".!"))
    if flag is not None:
        return NormalizedValue("true" if flag else "false", "boolean", boolean=flag, fixes=fixes)
    if len(text) > 4 and _LOCALE_PREFIX.match(text):
        fixes.append("locale_prefix")
        text = _LOCALE_PREFIX.sub("", text)
    text = _tidy_punctuation(text, fixes)
    if not text or _PLACEHOLDER.match(text):
        return NormalizedValue(text, "placeholder", fixes=fixes)
    if text.endswith(".") and len(text) <= 40 and not re.search(r"\.\s", text):
        text = text[:-1].rstrip()
        fixes.append("punctuation")
    if text.endswith("*") and text.count("*") == 1 and not text.startswith("*"):
        text = text[:-1].rstrip()
    measure = parse_measure(text)
    if measure is not None:
        fixes.extend(measure.fixes)
        return NormalizedValue(text, measure.kind, measure=measure, fixes=fixes)
    return NormalizedValue(text, "text", fixes=fixes)


def _text_units(text: str, fixes: list[str]) -> str:
    """Spec free text: ``1.5Ah 2Ah`` -> ``1.5 Ah 2 Ah``; ``50 HzHz`` -> ``50 Hz``."""
    def undouble(match: re.Match[str]) -> str:
        unit, doubled = canonical_unit(match.group(2))
        if doubled:
            fixes.append("duplicated_unit")
            return f"{match.group(1)}{unit}"
        return match.group(0)

    out = _UNIT_DOUBLED.sub(undouble, text)
    spaced = _SPACE_UNIT.sub(lambda m: f"{m.group(1)} {_UNITS[m.group(2)]}", out)
    if spaced != out:
        fixes.append("unit_spacing")
    return spaced


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

_GENERIC_GROUP = re.compile(
    r"^(?:specifications?|specs?|technical (?:data|specifications?|specs?)|tech(?:nical)? specs?|details?|product (?:details|specifications?)"
    r"|характеристики|основные характеристики)$",
    re.I,
)
_LABEL_UNIT = re.compile(r"^(?P<base>.+?)\s*(?:\(\s*(?P<u>[^()]{1,8})\s*\)|,\s*(?P<u2>[^,()\s]{1,6}))$")


def _identifier_words(token: str) -> str:
    if " " in token or not re.fullmatch(r"[\w.\-:@]+", token):
        return token
    if not ("_" in token or re.search(r"[a-z][A-Z]", token) or (token.isupper() and len(token) > 3)):
        return token
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", token).replace("_", " ").strip()
    if words.isupper() or words.islower():
        words = words.lower().capitalize()
    return _WS.sub(" ", words)


@dataclass(slots=True)
class NormalizedLabel:
    label: str            # display form
    key: str              # comparison form
    nested: bool          # was a "- child" of its group
    unit_hint: str        # canonical unit written in the label ("Power (W)"), stripped from `label`
    unit_hint_raw: str
    group_path: tuple[str, ...]
    fixes: list[str] = field(default_factory=list)

    def with_unit(self) -> NormalizedLabel:
        """The label as written, with the unit in it (used when the unit is not consumed by a value)."""
        text = f"{self.label} ({self.unit_hint_raw})"
        return NormalizedLabel(text, _fold(text), self.nested, "", "", self.group_path, self.fixes)


def normalize_label(raw_label: str, section: str = "") -> NormalizedLabel:
    fixes: list[str] = []
    text = normalize_text(raw_label, fixes)
    nested = bool(_BULLET.match(text))
    if nested:
        text = _BULLET.sub("", text)
        fixes.append("bullet_marker")
    stripped = text.rstrip(":").rstrip()
    if stripped.endswith("*") and stripped.count("*") == 1:
        stripped = stripped[:-1].rstrip()
        fixes.append("footnote_marker")
    if stripped != text:
        text = stripped
        fixes.append("punctuation")
    if _i18n_key(text):
        text = text.rsplit(".", 1)[-1]
        fixes.append("i18n_label_key")
    unit_hint = unit_hint_raw = ""
    match = _LABEL_UNIT.match(text)
    if match:
        raw_unit = match.group("u") or match.group("u2") or ""
        candidate, doubled = canonical_unit(raw_unit)
        if candidate and not doubled:
            unit_hint, unit_hint_raw, text = candidate, raw_unit, match.group("base")
    text = _identifier_words(text)
    text = _WS.sub(" ", text).strip()
    path = [p for p in (_tidy_punctuation(_identifier_words(part)) for part in re.split(r"\s*>\s*", normalize_text(section))) if p]
    return NormalizedLabel(text, _fold(text), nested, unit_hint, unit_hint_raw, tuple(path), fixes)


def qualified_label(label: NormalizedLabel) -> str:
    """``Drilling capacity > Steel``: children (bullets / very short labels) keep their parent."""
    parents = [p for p in label.group_path if not _GENERIC_GROUP.match(p)]
    if (label.nested or len(label.key) <= 2) and parents:
        return f"{parents[-1]} > {label.label}"
    return label.label


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_IDENTITY_LABEL = re.compile(
    r"^(?:name|product name|sku|mpn|gtin ?\d*|ean ?\d*|upc|isbn|asin|barcode|bar code|model|model (?:number|no|code|id|name)"
    r"|(?:article|item|part|catalog(?:ue)?|order|product|reference|type|serial) (?:number|no|code|id)|product id|id|url|link"
    r"|images?|offers?|price|currency|availability|breadcrumbs?|brand|manufacturer|category|colou?r|farbe|couleur|colore|barva|цвет|kolor|udi|basic udi di"
    r"|serial number|reference)$"
)
_LD_MEASURES = frozenset({"weight", "width", "height", "depth", "length"})
_LD_CONTENT = frozenset({"description", "review", "reviews", "aggregate rating", "slogan", "disambiguating description"})
_VARIANT_FIELDS = ("color", "colour", "size", "pattern", "material")
_ID_FIELDS = ("sku", "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14")
_CONTENT_SECTION = re.compile(
    r"recycl|disclaimer|shipping|delivery|returns?\b|offers?\b|promo|newsletter|\bfaq|reviews?\b|testimonial|\bstory\b|how to|\btips?\b"
    r"|instructions?\b|why\b|ingredients?\b|cookie",
    re.I,
)
_CONTENT_LABEL = re.compile(
    r"^(?:special offers?|offers?|price|discount|shipping|delivery|add to (?:cart|bag)|view all|e-?mail|phone|client name|current product"
    r"|step \d+|subscribe|register.*|free shipping.*)\b|\(\d+\)$",
    re.I,
)
_STANDARD_LABEL = re.compile(r"^(?:EN|IEC|ISO|DIN|CISPR|ETSI|UL|CSA|ANSI|ASTM)\b[\s\d./-]*", re.I)
_FILE_SIZE = re.compile(r"\d\s?(?:mb|kb|мб|кб)\b", re.I)
_STRUCTURED_METHODS = frozenset({
    "table_row", "definition_list", "json_ld_property", "json_state_pair", "json_state_keyed_spec",
    "json_state_map", "pdf_label_colon", "pdf_tech_section_line",
})
_SENTENCE = re.compile(r"[.!?](?:\s|$)")


def _is_list(text: str) -> bool:
    segments = [s for s in re.split(r"[,;/]\s*", text) if s.strip()]
    return len(segments) >= 3 and sum(len(s.split()) for s in segments) / len(segments) <= 4


def is_prose(text: str) -> bool:
    words = len(text.split())
    if _is_list(text) and len(text) <= 400:
        return False
    return len(text) > 160 or words >= 12 or (words >= 6 and bool(_SENTENCE.search(text.rstrip(". "))))


def classify(
    label: NormalizedLabel, value: NormalizedValue, method: str, section: str, model_compact: str = "",
) -> tuple[str, str]:
    """(class, reason).  Generic signals only: method, label vocabulary, value shape, section wording."""
    key, text = label.key, value.text
    if not key:
        return UNKNOWN, "empty_label"
    if label.label.startswith("@"):
        return UNKNOWN, "schema_artifact"
    if value.kind == "placeholder":
        return UNKNOWN, "placeholder_value"
    if value.kind == "template_text":
        return UNKNOWN, "template_text"
    if value.kind == "i18n_key":
        return UNKNOWN, "unresolved_i18n_key"
    if value.kind == "heading":
        return UNKNOWN, "value_is_heading"
    if method == "json_ld_field":
        if key in _LD_MEASURES:
            return SPEC, "json_ld_measure"
        if key in _LD_CONTENT:
            return CONTENT, "json_ld_content"
        return IDENTITY, "json_ld_scalar"
    if _IDENTITY_LABEL.match(key):
        return IDENTITY, "identifier_label"
    if _FILE_SIZE.search(text) or _FILE_SIZE.search(label.label):
        return CONTENT, "download_entry"
    compact = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if len(model_compact) >= 5 and model_compact in compact and len(text.split()) <= 4:
        return IDENTITY, "value_is_model_identifier"
    if _STANDARD_LABEL.match(label.label):
        return UNKNOWN, "standard_reference"
    if parse_measure(label.label) is not None or re.match(r"^\d+(?:[.,]\d+)?\s?[^\W\d_]{1,4}$", label.label):
        return UNKNOWN, "label_is_value"
    if _CONTENT_LABEL.search(label.label):
        return CONTENT, "content_label"
    spec_section = any(_SPEC_HINT.search(part) for part in label.group_path)
    structured = method in _STRUCTURED_METHODS
    measurable = value.kind in {"number", "range", "dimensions", "boolean"}
    if _CONTENT_SECTION.search(section) and not method.startswith("json"):
        return CONTENT, "content_section"
    if is_prose(text) and not (spec_section and structured and len(label.label.split()) <= 6):
        if label.label.endswith((".", "?", "!")) or len(label.label.split()) >= 5 or not structured:
            return CONTENT, "prose_value"
    if measurable:
        return SPEC, "measurable_value"
    if structured:
        return SPEC, "structured_pair"
    if spec_section:
        return SPEC, "spec_section"
    return UNKNOWN, "unstructured_pair"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Evidence:
    """One raw record, complete, plus the normalisation fixes that were applied to it."""
    raw_index: int
    raw_label: str
    raw_value: str
    source_url: str
    source_type: str
    location: str
    method: str
    section: str = ""
    document_type: str = ""
    page_ref: str = ""
    sku_scope: str = "page"
    seen_in: tuple[str, ...] = ()
    fixes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NormalizedAttribute:
    id: str
    label: str
    label_key: str
    qualified_label: str
    group_path: tuple[str, ...]
    value: str
    value_kind: str
    unit: str
    cls: str
    class_reason: str
    evidence: tuple[Evidence, ...]
    display: str = ""
    minimum: float | None = None
    maximum: float | None = None
    boolean: bool | None = None
    alternates: tuple[str, ...] = ()
    label_aliases: tuple[str, ...] = ()
    variant: str = ""
    fixes: tuple[str, ...] = ()

    @property
    def source_urls(self) -> tuple[str, ...]:
        return _unique(e.source_url for e in self.evidence)

    @property
    def source_types(self) -> tuple[str, ...]:
        return _unique(e.source_type for e in self.evidence)

    @property
    def locations(self) -> tuple[str, ...]:
        return _unique(loc for e in self.evidence for loc in (e.location, *e.seen_in))

    @property
    def methods(self) -> tuple[str, ...]:
        return _unique(e.method for e in self.evidence)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data.update(
            evidence=[e.to_dict() for e in self.evidence], source_urls=list(self.source_urls),
            source_types=list(self.source_types), locations=list(self.locations), methods=list(self.methods),
        )
        return data


@dataclass(frozen=True, slots=True)
class IdentityNode:
    """One JSON-LD ``Product`` node: the model itself, a variant, or the parent of variants."""
    source_url: str
    role: str                     # product | variant | parent
    variant: str
    fields: dict[str, str]
    raw_indices: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    product_name: str
    brand: str
    model: str
    attributes: tuple[NormalizedAttribute, ...]
    identity_nodes: tuple[IdentityNode, ...]
    raw_count: int
    raw_by_class: dict[str, int]
    fixes: dict[str, int]
    network_calls: int = 0        # always 0: normalisation only reads the raw records

    def of_class(self, cls: str) -> tuple[NormalizedAttribute, ...]:
        return tuple(a for a in self.attributes if a.cls == cls)

    def to_dict(self) -> dict[str, object]:
        return {
            "product_name": self.product_name, "brand": self.brand, "model": self.model,
            "raw_count": self.raw_count, "raw_by_class": self.raw_by_class, "fixes": self.fixes,
            "normalized_count": len(self.attributes), "network_calls": self.network_calls,
            "identity_nodes": [n.to_dict() for n in self.identity_nodes],
            "attributes": [a.to_dict() for a in self.attributes],
        }


# ---------------------------------------------------------------------------
# Normalisation of a sequence of raw records
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _Record:
    index: int
    raw: RawAttribute
    label: NormalizedLabel
    value: NormalizedValue
    cls: str = UNKNOWN
    reason: str = ""
    unit: str = ""
    variant: str = ""
    text: str = ""            # final normalised value text
    display: str = ""
    fixes: list[str] = field(default_factory=list)

    @property
    def label_key(self) -> str:
        """Nested children ("- Steel", "Hi") are only comparable inside their group."""
        if self.label.nested or len(self.label.key) <= 2:
            return _fold(qualified_label(self.label))
        return self.label.key

    @property
    def value_key(self) -> str:
        return self.text if self.value.kind in {"number", "range", "dimensions"} else _fold(self.text)


def _identity_nodes(records: list[_Record]) -> list[IdentityNode]:
    """Group consecutive JSON-LD scalar records of a source into Product nodes and decide their role."""
    nodes: list[tuple[str, dict[str, str], list[int]]] = []
    for record in records:
        if record.raw.method != "json_ld_field":
            continue
        url = record.raw.source_url
        if not nodes or nodes[-1][0] != url or record.label.key in nodes[-1][1]:
            nodes.append((url, {}, []))
        nodes[-1][1][record.label.key] = record.value.text
        nodes[-1][2].append(record.index)

    def own_id(fields: dict[str, str]) -> str:
        return next((fields[k] for k in _ID_FIELDS if fields.get(k)), "")

    out: list[IdentityNode] = []
    for position, (url, fields, indices) in enumerate(nodes):
        siblings = [f for p, (u, f, _) in enumerate(nodes) if u == url and p != position]
        sibling_ids = {own_id(f) for f in siblings} - {""}
        identifier = own_id(fields)
        if identifier and (any(fields.get(k) for k in _VARIANT_FIELDS) or sibling_ids - {identifier}):
            role = "variant"
        elif not identifier and any(own_id(f) or any(f.get(k) for k in _VARIANT_FIELDS) for f in siblings):
            role = "parent"
        else:
            role = "product"
        variant = ""
        if role == "variant":
            variant = next((fields[k] for k in _VARIANT_FIELDS if fields.get(k)), "") or fields.get("name", "") or identifier
        out.append(IdentityNode(url, role, variant, dict(fields), tuple(indices)))
    return out


def _host_and_leaf(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    return parsed.netloc.casefold(), parsed.path.rstrip("/").rsplit("/", 1)[-1].casefold()


def _finalise_value(record: _Record) -> None:
    """Spec values get their measure rewritten (unit, dash, separators); everything else stays cleaned text."""
    value, label = record.value, record.label
    if record.cls == SPEC and value.measure is not None:
        measure, unit = value.measure, value.measure.unit
        if label.unit_hint:
            if not unit:
                unit = label.unit_hint
                record.fixes.append("label_unit_moved")
            elif unit == label.unit_hint:
                record.fixes.append("duplicated_unit")
        record.unit, record.text = unit, measure.value
        record.display = f"{measure.value} {unit}".strip()
        if measure.alternates:
            record.display += f" ({'; '.join(measure.alternates)})"
    elif record.cls == SPEC and value.kind == "text":
        record.text = record.display = _text_units(value.text, record.fixes)
    else:
        record.text = record.display = value.text
    consumed = bool(label.unit_hint) and record.unit == label.unit_hint and record.cls == SPEC and value.measure is not None
    if label.unit_hint and not consumed:
        record.label = label.with_unit()


def normalize_attributes(
    attributes: Sequence[RawAttribute], model: str = "",
) -> tuple[tuple[NormalizedAttribute, ...], tuple[IdentityNode, ...], dict[str, int]]:
    model_compact = re.sub(r"[^A-Za-z0-9]", "", model or "").upper()
    records: list[_Record] = []
    for index, raw in enumerate(attributes):
        label = normalize_label(raw.raw_label, raw.section)
        value = normalize_value(raw.raw_value)
        record = _Record(index, raw, label, value, fixes=[*label.fixes, *value.fixes])
        record.cls, record.reason = classify(label, value, raw.method, raw.section, model_compact)
        records.append(record)

    # a section that carries template text (lorem ipsum form block) is not product data
    placeholder_sections = {(r.raw.source_url, r.raw.section) for r in records if r.reason == "template_text" and r.raw.section}
    for record in records:
        if (
            record.reason != "template_text" and (record.raw.source_url, record.raw.section) in placeholder_sections
            and record.cls != UNKNOWN and record.raw.method != "json_ld_field"
            and record.value.kind not in {"number", "range", "dimensions"}
        ):
            record.cls, record.reason = UNKNOWN, "template_block"

    nodes = _identity_nodes(records)
    variant_of = {i: node.variant for node in nodes if node.role == "variant" for i in node.raw_indices}
    for record in records:
        record.variant = variant_of.get(record.index, "")
        _finalise_value(record)

    # ---- clustering: identical label + value (+ compatible unit) -----------------------------------
    parent = list(range(len(records)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    buckets: dict[tuple[str, str, str], list[int]] = {}
    for record in records:
        buckets.setdefault((record.label_key, record.value_key, record.variant), []).append(record.index)
    for members in buckets.values():
        by_unit: dict[str, int] = {}
        for i in members:
            if records[i].unit:
                union(i, by_unit.setdefault(records[i].unit, i))
        blanks = [i for i in members if not records[i].unit]
        anchor = next(iter(by_unit.values())) if len(by_unit) == 1 else (blanks[0] if blanks else None)
        for i in blanks:
            union(i, anchor)

    # ---- sibling locales of one page: align by value order (labels are translated, values are not) ---
    streams: dict[tuple[str, str, str], list[int]] = {}
    for record in records:
        if record.cls in {SPEC, IDENTITY}:
            host, leaf = _host_and_leaf(record.raw.source_url)
            streams.setdefault((host, leaf, record.raw.source_type), []).append(record.index)
    for members in streams.values():
        by_stream: dict[tuple[str, str], list[int]] = {}
        for i in members:
            by_stream.setdefault((records[i].raw.source_url, records[i].raw.method), []).append(i)
        keys = list(by_stream)
        for n, ka in enumerate(keys):
            for kb in keys[n + 1:]:
                # sibling locales (same method) or the same page's JSON-LD vs JSON state (same url)
                if ka == kb or (ka[0] != kb[0] and ka[1] != kb[1]):
                    continue
                left, right = by_stream[ka], by_stream[kb]
                if len(left) < 4 or len(right) < 4:
                    continue
                matcher = SequenceMatcher(
                    None, [records[i].value_key for i in left], [records[i].value_key for i in right], autojunk=False,
                )
                for block in matcher.get_matching_blocks():
                    if block.size < 4 or len({records[left[block.a + k]].value_key for k in range(block.size)}) < 2:
                        continue
                    for k in range(block.size):
                        i, j = left[block.a + k], right[block.b + k]
                        if records[i].variant == records[j].variant and records[i].unit == records[j].unit:
                            union(i, j)

    groups: dict[int, list[_Record]] = {}
    for record in records:
        groups.setdefault(find(record.index), []).append(record)

    attributes: list[NormalizedAttribute] = []
    for members in sorted(groups.values(), key=lambda g: g[0].index):
        first = min(members, key=lambda r: (_CLASS_RANK[r.cls], r.index))
        measure = first.value.measure if first.cls == SPEC else None
        unit = next((r.unit for r in members if r.unit), "")
        display = f"{first.text} {unit}".strip() if unit and not first.unit else first.display
        evidence = tuple(
            Evidence(
                r.index, r.raw.raw_label, r.raw.raw_value, r.raw.source_url, r.raw.source_type, r.raw.location, r.raw.method,
                r.raw.section, r.raw.document_type, r.raw.page_ref, r.raw.sku_scope, tuple(r.raw.seen_in), _unique(r.fixes),
            )
            for r in members
        )
        attributes.append(NormalizedAttribute(
            id=f"na{len(attributes) + 1:04d}", label=first.label.label, label_key=first.label.key,
            qualified_label=qualified_label(first.label), group_path=first.label.group_path,
            value=first.text, value_kind=first.value.kind, unit=unit, cls=first.cls, class_reason=first.reason,
            evidence=evidence, display=display,
            minimum=measure.minimum if measure else None, maximum=measure.maximum if measure else None,
            boolean=first.value.boolean, alternates=measure.alternates if measure else (),
            label_aliases=_unique(r.label.label for r in members if r.label.key != first.label.key),
            variant=first.variant, fixes=_unique(f for r in members for f in r.fixes),
        ))
    fix_counts: dict[str, int] = {}
    for record in records:
        for fix in _unique(record.fixes):
            fix_counts[fix] = fix_counts.get(fix, 0) + 1
    return tuple(attributes), tuple(nodes), fix_counts
