"""Stage 36: category-aware canonical mapping of normalised attributes.

``NormalizedAttribute -> canonical field`` (key, label, value, unit, confidence, method, evidence).
Pure and offline: it reads Stage 35 results only.  It does NOT resolve conflicts, choose winners,
enrich, score completeness or build a profile - attributes that map to the same key are kept side
by side and reported as collisions.

Generic signals only: multilingual label patterns, group/section path, unit family, value shape,
category, position inside a repeated block.  No brand, model or SKU rules.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from fractions import Fraction
import re
import unicodedata
from typing import Callable, Iterable

from core.category import CategoryResult, detect_category
from core.normalization import SPEC, Evidence, is_prose, NormalizationResult, NormalizedAttribute, _number, canonical_unit, parse_measure

MAPPED, AMBIGUOUS, UNMAPPED, NOT_A_SPEC = "mapped", "ambiguous", "unmapped", "not_a_spec"
STATUSES = (MAPPED, AMBIGUOUS, UNMAPPED, NOT_A_SPEC)

MAP_MIN = 0.70        # below this a mapping is never `mapped`
CANDIDATE_MIN = 0.40  # below this a candidate is not even listed
MARGIN = 0.10         # the best key must lead the runner-up by this much
STRONG, GENERIC, WEAK, GROUP_ONLY = 0.90, 0.72, 0.60, 0.78
SHAPE_CAP = 0.50      # label fits but the value cannot be interpreted -> never mapped
IMPLIED_CAP = 0.75    # unit was not written and had to be inferred

EN_DASH = "–"


# ---------------------------------------------------------------------------
# Folding
# ---------------------------------------------------------------------------

def fold(text: str) -> str:
    """Case/accents-insensitive form used for both labels and patterns (``Höhe`` -> ``hohe``)."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def fold_label(text: str) -> str:
    return " ".join(re.sub(r"[^\w%]+|_", " ", fold(text)).split())


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(fold(pattern).replace("(?p<", "(?P<"))


def _rxs(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(_rx(p) for p in patterns)


# ---------------------------------------------------------------------------
# Units: families and safe (exact) conversion
# ---------------------------------------------------------------------------

def _f(text: str) -> Fraction:
    return Fraction(Decimal(text))


# family -> (canonical unit, {unit: exact factor to canonical}, units tried when the unit is missing)
FAMILIES: dict[str, tuple[str, dict[str, Fraction], tuple[str, ...]]] = {
    "length": ("mm", {"mm": Fraction(1), "cm": Fraction(10), "m": Fraction(1000), "in": _f("25.4"), "ft": _f("304.8"), "µm": _f("0.001")},
               ("mm", "cm", "m")),
    "mass": ("kg", {"kg": Fraction(1), "g": _f("0.001"), "mg": _f("0.000001"), "lb": _f("0.45359237"), "oz": _f("0.028349523125")},
             ("kg", "g")),
    "volume": ("L", {"L": Fraction(1), "ml": _f("0.001"), "cl": _f("0.01"), "dl": _f("0.1")}, ("L", "ml")),
    "power": ("W", {"W": Fraction(1), "kW": Fraction(1000)}, ("W", "kW")),
    "voltage": ("V", {"V": Fraction(1), "mV": _f("0.001"), "kV": Fraction(1000)}, ("V",)),
    "current": ("A", {"A": Fraction(1), "mA": _f("0.001")}, ("A", "mA")),
    "charge": ("Ah", {"Ah": Fraction(1), "mAh": _f("0.001")}, ("Ah", "mAh")),
    "energy": ("Wh", {"Wh": Fraction(1), "kWh": Fraction(1000)}, ("Wh", "kWh")),
    "frequency": ("Hz", {"Hz": Fraction(1), "kHz": Fraction(1000), "MHz": Fraction(1000000)}, ("Hz",)),
    "rotation": ("rpm", {"rpm": Fraction(1)}, ("rpm",)),
    "impact_rate": ("ipm", {"ipm": Fraction(1), "bpm": Fraction(1)}, ("ipm",)),
    "pressure": ("bar", {"bar": Fraction(1), "kPa": _f("0.01"), "Pa": _f("0.00001"), "MPa": Fraction(10)}, ("bar",)),
    "torque": ("Nm", {"Nm": Fraction(1)}, ("Nm",)),
    "sound": ("dB(A)", {"dB(A)": Fraction(1)}, ("dB(A)",)),
    "temperature": ("°C", {"°C": Fraction(1), "°F": Fraction(5, 9)}, ("°C", "°F")),   # °F is affine, see to_canonical
    "resolution": ("DPI", {"DPI": Fraction(1)}, ("DPI",)),
    "duration_months": ("month", {"month": Fraction(1), "year": Fraction(12)}, ("month", "year")),
    "duration_years": ("year", {"year": Fraction(1), "month": Fraction(1, 12)}, ("year",)),
    "duration_h": ("h", {"h": Fraction(1), "min": Fraction(1, 60), "d": Fraction(24)}, ("h", "d")),
    "distance": ("m", {"m": Fraction(1), "cm": _f("0.01"), "mm": _f("0.001"), "ft": _f("0.3048"), "in": _f("0.0254")}, ("m", "cm", "mm")),
}
_FAMILY_OF: dict[str, list[str]] = defaultdict(list)
for _family, (_, _factors, _) in FAMILIES.items():
    for _unit in _factors:
        _FAMILY_OF[_unit].append(_family)
_CYRILLIC_UNITS = {
    "мм": "mm", "см": "cm", "м": "m", "кг": "kg", "г": "g", "л": "L", "мл": "ml", "Вт": "W", "кВт": "kW",
    "В": "V", "А": "A", "Гц": "Hz", "бар": "bar", "об/мин": "rpm", "мин": "min", "ч": "h",
}
_WORD_UNITS: dict[str, str] = {}


def _words(unit: str, *words: str) -> None:
    for word in words:
        _WORD_UNITS[fold(word)] = unit


_words("L", "litre", "litres", "liter", "liters", "литр", "литра", "литров", "litri", "litry", "litru")
_words("month", "month", "months", "monat", "monate", "monaten", "mois", "mesi", "mese", "mesic", "mesice", "mesicu", "месяц", "месяца", "месяцев")
_words("year", "year", "years", "jahr", "jahre", "jahren", "an", "ans", "annee", "annees", "anno", "anni", "rok", "roky", "let", "год", "года", "лет")
_words("d", "day", "days", "tag", "tage", "tagen", "jour", "jours", "giorno", "giorni", "den", "dni", "дней", "день", "дня")
_words("h", "hour", "hours", "stunde", "stunden", "heure", "heures", "ora", "ore", "hodina", "hodiny", "hodin", "час", "часа", "часов")


def unit_token(token: str) -> str:
    """Canonical Latin unit for a written unit/word (``Вт`` -> ``W``, ``Tage`` -> ``d``); ``""`` when unknown."""
    token = token.strip()
    if token in _CYRILLIC_UNITS:
        return _CYRILLIC_UNITS[token]
    unit, _ = canonical_unit(token)
    if unit:
        return _CYRILLIC_UNITS.get(unit, unit)
    return _WORD_UNITS.get(fold(token), "")


def families_of(unit: str) -> list[str]:
    return list(_FAMILY_OF.get(unit, ()))


def family_for(unit: str, wanted: str = "") -> str:
    """Family of ``unit``; with ``wanted`` the shared family is preferred (``m``: length vs distance)."""
    found = families_of(unit)
    if wanted and wanted in found:
        return wanted
    return found[0] if found else ""


def to_canonical(family: str, unit: str, value: Fraction) -> Fraction | None:
    """Exact conversion into the family's canonical unit; ``None`` when the unit does not belong to the family."""
    _, factors, _ = FAMILIES[family]
    if unit not in factors:
        return None
    if family == "temperature" and unit == "°F":
        return (value - 32) * Fraction(5, 9)
    return value * factors[unit]


def format_number(value: Fraction) -> tuple[str, bool]:
    """(text, rounded).  Exact when it terminates within 6 decimals, otherwise rounded half-up to 6 decimals."""
    exact = value.denominator
    for prime in (2, 5):
        while exact % prime == 0:
            exact //= prime
    quantum = Decimal("0.000001")
    number = Decimal(value.numerator) / Decimal(value.denominator)
    rounded = exact != 1 or abs(number - number.quantize(quantum)) > 0
    text = format(number.quantize(quantum) if rounded else number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return (text if text not in {"", "-0"} else "0"), rounded


_METRIC = frozenset({"mm", "cm", "m", "µm", "kg", "g", "mg", "L", "ml", "cl", "dl", "W", "kW", "V", "A", "mA", "Ah", "mAh", "Wh", "kWh"})


# ---------------------------------------------------------------------------
# Value analysis
# ---------------------------------------------------------------------------

_TRUE_WORDS = frozenset(fold(w) for w in ("yes", "true", "ja", "oui", "si", "sì", "да", "ano", "tak"))
_FALSE_WORDS = frozenset(fold(w) for w in ("no", "false", "nein", "non", "нет", "ne", "nie"))
_UNAVAILABLE = _rx(r"^(?:not available|non disponibile|non disponible|nicht verfugbar|nedostupn\w*|недоступн\w*|нет)\b")
_SEP = r"(?:-|–|—|bis|to|und|až|az|/|до)"
_UNIT_ALT = "|".join(
    [re.escape(u) for u in sorted({*_FAMILY_OF, *_CYRILLIC_UNITS} - {"in", "h", "m", "s", "N", "g", "L", "d"}, key=len, reverse=True)]
    + [f"(?i:{'|'.join(sorted((re.escape(w) for w in _WORD_UNITS), key=len, reverse=True))})"]
    + ["[LhmgN]"]
)
_EMBEDDED = re.compile(
    rf"(?<![\w.,<>≤≥~])(?P<a>\d[\d.,]*)(?:\s*(?P<sep>{_SEP})\s*(?P<b>\d[\d.,]*))?\s*(?P<u>{_UNIT_ALT})(?![\w])"
)
_WORD_FULL = re.compile(rf"^(?P<a>\d[\d.,]*)\s*(?P<u>(?i:{'|'.join(sorted((re.escape(w) for w in _WORD_UNITS), key=len, reverse=True))}))\b")
_CYR_X = re.compile(r"^(?P<n>\d[\d.,]*(?:\s*[xх×*]\s*\d[\d.,]*){1,3})$", re.I)


@dataclass(frozen=True, slots=True)
class Measurement:
    lo: Fraction
    hi: Fraction
    unit: str                # canonical Latin unit, "" when written without one
    via: str                 # stage35 | word_unit | embedded | prefix
    at_start: bool = True
    alternates: tuple[str, ...] = ()
    slash_alternatives: bool = False


@dataclass(frozen=True, slots=True)
class ValueInfo:
    kind: str                       # boolean | measure | dimensions | text
    text: str
    boolean: bool | None = None
    measure: Measurement | None = None
    parts: tuple[Fraction, ...] = ()
    unit: str = ""
    lead_boolean: bool | None = None
    lead_count: int | None = None


def _fraction(token: str) -> Fraction:
    return Fraction(Decimal(_number(token)[0]))


def _embedded(text: str) -> list[Measurement]:
    found: list[Measurement] = []
    for match in _EMBEDDED.finditer(text):
        unit = unit_token(match.group("u"))
        if not unit:
            continue
        low = _fraction(match.group("a"))
        high = _fraction(match.group("b")) if match.group("b") else low
        if high < low:
            continue
        found.append(Measurement(low, high, unit, "embedded", match.start() == 0, (), match.group("sep") == "/"))
    return found


def analyse_value(attribute: NormalizedAttribute) -> ValueInfo:
    text = attribute.display or attribute.value
    if attribute.value_kind == "boolean":
        return ValueInfo("boolean", text, boolean=attribute.boolean)
    if attribute.value_kind in {"number", "range"} and attribute.minimum is not None:
        unit = unit_token(attribute.unit) if attribute.unit else ""
        if attribute.unit and not unit:
            return ValueInfo("text", text)
        low, high = (_fraction(p) for p in (attribute.value.split(EN_DASH) if EN_DASH in attribute.value else (attribute.value,) * 2))
        return ValueInfo("measure", text, measure=Measurement(low, high, unit, "stage35", True, attribute.alternates), unit=unit)
    if attribute.value_kind == "dimensions":
        parts = tuple(_fraction(p.strip()) for p in attribute.value.split("×"))
        unit = unit_token(attribute.unit) if attribute.unit else ""
        return ValueInfo("dimensions", text, parts=parts, unit=unit)
    value = attribute.value
    folded = fold(value)
    first = re.split(r"\W+", folded.strip(), maxsplit=1)[0] if folded.strip() else ""
    lead_boolean = True if first in _TRUE_WORDS else False if first in _FALSE_WORDS or _UNAVAILABLE.search(folded) else None
    count = re.match(r"^\s*(\d+)(?![\d.,]|\s*[-–]\s*\d)", value)
    if _CYR_X.match(value.strip()) and re.search(r"[xх×*]", value):
        parts = tuple(_fraction(p) for p in re.split(r"\s*[xх×*]\s*", value.strip(), flags=re.I))
        return ValueInfo("dimensions", text, parts=parts)
    word = _WORD_FULL.match(value.strip())
    measure = None
    if word and unit_token(word.group("u")):
        number = _fraction(word.group("a"))
        measure = Measurement(number, number, unit_token(word.group("u")), "prefix", True)
    return ValueInfo("text", text, measure=measure, lead_boolean=lead_boolean, lead_count=int(count.group(1)) if count else None)


# ---------------------------------------------------------------------------
# Concepts (the canonical schema)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Concept:
    key: str
    label: str
    unit: str
    family: str
    vtype: str                                   # measure | count | boolean | text | dimensions
    categories: frozenset[str] | None
    exclude_categories: frozenset[str]
    labels: tuple[re.Pattern[str], ...]
    generic: tuple[re.Pattern[str], ...]
    weak: tuple[re.Pattern[str], ...]
    groups: tuple[re.Pattern[str], ...]
    need_group: bool
    group_only: bool
    exclude: re.Pattern[str] | None
    plausible: tuple[float, float] | None
    test: Callable[[float, float], bool] | None
    test_reason: str
    value_re: re.Pattern[str] | None            # extracts the number from text (unit stays `unit`)
    value_gate: re.Pattern[str] | None          # the text value must look like this
    prefix: bool                                 # a leading "N unit ..." is enough
    multi: bool
    component: bool
    embedded_in: tuple[str, ...]
    axis_split: bool


CONCEPTS: list[Concept] = []


def _label_of(key: str, unit: str) -> str:
    base = key[: -len(unit) - 1] if unit and key.endswith("_" + unit.lower().replace("°", "").replace("(a)", "a")) else key
    for suffix in ("_mm", "_kg", "_w", "_v", "_hz", "_a", "_ah", "_mah", "_l", "_m", "_rpm", "_ipm", "_nm", "_bar", "_c", "_dba", "_h", "_months", "_years", "_dpi"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.replace("_", " ").capitalize()


def concept(key: str, *, unit: str = "", family: str = "", vtype: str = "text", label: str = "", cats: Iterable[str] | None = None,
            xcats: Iterable[str] = (), labels: Iterable[str] = (), generic: Iterable[str] = (), weak: Iterable[str] = (),
            groups: Iterable[str] = (), need_group: bool = False, group_only: bool = False, exclude: str = "",
            plausible: tuple[float, float] | None = None, test: Callable[[float, float], bool] | None = None,
            test_reason: str = "", value_re: str = "", value_gate: str = "", prefix: bool = False, multi: bool = False,
            component: bool = False, embedded_in: Iterable[str] = (), axis_split: bool = False) -> None:
    if family and unit != FAMILIES[family][0] and vtype in {"measure", "dimensions"}:
        raise ValueError(f"{key}: unit {unit!r} is not canonical for family {family}")
    CONCEPTS.append(Concept(
        key, label or _label_of(key, unit), unit, family, vtype, frozenset(cats) if cats else None, frozenset(xcats),
        _rxs(*labels), _rxs(*generic), _rxs(*weak), _rxs(*groups), need_group, group_only, _rx(exclude) if exclude else None,
        plausible, test, test_reason, _rx(value_re) if value_re else None, _rx(value_gate) if value_gate else None,
        prefix, multi, component, tuple(embedded_in), axis_split,
    ))


OVEN, COOKTOP, AIRFRYER, TOOL, ORAL, COFFEE, PERIPH, SKIN = ("oven", "cooktop", "air_fryer", "power_tool", "oral_care",
                                                             "coffee_machine", "computer_peripheral", "skincare")
COOKING = (OVEN, COOKTOP, AIRFRYER)

# ---- universal: electrical, mass, geometry -------------------------------------------------
concept("power_w", unit="W", family="power", vtype="measure", plausible=(1, 20000), labels=(
    r"^(?:rated |nominal |total |input |max )?(?:power|wattage)(?: input| rating| consumption)?$",
    r"^(?:continuous rating input|connection rating|connected load|rated input|power consumption)$",
    r"^(?:nenn|anschluss)?leistung$|^anschlusswert$|^leistungsaufnahme$",
    r"^puissance(?: de raccordement| nominale| absorbee)?$|^potenza(?: assorbita| nominale)?$",
    r"^(?:prikon|vykon|jmenovity prikon|jmenovity vykon)$", r"^(?:мощность|номинальная мощность|потребляемая мощность)$",
), weak=(r"^connection$",))
_VOLT_LABELS = (
    r"^(?:rated |nominal |operating )?(?:voltage|spannung|tension|tensione|napeti|напряжение)$",
    r"^(?:power supply|stromversorgung|alimentation|alimentazione|napajeni|напряжение питания|netzspannung|mains voltage|supply voltage"
    r"|rated voltage|nennspannung|jmenovite napeti|номинальное напряжение)$",
)
concept("voltage_v", unit="V", family="voltage", vtype="measure", plausible=(1, 1000), xcats=(TOOL,), labels=_VOLT_LABELS)
concept("voltage_v", unit="V", family="voltage", vtype="measure", plausible=(1, 1000), cats=(TOOL,), labels=_VOLT_LABELS,
        test=lambda lo, hi: lo >= 100, test_reason="mains voltage expected >= 100 V for a power tool")
concept("battery_voltage_v", unit="V", family="voltage", vtype="measure", plausible=(1, 100), cats=(TOOL,), labels=_VOLT_LABELS,
        test=lambda lo, hi: hi <= 60, test_reason="battery voltage expected <= 60 V")
concept("battery_voltage_v", unit="V", family="voltage", vtype="measure", plausible=(1, 100), labels=(
    r"^(?:battery|akku|accu|batterie|bateria|baterie|аккумулятор)(?: voltage| spannung| tension| tensione| napeti| напряжение)$",
    r"^(?:akkuspannung|batteriespannung|tension de la batterie|tensione batteria|napeti akumulatoru|напряжение аккумулятора)$"))
concept("frequency_hz", unit="Hz", family="frequency", vtype="measure", plausible=(1, 100000), embedded_in=("voltage_v",), labels=(
    r"^(?:rated |nominal |jmenovita )?(?:frequency|frequenz|frequence|frequenza|frekvence|частота)$", r"^(?:номинальная частота)$"))
concept("rated_current_a", unit="A", family="current", vtype="measure", plausible=(0.5, 200), labels=(
    r"^(?:rated |nominal )?(?:current|stromstarke|intensite|corrente|proud|ток)$", r"^сила тока$"))
concept("fuse_rating_a", unit="A", family="current", vtype="measure", plausible=(0.5, 200), labels=(
    r"^(?:absicherung|sicherung|fuse(?: rating)?|fusible|fusibile|jistic|предохранитель)$",))
concept("weight_kg", unit="kg", family="mass", vtype="measure", plausible=(0.05, 500), component=True, labels=(
    r"^(?:net |assembled product |product |unit |device |appliance |bare tool )?(?:weight|gewicht|poids|peso|hmotnost|вес|масса)(?: net| netto| skin)?$",
    r"^(?:netto|net)gewicht$|^poids net$|^peso netto$|^вес нетто$|^масса нетто$",
), exclude=r"gross|brutto|brut\b|lordo|packed|verpackt|emball|imball|carton")
concept("gross_weight_kg", unit="kg", family="mass", vtype="measure", plausible=(0.02, 600), labels=(
    r"^(?:gross weight|bruttogewicht|poids brut|peso lordo|hruba hmotnost|вес брутто|масса брутто)$",))
_NOT_HEIGHT_ETC = (r"niche|nische|nicchia|packed|packaging|verpack|emball|imball|balen|упаков|cup|salku|tasse|tazza|чашк|cord|kabel|cable|cavo"
                   r"|шнур|tank|hopper|travel|carton|stitch|drill|capacity|volume")
for _key, _names in (
    ("height_mm", r"height|hohe|hauteur|altezza|vyska|высота"),
    ("width_mm", r"width|breite|largeur|larghezza|sirka|ширина"),
    ("depth_mm", r"depth|tiefe|profondeur|profondita|hloubka|глубина"),
    ("length_mm", r"length|lange|longueur|lunghezza|delka|длина"),
):
    concept(_key, unit="mm", family="length", vtype="measure", plausible=(1, 5000), component=True, exclude=_NOT_HEIGHT_ETC, labels=(
        rf"^(?:assembled product |product |item |overall |device |appliance |unit )?(?:{_names})$",))
concept("dimensions_mm", unit="mm", family="length", vtype="dimensions", plausible=(5, 3000), axis_split=True, exclude=r"packed|verpackt|emball|imball|balen|упаков|niche|nische|nicchia", labels=(
    r"^(?:overall |product |device |appliance )?(?:dim|dimensions?|abmessungen?|dimensioni|rozmery|размеры?|габариты)(?: des gerates| du produit| del prodotto| vyrobku)?(?: h x b x t| h x w x d| l x w x h)?$",
    r"^(?:rozmery|размеры?|abmessungen)\b"))
concept("packed_dimensions_mm", unit="mm", family="length", vtype="dimensions", plausible=(5, 3000), labels=(
    r"(?:dim|dimensions?|abmessungen?|dimensioni|rozmery|размеры?).*(?:packed|verpackt|emball|imball|balen|упаков)",
    r"(?:packed|verpackt|emball|imball).*(?:dim|dimensions?|abmessungen?|dimensioni)"))
_NICHE = {"height": r"(?:nischenhohe|height niche|niche height|hauteur.*niche|altezza.*nicchia|vyska nisy)",
          "width": r"(?:nischenbreite|width niche|niche width|largeur.*niche|larghezza.*nicchia|sirka nisy)",
          "depth": r"(?:nischentiefe|depth niche|niche depth|profondeur.*niche|profondita.*nicchia|hloubka nisy)"}
_MIN, _MAX = r"\b(?:min|minim\w*)\b", r"\b(?:max|maxim\w*|massim\w*)\b"
for _axis in ("height", "width"):
    for _tag, _bound in (("min", _MIN), ("max", _MAX)):
        concept(f"niche_{_axis}_{_tag}_mm", unit="mm", family="length", vtype="measure", cats=(OVEN, COOKTOP), plausible=(50, 3000),
                labels=(rf"^(?=.*{_NICHE[_axis]})(?=.*{_bound})",))
concept("niche_depth_mm", unit="mm", family="length", vtype="measure", cats=(OVEN, COOKTOP), plausible=(50, 3000),
        labels=(rf"^{_NICHE['depth']}",))
concept("cord_length_m", unit="m", family="distance", vtype="measure", plausible=(0.3, 15), labels=(
    r"^(?=.*(?:length|lgth|lange|longueur|lunghezza|delka|длина))(?=.*(?:cord|cable|kabel|cordon|cavo|prívod|privod|шнур|кабель))",))
concept("noise_level_dba", unit="dB(A)", family="sound", vtype="measure", labels=(
    r"^(?:sound|noise)(?: level| pressure level| power level)?$|^schall\w*$|^gerauschpegel$|^niveau sonore$|^livello di rumore$|^hladina hluku$|^уровень шума$",))
concept("energy_efficiency_class", vtype="text", labels=(
    r"energieeffizienzklasse|energy (?:efficiency )?class|classe d efficacite energetique|classe di efficienza energetica"
    r"|energeticka trida|класс энергоэффективности",))
concept("warranty_months", unit="month", family="duration_months", vtype="measure", plausible=(1, 240), prefix=True, labels=(
    r"^(?:срок гарантии|гарантия|гарантийный срок|warranty(?: period)?|garantie|garantiezeit|garanzia|zaruka|zarucni doba)$",))
concept("service_life_years", unit="year", family="duration_years", vtype="measure", plausible=(0.5, 30), prefix=True, labels=(
    r"^(?:срок службы|service life|lebensdauer|duree de vie|vita utile|zivotnost)$",))
concept("protection_class", vtype="text", labels=(
    r"^(?:класс защиты|protection class|schutzklasse|classe de protection|classe di protezione|trida ochrany)$",))
concept("ip_rating", vtype="text", labels=(r"^(?:ip rating|ip code|schutzart|indice de protection|grado di protezione|krytí|степень защиты)$",))
concept("electrical_connection_type", vtype="text", labels=(
    r"art des elektrischen anschluss|type elec connect|type de raccordement electrique|tipo di allacciamento elettrico"
    r"|electrical connection(?: type)?",))
concept("plug_type", vtype="text", labels=(r"^(?:steckerart|plug type|type de prise|tipo di spina|typ zastrcky|тип вилки)$",))
concept("connection_cable_type", vtype="text", labels=(
    r"anschlusskabelart|conne\w*ng cable type|type de cable de connexion|tipo di cavo elettrico",))
concept("included_accessories", vtype="text", multi=True, labels=(
    r"^(?:integriertes zubehor|zubehor|accessoires inclus|accessori inclusi|accessories incl(?:uded)?|included accessories|prislusenstvi"
    r"|комплектация|в комплекте|lieferumfang|scope of delivery)$",))
concept("included_accessories", vtype="text", multi=True, groups=(r"accessor|zubehor|prislusenstvi|комплект",), group_only=True)
concept("included_accessories", vtype="text", multi=True, value_gate=r"^\d+\s*(?:x\s*)?\S", labels=(
    r"^(?:handle|handstuck|manche|impugnatura|rukojet|brush heads?|burstenkopfe|burstenkopf|tetes de brosse|testine|hlavice"
    r"|travel case|reiseetui|etui de voyage|astuccio da viaggio|charger|ladegerat|chargeur|caricabatterie|nabijecka|зарядное устройство)$",))
concept("battery_type", vtype="text", labels=(
    r"^(?:battery|akku|accu|batterie|batteria|baterie)[ -]?(?:type|typ|tipo|technology|technologie)$|^batterietyp$|^akkutyp$|^тип (?:батареи|аккумулятора)$",),
    )
concept("battery_capacity_ah", unit="Ah", family="charge", vtype="measure", plausible=(0.1, 100), labels=(
    r"^(?:battery amp hours?|battery capacity|akkukapazitat|capacite de la batterie|kapacita baterie|емкость аккумулятора|ёмкость аккумулятора)$",))
concept("battery_capacity_ah", unit="Ah", family="charge", vtype="measure", plausible=(0.01, 100), embedded_in=("battery_type",))
concept("battery_count", vtype="count", labels=(r"^(?:total )?number of batteries$|^anzahl akkus$|^nombre de batteries$|^pocet baterii$",))
concept("battery_runtime_h", unit="h", family="duration_h", vtype="measure", plausible=(0.05, 20000), prefix=True, labels=(
    r"^(?:operating time|betriebsdauer|runtime|run time|battery life|battery runtime|autonomie|autonomia|doba provozu|время работы)(?: full to empty| voller bis leerer akku)?$",
    r"^(?:operating time|betriebsdauer)\b"))
concept("battery_charging_type", vtype="text", labels=(r"^(?:battery|batterie|akku|batteria|baterie|аккумулятор)$",))
concept("battery_indicator", vtype="text", labels=(r"^(?:battery indicator|akkuanzeige|indicateur de batterie|indicatore batteria)$",))
concept("wireless_technology", vtype="text", labels=(
    r"^(?:wireless technology|bluetooth wireless technology|kabellose bluetooth technologie|technologie sans fil|tecnologia wireless)$",))
concept("wireless_range_m", unit="m", family="distance", vtype="measure", plausible=(0.1, 500), labels=(
    r"^(?:wireless range|funkreichweite|portee sans fil|raggio d azione|dosah)$",))
concept("android_compatibility", vtype="text", labels=(r"^android\b",))
concept("ios_compatibility", vtype="text", labels=(r"^i ?os\b",))
concept("compatibility", vtype="text", labels=(r"^(?:compatibility|kompatibilitat|compatibilite|compatibilita|kompatibilita|совместимость)$",))
concept("system_requirements", vtype="text", labels=(r"^(?:requirements|system requirements|systemanforderungen|configuration requise)$",))
concept("display_type", vtype="text", labels=(
    r"^(?:display|screen|displej|дисплей|bildschirm|ecran)(?: type)?$", r"^(?:anzeigetyp|type d ecran|tipo di display|тип дисплея)$"))
concept("control_type", vtype="text", labels=(
    r"^(?:controls?|control panel|control type|bedienkonzept|bedienung|ovladani|управление|commande|controllo|type de commande|display control)$",))
concept("body_material", vtype="text", labels=(
    r"^(?:material korpusa|материал корпуса|gehausematerial|housing material|body material|materiau du boitier|materiale.* corpo|material.*korpus)$",))
concept("body_material", vtype="text", groups=(r"colou?r ?mate|body|housing|gehause|korpus|корпус",), need_group=True, labels=(
    r"^(?:material|materialy?|materiau|materiale|материал)$",))
concept("body_material", vtype="text", weak=(r"^(?:material|materialy?|materiau|materiale|материал)$",))
concept("voice_control", vtype="boolean", labels=(r"^(?:sprachsteuerung uber sprachassistent|voice control via voice assistant|commande vocale|controllo vocale)$",))
concept("smart_connectivity", vtype="boolean", labels=(r"vernetzt|\bwi ?fi\b|\bwlan\b|connectable|smart home",))

# ---- built-in oven / cooktop -----------------------------------------------------------------
concept("usable_volume_l", unit="L", family="volume", vtype="measure", cats=(OVEN,), plausible=(5, 500), labels=(
    r"nutzinhalt|volume utile|use vol|cavity volume|oven capacity|usable (?:volume|capacity)|cooking (?:space|volume)|objem trouby|vnitrni objem|полезный объем|объем духовки",),
    generic=(r"^(?:capacity|volume|kapazitat|inhalt|capacite|capacita|kapacita|objem|объем|вместимость)$",))
concept("temperature_range_c", unit="°C", family="temperature", vtype="measure", cats=COOKING, plausible=(30, 600), labels=(
    r"temperature ?range|temp range|temperaturbereich|niveau de temperature|gamma temperature|rozsah teplot|диапазон температур|терморегулятор|"
    r"available temperature",))
concept("cooking_methods", vtype="text", cats=(OVEN,), exclude=r"hc only|nur uber|uniquement|solo tramite|only via|anzahl|nombre|numero|number", labels=(
    r"beheizungsarten|cooking methods?|mode de cuisson|metodo di cottura|heating (?:methods|modes)|heizmethoden|heizarten|rezimy",))
concept("cooking_methods_app_only", vtype="text", cats=(OVEN,), labels=(
    r"(?:heizmethoden|methodes de chauffage|metodi di riscaldamento|heating modes|heating methods).*(?:hc only|nur uber|uniquement via|solo tramite|only via)",))
concept("cooking_methods_count", vtype="count", cats=(OVEN,), labels=(
    r"anzahl beheizungsarten|nombre de modes de cuisson|numero di funzioni e combinazioni di cottura|number of (?:cooking|heating) (?:methods|modes)|pocet rezimu",))
concept("cleaning_system", vtype="text", cats=(OVEN,), labels=(
    r"cleaning (?:integrated|system)|reinigungssystem|systeme de nettoyage|sistema di pulizia|system cisteni|система очистки",))
concept("rack_system", vtype="text", cats=(OVEN,), labels=(
    r"auszugssystem|backofenauszug|systeme d extraction|sistema d estrazione|telescopic|extraction system",))
concept("door_hinge_type", vtype="text", cats=(OVEN,), labels=(r"turanschlag|door hinge|charniere de la porte|cerniera della porta|emplacement de la charniere",))
concept("hot_air_system", vtype="text", cats=(OVEN,), labels=(r"heissluft|heisluft|hot ?air|aria calda|chaleur tournante",))
concept("steam_technology", vtype="text", cats=(OVEN,), labels=(r"steam technology|dampf|tecnologia a vapore|technologie vapeur",))
concept("sensors", vtype="text", cats=(OVEN,), labels=(r"^(?:sensors?|sensori|capteurs?|sensoren)$",))
concept("assistance_systems", vtype="text", cats=(OVEN,), labels=(r"unterstutzende funktionen|supporting systems|systemes d aide|sistemi di supporto",))
concept("installation_type", vtype="text", cats=(OVEN, COOKTOP), labels=(
    r"installation oven|installation type|einbauart|type d installation|tipo di installazione",))
concept("hob_control_integrated", vtype="boolean", cats=(OVEN,), labels=(r"integrierte kochfeldsteuereinheiten|hobs? control integrated",))
concept("hob_control_type", vtype="text", cats=(OVEN,), labels=(r"tipologia di piano controllabile|hobs? control type",))
concept("fast_preheat", vtype="boolean", cats=(OVEN,), labels=(
    r"schnellaufheizung|prechauffage rapide|preriscaldamento rapido|fast ?preheat|rychle predehrati",))
concept("pizza_function", vtype="boolean", cats=(OVEN,), labels=(r"pizza ?funktion|position pizza|funzione pizza|pizza ?setting|pizza function",))
concept("crisp_function", vtype="boolean", cats=(OVEN,), labels=(r"crisp ?function|crisp funktion|fonction crisp|funzione crisp",))
concept("air_fry_function", vtype="boolean", cats=(OVEN,), labels=(
    r"air ?fry ?function|air fry funktion|fonction air fry|funzione frittura ad aria|air fry funktion",))
concept("voice_door_opening", vtype="boolean", cats=(OVEN,), labels=(
    r"turoffnung.*sprach|door opening.*voice|ouverture de la porte.*vocale|apertura porta.*vocale",))
concept("cooking_assistant_app", vtype="boolean", cats=(OVEN,), labels=(r"kochassistent|cooking assistant|assistant de cuisson|assistente di cottura",))

# ---- air fryer ---------------------------------------------------------------------------------
concept("bowl_capacity_l", unit="L", family="volume", vtype="measure", cats=(AIRFRYER,), plausible=(0.5, 30), prefix=True, labels=(
    r"^(?:объем чаши(?: \d)?|bowl capacity|capacity per bowl|volume of (?:each )?bowl|korbvolumen|objem kosiku)$",),
    generic=(r"^(?:capacity|volume|kapazitat|inhalt|capacite|capacita|objem|объем|вместимость)$",))
concept("total_capacity_l", unit="L", family="volume", vtype="measure", cats=(AIRFRYER,), plausible=(0.5, 60), prefix=True, labels=(
    r"общий объем|total capacity|gesamtvolumen|gesamtkapazitat|capacite totale|capacita totale|celkovy objem|celkova kapacita",))
concept("bowl_material", vtype="text", cats=(AIRFRYER,), labels=(r"материал чаши|bowl material|basket material|korbmaterial|materiau du panier",))
concept("heating_element_type", vtype="text", labels=(r"тип нагревательного элемента|heating element|heizelement|resistance chauffante",))
concept("program_count", vtype="count", labels=(
    r"количество программ|number of programs?|program count|anzahl programme|nombre de programmes|numero di programmi|pocet programu",))
concept("timer_available", vtype="boolean", cats=(AIRFRYER, OVEN), labels=(r"^(?:timer|таймер|minuterie|zeitschaltuhr|casovac)$",))

# ---- coffee machine -------------------------------------------------------------------------
concept("water_tank_capacity_l", unit="L", family="volume", vtype="measure", plausible=(0.05, 30), labels=(
    r"^(?=.*(?:water|wasser|eau|acqua|vodu|vody|воды|воду))(?=.*(?:tank|reservoir|behalter|nadrz\w*|бак|резервуар|serbatoio|container))"
    r"(?=.*(?:capacity|volume|kapazitat|capacite|capacita|kapacita|objem|объем|fassungsvermogen|inhalt))",))
concept("pump_pressure_bar", unit="bar", family="pressure", vtype="measure", cats=(COFFEE,), plausible=(1, 50), labels=(
    r"pump pressure|pumpendruck|pression de la pompe|pressione (?:della )?pompa|tlak cerpadla|давление насоса",))
concept("max_cup_height_mm", unit="mm", family="length", vtype="measure", cats=(COFFEE,), plausible=(20, 400), labels=(
    r"max\w* (?:cup|tassen) ?height|cup height|maximalni vyska salku|tassenhohe|hauteur maximale de la tasse|altezza massima .*tazza|высота чашки",))
concept("heating_system", vtype="text", cats=(COFFEE,), labels=(r"^(?:heating system|heizsystem|systeme de chauffage|sistema di riscaldamento|system vytapeni|система нагрева)$",))
concept("milk_system", vtype="text", cats=(COFFEE,), labels=(r"^(?:milk system|milchsystem|systeme de lait|sistema latte|system mleka)$",))
_BEVERAGES = {
    "latte_macchiato": r"^latte macchiato(?: function)?$", "hot_milk": r"^(?:hot milk|horke mleko|heisse milch|lait chaud|latte caldo)$",
    "cappuccino": r"^(?:automatic |automaticke )?cappuccino(?: function)?$", "latte": r"^latte(?: function)?$",
    "flat_white": r"^flat white(?: function)?$", "espresso": r"^espresso(?: function)?$", "coffee": r"^(?:coffee|filter coffee)(?: function)?$",
    "americano": r"^americano(?: long black)?(?: function)?$", "hot_water": r"^hot water(?: available)?(?: dispen[cs]er)?$",
    "cold_brew": r"^cold brew(?: function)?$",
}
for _name, _pattern in _BEVERAGES.items():
    concept(f"beverage_{_name}", vtype="boolean", cats=(COFFEE,), labels=(_pattern,))
concept("removable_drip_tray", vtype="boolean", cats=(COFFEE,), labels=(r"odkapavaci tacek|drip tray|tropfschale|bac egouttoir|vassoio gocciolatore",))
concept("water_level_indicator", vtype="boolean", cats=(COFFEE,), labels=(r"ukazatel hladiny vody|water level indicator|wasserstandsanzeige|indicateur de niveau d eau",))
concept("cup_tray", vtype="boolean", cats=(COFFEE,), labels=(r"odkladaci plocha na salky|cup tray|cup holder|tassenablage|plateau chauffe-tasses",))
concept("capsule_compatible", vtype="boolean", cats=(COFFEE,), labels=(r"kapsl|capsule|kapsel|cialde|\bese pods?\b",))
concept("filter_count", vtype="count", cats=(COFFEE,), labels=(r"pocet filtru|number of filters|filter count|anzahl filter|nombre de filtres",))
concept("pre_infusion", vtype="boolean", cats=(COFFEE,), labels=(r"predspareni|pre ?infusion|bruhung|pre ?brew",))

# ---- power tool ------------------------------------------------------------------------------
concept("max_speed_rpm", unit="rpm", family="rotation", vtype="measure", cats=(TOOL,), plausible=(10, 60000), labels=(
    r"^max(?:imum)?\.? (?:speed|rpm|drehzahl)$|^hochstdrehzahl$|^vitesse maximale$|^velocita massima$|^maximalni otacky$|^максимальная скорость$|^макс скорость$",))
_NLS = (r"^(?:no load speed|leerlaufdrehzahl|vitesse a vide|velocita a vuoto|volnobezne otacky|скорость холостого хода|обороты холостого хода)$",)
concept("no_load_speed_rpm", unit="rpm", family="rotation", vtype="measure", cats=(TOOL,), plausible=(10, 60000), labels=_NLS)
concept("impact_rate_ipm", unit="ipm", family="impact_rate", vtype="measure", cats=(TOOL,), plausible=(100, 100000), labels=_NLS + (
    r"^(?:impacts? per minute|blows per minute|schlagzahl|coups par minute|colpi al minuto|udery za minutu|ударов в минуту)$",))
_NLS_GROUP = (r"no load speed|leerlaufdrehzahl|vitesse a vide|velocita a vuoto|volnobezne otacky|холост",)
_IPM_GROUP = (r"impacts? per minute|blows per minute|schlagzahl|coups par minute|colpi al minuto|udery za minutu|ударов в минуту",)
for _tag, _lab in (("high", r"^(?:hi|high|hoch|haut|alto|vysoke|высокая?)$"), ("low", r"^(?:lo|low|niedrig|bas|basso|nizke|низкая?)$")):
    concept(f"no_load_speed_{_tag}_rpm", unit="rpm", family="rotation", vtype="measure", cats=(TOOL,), plausible=(0, 60000),
            labels=(_lab,), groups=_NLS_GROUP, need_group=True)
    concept(f"impact_rate_{_tag}_ipm", unit="ipm", family="impact_rate", vtype="measure", cats=(TOOL,), plausible=(0, 100000),
            labels=(_lab,), groups=_IPM_GROUP, need_group=True)
concept("max_torque_nm", unit="Nm", family="torque", vtype="measure", cats=(TOOL,), plausible=(1, 3000), labels=(
    r"torque|drehmoment|couple|coppia|kroutici moment|крутящий момент",))
_DRILL_GROUP = (r"drilling capacity|bohrleistung|bohrdurchmesser|capacite de percage|capacita di foratura|vrtaci kapacita|диаметр сверления",)
for _mat, _lab in (("steel", r"^(?:steel|stahl|acier|acciaio|ocel|сталь|металл)$"), ("wood", r"^(?:wood|holz|bois|legno|drevo|дерево|древесина)$"),
                   ("concrete", r"^(?:concrete|beton|calcestruzzo|бетон)$"), ("holesaw", r"^(?:hole ?saw|lochsage|scie cloche|sega a tazza|коронка)$")):
    concept(f"drilling_capacity_{_mat}_mm", unit="mm", family="length", vtype="measure", cats=(TOOL,), plausible=(1, 500),
            labels=(_lab,), groups=_DRILL_GROUP, need_group=True)
concept("chuck_type", vtype="text", cats=(TOOL,), labels=(r"^(?:chuck type|bohrfuttertyp|type de mandrin|tipo di mandrino|тип патрона)$",))
concept("motor_type", vtype="text", cats=(TOOL,), labels=(r"^(?:motor type|motortyp|type de moteur|tipo di motore|typ motoru|тип двигателя)$",))
concept("corded_or_cordless", vtype="text", cats=(TOOL,), labels=(r"cordless or corded|corded or cordless|akku oder netz",))
concept("power_source", vtype="text", labels=(r"^(?:power source|stromquelle|source d alimentation|zdroj napajeni|источник питания)$",),
        weak=(r"^напряжение питания$",))
concept("tool_only", vtype="boolean", cats=(TOOL,), labels=(r"^(?:tool only|nur werkzeug|solo utensile|solo corpo)$",))
concept("charger_included", vtype="boolean", cats=(TOOL,), labels=(r"^(?:charger included|ladegerat enthalten|chargeur inclus|caricabatterie incluso)$",))
concept("clutch_setting_count", vtype="count", cats=(TOOL,), labels=(r"clutch settings|drehmomentstufen|nombre de positions d embrayage",))
concept("piece_count", vtype="count", labels=(r"^(?:piece count|number of pieces|teileanzahl)$",))
concept("case_type", vtype="text", cats=(TOOL,), labels=(r"^(?:case type|kofferart|type de coffret)$",))
concept("battery_compatibility", vtype="text", labels=(r"battery compatibility|compatible batter|akku ?kompatibilitat",))

# ---- oral care ---------------------------------------------------------------------------------
concept("brush_movements_per_min", unit="movements/min", vtype="measure", cats=(ORAL,), labels=(
    r"^(?:speed|geschwindigkeit|vitesse|velocita|rychlost|скорость)$",), value_re=(
    r"^(?P<n>\d[\d.,]*)\s*(?:brush(?:ing)?(?: head)? movements?|burstenkopfbewegungen|mouvements|movimenti)"))
concept("brushing_timer", vtype="text", cats=(ORAL,), labels=(r"^(?:timer|minuterie|zeitschaltuhr|casovac)$",))

# ---- computer peripheral -----------------------------------------------------------------------
concept("sensor_technology", vtype="text", cats=(PERIPH,), labels=(r"^(?:sensor technology|sensortechnologie|technologie du capteur|tecnologia del sensore)$",))
concept("dpi_nominal", unit="DPI", family="resolution", vtype="measure", cats=(PERIPH,), plausible=(100, 100000),
        labels=(r"^(?:nominal value|nominal dpi|nominalwert)$",), groups=(r"sensor|tracking|dpi|auflosung",), need_group=True)
concept("dpi_range", unit="DPI", family="resolution", vtype="measure", cats=(PERIPH,), plausible=(100, 100000), labels=(
    r"^dpi(?: \(.*\))?$|dpi range|dpi min",))
concept("button_count", vtype="count", cats=(PERIPH,), prefix=True, labels=(r"number of buttons|anzahl tasten|nombre de boutons|pocet tlacitek|количество кнопок",))
concept("gesture_button", vtype="boolean", cats=(PERIPH,), labels=(r"^gesture button$",))
concept("thumb_wheel", vtype="boolean", cats=(PERIPH,), labels=(r"^thumb wheel$",))
concept("scroll_wheel", vtype="text", cats=(PERIPH,), labels=(r"^(?:scroll wheel|mausrad|scrollrad|molette de defilement|колесо прокрутки)$",))
concept("customization_software", vtype="text", cats=(PERIPH,), labels=(r"customi[sz]ation app|companion app",))
concept("usb_receiver", vtype="text", cats=(PERIPH,), labels=(r"usb receiver|usb empfanger|recepteur usb",))

# ---- skincare ---------------------------------------------------------------------------------
concept("ph_range", vtype="measure", cats=(SKIN,), labels=(r"^ph(?: range| value| wert)?$",))
for _name in ("alcohol", "oil", "silicone", "water", "fragrance", "paraben", "gluten", "sulfate"):
    concept(f"is_{_name}_free", vtype="boolean", cats=(SKIN,), labels=(rf"^{_name}[ -]?free$",))
concept("is_vegan", vtype="boolean", cats=(SKIN,), labels=(r"^vegan$",))
concept("is_cruelty_free", vtype="boolean", cats=(SKIN,), labels=(r"^cruelty[ -]?free$",))

CARDINALITY = {c.key: ("list" if c.multi else "single") for c in CONCEPTS}
LABELS = {c.key: c.label for c in CONCEPTS}
UNITS = {c.key: c.unit for c in CONCEPTS}
_COMPANIONS: dict[str, list[Concept]] = defaultdict(list)
for _c in CONCEPTS:
    for _host in _c.embedded_in:
        _COMPANIONS[_host].append(_c)
_GENERIC_LABELS = _rx(r"^(?:type|typ|tip|тип|name|value|wert|valeur|info|information|details?|other|sonstige\w*)$")
_AXES = {"h": "height_mm", "в": "height_mm", "b": "width_mm", "w": "width_mm", "ш": "width_mm", "t": "depth_mm", "d": "depth_mm",
         "г": "depth_mm", "l": "length_mm"}
_AXIS_ORDER = re.compile(r"\(\s*([a-zа-я])\s*[x×х*]\s*([a-zа-я])\s*[x×х*]\s*([a-zа-я])\s*\)")


# ---------------------------------------------------------------------------
# "Not a spec": generic patterns for metadata / legal / promotional content
# ---------------------------------------------------------------------------

_NAS_LABELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("identity_metadata", _rx(r"barcode|bar code|штрихкод|\bgtin\b|\bean\b|\bupc\b|\bsku\b|artikelnummer|^product type$|тип товара|^category$|country of origin|страна производ|herkunftsland|pays d origine|paese di origine|zeme puvodu|made in")),
    ("variant_metadata", _rx(r"colou?r|farbe|couleur|colore|barva|цвет|^col (?:main|door|panel)")),
    ("commercial_metadata", _rx(r"\beligible\b|\boffers?\b|\bprice\b|availability|\bfsa\b")),
    ("documentation_metadata", _rx(r"user manual|gebrauchsanweisung|mode d emploi|manuale|manual lang|navod|руководство|инструкция")),
    ("legal_or_certification", _rx(r"\bsrn\b|^remarks?$|declaration|directive|richtlinie|regulation|conformity|compliance|harmoni|\bсъюз|европейск|^(?:en|iec|iso|cispr|din) ?\d|certificat|zertifik")),
    ("company_or_address", _rx(r"\b(?:ltd|llc|gmbh|inc|corp|ооо|тоо|зао|оао|лтд|бин|инн|vat|eori)\b|^ко\b|\bко,|^(?:дом|ул\w*|улица|street|str|strasse|каб\w*|офис|office|floor|этаж|город|city|zip|postal)$|\bhuaan|\bкитай\b")),
)
_PROMO_GROUP = _rx(r"\bcs ?chapter\b|\bchapter\b|benefit|feature highlights|why choose|marketing|promo")
_PROMO_VALUE = _rx(r"\*{2,}|up to \d+ ?x|\d+ ?x (?:more|better|healthier|effective|gesunder\w*|effektiver)|% more|bis zu \d+|gently|exceptional|invigorating|außergewöhnlich|ausserordentlich|receive|erhalten sie|track your|verfolgen sie|get (?:three|\d)|helps? you|lets you|informiert sie|automatically|automatische anpassung")
_LEGAL_VALUE = _rx(r"\bкаб\.|\bстр\.|\bул\.|\bgmbh\b|\bltd\b|\bбин\b|schedule \d")


def not_a_spec_reason(attribute: NormalizedAttribute) -> str:
    labels = [attribute.label, *attribute.label_aliases]
    folded = [fold_label(x) for x in labels]
    value = fold(attribute.value)
    first = attribute.evidence[0]
    for reason, pattern in _NAS_LABELS:
        if any(pattern.search(f) for f in folded):
            return reason
    if _LEGAL_VALUE.search(value):
        return "company_or_address"
    if first.method.startswith("pdf") and (len(folded[0].split()) >= 5 or attribute.label[:1].islower()):
        return "document_fragment"
    words = len(value.split())
    group = fold_label(" > ".join(attribute.group_path))
    promo = bool(_PROMO_VALUE.search(value))
    if promo and words >= 3:
        return "promotional_content"
    if _PROMO_GROUP.search(group) and words >= 4 and not _embedded(attribute.value) and not re.search(r"[<>≤≥]\s*\d", attribute.value):
        return "promotional_content"
    return ""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Candidate:
    canonical_key: str
    confidence: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MappedField:
    attribute: NormalizedAttribute
    status: str
    category: str
    canonical_key: str = ""
    canonical_label: str = ""
    normalized_value: str = ""
    unit: str = ""
    value_type: str = ""
    value_min: float | None = None
    value_max: float | None = None
    confidence: float = 0.0
    mapping_method: str = ""
    unit_status: str = ""              # explicit | converted | implied | none
    evidence: tuple[dict[str, object], ...] = ()
    candidates: tuple[Candidate, ...] = ()
    reasons: tuple[str, ...] = ()
    conversion: dict[str, object] | None = None
    context: dict[str, object] = field(default_factory=dict)
    derived_from: str = ""

    def to_dict(self) -> dict[str, object]:
        data = {k: getattr(self, k) for k in self.__dataclass_fields__ if k not in {"attribute", "candidates"}}
        data["candidates"] = [asdict(c) for c in self.candidates]
        data["attribute_id"] = self.attribute.id
        data["attribute"] = self.attribute.to_dict()
        return data


@dataclass(frozen=True, slots=True)
class Collision:
    canonical_key: str
    kind: str                          # same_value | equivalent_after_conversion | different_values | list_field
    members: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CanonicalMappingResult:
    product_name: str
    brand: str
    model: str
    category: CategoryResult
    fields: tuple[MappedField, ...]
    collisions: tuple[Collision, ...]
    other_attributes: int              # non-spec attributes (identity / marketing / unknown) that were not mapped at all
    network_calls: int = 0

    def attribute_status(self) -> dict[str, str]:
        best: dict[str, str] = {}
        rank = {MAPPED: 0, AMBIGUOUS: 1, UNMAPPED: 2, NOT_A_SPEC: 3}
        for f in self.fields:
            if f.attribute.id not in best or rank[f.status] < rank[best[f.attribute.id]]:
                best[f.attribute.id] = f.status
        return best

    def mapped_fields(self) -> tuple[MappedField, ...]:
        return tuple(f for f in self.fields if f.status == MAPPED)

    def summary(self) -> dict[str, object]:
        status = self.attribute_status()
        counts = Counter(status.values())
        eligible = len(status) - counts[NOT_A_SPEC]
        keys = {f.canonical_key for f in self.mapped_fields()}
        return {
            "product": self.product_name, "category": self.category.category_id, "category_confidence": self.category.confidence,
            "normalized_specs": len(status), "mapped": counts[MAPPED], "ambiguous": counts[AMBIGUOUS], "unmapped": counts[UNMAPPED],
            "not_a_spec": counts[NOT_A_SPEC], "eligible": eligible, "unique_canonical_fields": len(keys),
            "coverage": round(counts[MAPPED] / eligible, 3) if eligible else 0.0,
            "collisions": Counter(c.kind for c in self.collisions),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "product_name": self.product_name, "brand": self.brand, "model": self.model, "network_calls": self.network_calls,
            "category": {"category_id": self.category.category_id, "category_name": self.category.category_name,
                         "parent_category": self.category.parent_category, "confidence": self.category.confidence,
                         "source": self.category.source, "evidence": list(self.category.evidence)},
            "summary": {**self.summary(), "collisions": dict(self.summary()["collisions"])},
            "fields": [f.to_dict() for f in self.fields],
            "collisions": [c.to_dict() for c in self.collisions],
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _Eval:
    ok: bool = True
    cap: float | None = None
    penalty: float = 0.0
    value: str = ""
    unit: str = ""
    lo: float | None = None
    hi: float | None = None
    unit_status: str = ""
    conversion: dict[str, object] | None = None
    method: str = ""
    notes: list[str] = field(default_factory=list)
    reject: str = ""                 # rule is not applicable at all (wrong unit family)
    parts: tuple[Fraction, ...] = ()


def _number_text(low: Fraction, high: Fraction) -> tuple[str, bool]:
    a, ra = format_number(low)
    if high == low:
        return a, ra
    b, rb = format_number(high)
    return f"{a}{EN_DASH}{b}", ra or rb


def _infer_unit(c: Concept, values: tuple[Fraction, ...]) -> tuple[str | None, str]:
    """Pick the one unit under which every value is plausible; ``(None, reason)`` when none/several fit."""
    _, _, tried = FAMILIES[c.family]
    if c.plausible is None:
        return (tried[0], "") if len(tried) == 1 else (None, f"unit missing; candidates {'|'.join(tried)} without plausibility range")
    fitting = []
    for unit in tried:
        converted = [to_canonical(c.family, unit, v) for v in values]
        if all(x is not None and c.plausible[0] <= float(x) <= c.plausible[1] for x in converted):
            fitting.append(unit)
    if len(fitting) == 1:
        return fitting[0], ""
    if not fitting:
        return None, f"unit missing and value implausible in {'|'.join(tried)}"
    return None, f"unit missing; plausible as {'|'.join(fitting)}"


def _measurement_for(c: Concept, info: ValueInfo, ev: _Eval) -> Measurement | None:
    m = info.measure
    if info.kind == "measure" and m is not None:
        return m
    if m is not None and info.kind == "text" and (m.via == "prefix" and (c.prefix or c.family in {"volume", "duration_months", "duration_h"})):
        return m
    return None


def _text_measurement(c: Concept, info: ValueInfo, ev: _Eval) -> Measurement | None:
    """Measure hidden in text: ``4 литра``, ``2 year limited warranty``, ``200-8000 DPI (...)``, ``32.8 ft (10 m)*``."""
    text = info.text
    if info.measure is not None and info.measure.via == "prefix" and (c.prefix or info.measure.at_start):
        if c.prefix or re.fullmatch(r"\s*\d[\d.,]*\s*\S+\s*", text):
            return info.measure
    found = [m for m in _embedded(text) if family_for(m.unit, c.family) == c.family]
    if not found:
        return None
    first = found[0]
    if len(found) > 1:
        second = found[1]
        a, b = to_canonical(c.family, first.unit, first.lo), to_canonical(c.family, second.unit, second.lo)
        consistent = (a is not None and b is not None and b != 0 and first.lo == first.hi and second.lo == second.hi
                      and abs(float(a) / float(b) - 1) <= 0.02)
        if consistent and "(" in text:
            first = Measurement(first.lo, first.hi, first.unit, "embedded", first.at_start, (f"{format_number(second.lo)[0]} {second.unit}",))
        elif first.at_start:
            ev.notes.append(f"{len(found) - 1} further measure(s) in the text ignored")
        else:
            ev.notes.append("several measures of the same kind in the text")
            return None
    if not first.at_start:
        ev.penalty += 0.08
        ev.notes.append("measure is not at the start of the text")
    if first.slash_alternatives:
        ev.notes.append("`a/b unit` read as a range of alternatives")
    return first


def _tolerance(primary: Fraction) -> float:
    """Relative agreement expected between a value and its alternate: 1%, or the rounding of the primary if that is coarser."""
    if primary == 0:
        return 0.01
    exponent = (Decimal(primary.numerator) / Decimal(primary.denominator)).normalize().as_tuple().exponent
    decimals = max(0, -int(exponent))
    return max(0.01, 0.5 * 10 ** -decimals / float(abs(primary)))


def _eval_measure(c: Concept, info: ValueInfo, ev: _Eval, allow_text: bool = True) -> _Eval:
    measurement = _measurement_for(c, info, ev)
    if measurement is None and allow_text and info.kind == "text":
        measurement = _text_measurement(c, info, ev)
        if measurement is not None and measurement.via == "embedded":
            ev.method = "embedded_measure"
            ev.penalty += 0.08
    if measurement is None and info.kind == "text" and c.value_re is not None:
        match = c.value_re.search(fold(info.text))
        if match:
            token = match.group("n")
            if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", token):      # 62.000 / 62,000: a count with a thousands separator
                token = re.sub(r"[.,]", "", token)
            number = _fraction(token)
            measurement = Measurement(number, number, "", "value_re")
            ev.method = "value_pattern"
            ev.penalty += 0.05
    if measurement is None:
        ev.ok, ev.cap = False, SHAPE_CAP
        ev.notes.append(f"value {info.text[:40]!r} is not a {c.family or 'plain'} measure")
        return ev
    if not c.family:                        # plain number / range (pH, brush movements)
        if measurement.unit and measurement.via == "stage35":
            ev.ok, ev.cap = False, SHAPE_CAP
            ev.notes.append(f"unexpected unit {measurement.unit}")
            return ev
        ev.value, rounded = _number_text(measurement.lo, measurement.hi)
        ev.unit, ev.unit_status = c.unit, "none" if not c.unit else "explicit"
        ev.lo, ev.hi = float(measurement.lo), float(measurement.hi)
        return ev
    unit, unit_status = measurement.unit, "explicit"
    if unit and c.family not in families_of(unit):
        ev.reject = f"unit {unit} is not a {c.family} unit"
        return ev
    if not unit:
        inferred, why = _infer_unit(c, (measurement.lo, measurement.hi))
        if inferred is None:
            ev.ok, ev.cap = False, SHAPE_CAP
            ev.notes.append(why)
            return ev
        unit, unit_status = inferred, "implied"
        ev.cap = IMPLIED_CAP
        ev.notes.append(f"unit not written; {unit} is the only plausible unit")
    canonical = FAMILIES[c.family][0]
    low, high = to_canonical(c.family, unit, measurement.lo), to_canonical(c.family, unit, measurement.hi)
    assert low is not None and high is not None
    conversion: dict[str, object] | None = None
    method = ""
    if unit != canonical:
        rounded_any = False
        alt_used = ""
        # a source alternate in a metric unit ("3.32 in (84.3 mm)") beats arithmetic on an imperial unit
        for alt in measurement.alternates:
            parsed = parse_measure(alt.split(";")[0].strip())
            alt_unit = unit_token(parsed.unit) if parsed and parsed.unit else ""
            if parsed and alt_unit and c.family in families_of(alt_unit) and alt_unit in _METRIC and unit not in _METRIC and parsed.kind == "number":
                alt_value = to_canonical(c.family, alt_unit, _fraction(parsed.value))
                if alt_value is not None and high != 0 and abs(float(alt_value) / float(high) - 1) <= _tolerance(measurement.lo) and measurement.lo == measurement.hi:
                    low = high = alt_value
                    alt_used, method = alt, "source_alternate"
                    break
        else:
            method = "unit_conversion"
        text_value, rounded_any = _number_text(low, high)
        conversion = {
            "original_value": info.text, "original_number": _number_text(measurement.lo, measurement.hi)[0], "original_unit": unit,
            "target_unit": canonical, "method": method, "rounded": rounded_any, "alternates": list(measurement.alternates),
        }
        if alt_used:
            conversion["used_alternate"] = alt_used
        if unit_status != "implied":
            unit_status = "converted"
        ev.notes.append(f"converted {conversion['original_number']} {unit} -> {text_value} {canonical}")
    ev.value, _ = _number_text(low, high)
    ev.unit, ev.unit_status, ev.conversion = canonical, unit_status, conversion
    ev.lo, ev.hi = float(low), float(high)
    if measurement.alternates and conversion is None:
        ev.conversion = {"original_value": info.text, "alternates": list(measurement.alternates), "method": "as_written"}
    return ev


def _eval_dimensions(c: Concept, info: ValueInfo, ev: _Eval) -> _Eval:
    if info.kind != "dimensions" or not info.parts:
        ev.ok, ev.cap = False, SHAPE_CAP
        ev.notes.append("single value where several axes are expected" if info.kind == "measure" else f"value {info.text[:40]!r} is not a set of dimensions")
        return ev
    unit = info.unit
    status = "explicit"
    if unit and c.family not in families_of(unit):
        ev.reject = f"unit {unit} is not a {c.family} unit"
        return ev
    if not unit:
        inferred, why = _infer_unit(c, info.parts)
        if inferred is None:
            ev.ok, ev.cap = False, SHAPE_CAP
            ev.notes.append(why)
            return ev
        unit, status, ev.cap = inferred, "implied", IMPLIED_CAP
        ev.notes.append(f"unit not written; {unit} is the only plausible unit")
    canonical = FAMILIES[c.family][0]
    converted = tuple(to_canonical(c.family, unit, p) for p in info.parts)
    assert all(p is not None for p in converted)
    ev.parts = converted            # type: ignore[assignment]
    texts = [format_number(p)[0] for p in converted]     # type: ignore[arg-type]
    ev.value = " × ".join(texts)
    ev.unit = canonical
    if unit != canonical:
        status = "converted" if status != "implied" else status
        ev.conversion = {"original_value": info.text, "original_unit": unit, "target_unit": canonical, "method": "unit_conversion"}
    ev.unit_status = status
    ev.lo, ev.hi = float(min(converted)), float(max(converted))  # type: ignore[type-var,arg-type]
    return ev


def _evaluate(c: Concept, attribute: NormalizedAttribute, info: ValueInfo) -> _Eval:
    ev = _Eval()
    if c.value_gate is not None and not c.value_gate.search(fold(info.text)):
        ev.reject = "value does not look like the expected item"
        return ev
    if c.vtype == "boolean":
        if info.kind == "boolean":
            ev.value = "true" if info.boolean else "false"
        elif info.kind == "text" and info.lead_boolean is not None:
            ev.value = "true" if info.lead_boolean else "false"
            ev.penalty += 0.05
            ev.notes.append("boolean read from the leading word; the rest is kept in the evidence")
        else:
            ev.ok, ev.cap = False, SHAPE_CAP
            ev.notes.append(f"value {info.text[:40]!r} is not yes/no")
        return ev
    if c.vtype == "text":
        if info.kind == "boolean":
            ev.ok, ev.cap = False, SHAPE_CAP
            ev.notes.append("yes/no value for a descriptive field")
        elif len(info.text) > 160 and is_prose(info.text):
            ev.ok, ev.cap = False, SHAPE_CAP
            ev.notes.append("value is running prose, not a field value")
        else:
            ev.value = info.text
        return ev
    if c.vtype == "count":
        m = info.measure
        if info.kind == "measure" and m is not None and not m.unit and m.lo == m.hi and m.lo.denominator == 1:
            ev.value, ev.lo, ev.hi = format_number(m.lo)[0], float(m.lo), float(m.lo)
        elif info.kind == "text" and info.lead_count is not None and (c.prefix or info.text.strip().isdigit()):
            ev.value, ev.lo, ev.hi = str(info.lead_count), float(info.lead_count), float(info.lead_count)
            ev.penalty += 0.05
            ev.notes.append("count read from the leading number")
        else:
            ev.ok, ev.cap = False, SHAPE_CAP
            ev.notes.append(f"value {info.text[:40]!r} is not a count")
        return ev
    if c.vtype == "dimensions":
        return _eval_dimensions(c, info, ev)
    return _eval_measure(c, info, ev)


@dataclass(slots=True)
class _Hit:
    concept: Concept
    score: float
    match: str
    via: str              # label | generic | weak | group_only | alias
    label: str
    group_matched: bool
    ev: _Eval | None = None
    reasons: list[str] = field(default_factory=list)


def _applies(c: Concept, category: str) -> bool:
    return (c.categories is None or category in c.categories) and category not in c.exclude_categories


def _label_hit(c: Concept, label: str, group: str) -> tuple[float, str, str, bool] | None:
    if c.exclude is not None and c.exclude.search(label):
        return None
    group_matched = bool(c.groups) and any(g.search(group) for g in c.groups)
    if c.need_group and not group_matched:
        return None
    if c.group_only:
        return (GROUP_ONLY, "group", "group_only", True) if group_matched else None
    for score, via, patterns in ((STRONG, "label", c.labels), (GENERIC, "generic", c.generic), (WEAK, "weak", c.weak)):
        if via == "generic" and c.categories is None:
            continue
        for pattern in patterns:
            match = pattern.search(label)
            if match:
                return score, match.group(0) or label, via, group_matched
    return None


def _label_only_keys(label: str, group: str, category: str) -> set[str]:
    keys = set()
    for c in CONCEPTS:
        if _applies(c, category) and not c.group_only and _label_hit(c, label, group) is not None:
            keys.add(c.key)
    return keys


def _signal(kind: str, detail: str, weight: float = 0.0) -> dict[str, object]:
    return {"kind": kind, "detail": detail, "weight": round(weight, 3)}


def _source_records(attribute: NormalizedAttribute) -> list[dict[str, object]]:
    return [{"kind": "source_record", **e.to_dict()} for e in attribute.evidence]


def _axis_order(attribute: NormalizedAttribute) -> tuple[str, str, str] | None:
    texts = [attribute.label, *attribute.label_aliases, *(e.raw_label for e in attribute.evidence)]
    for text in texts:
        match = _AXIS_ORDER.search(fold(text))
        if match:
            keys = tuple(_AXES.get(g, "") for g in match.groups())
            if all(keys) and len(set(keys)) == 3:
                return keys  # type: ignore[return-value]
    return None


def _round(x: float) -> float:
    return round(min(x, 0.99), 3)


def map_attribute(attribute: NormalizedAttribute, category: CategoryResult) -> list[MappedField]:
    cat = category.category_id
    # long "sections" are page titles / captions, not section headers: they are no context signal
    group = fold_label(" > ".join(p for p in attribute.group_path if len(p.split()) <= 6))
    info = analyse_value(attribute)
    primary = fold_label(attribute.label)
    aliases = [fold_label(a) for a in attribute.label_aliases if fold_label(a) != primary]

    def collect(labels: list[str], alias_pass: bool) -> list[_Hit]:
        hits: list[_Hit] = []
        for concept_ in CONCEPTS:
            if not _applies(concept_, cat):
                continue
            best = None
            for label in labels:
                hit = _label_hit(concept_, label, group)
                if hit and (best is None or hit[0] > best[0][0]):
                    best = (hit, label)
            if best is None:
                continue
            (score, matched, via, group_matched), label = best
            hits.append(_Hit(concept_, score - (0.05 if alias_pass else 0), matched, "alias" if alias_pass else via, label, group_matched))
        return hits

    hits = collect([primary], False)
    if not hits and aliases:
        hits = collect(aliases, True)
    unit_rejects: list[str] = []
    live: list[_Hit] = []
    for hit in hits:
        c = hit.concept
        hit.ev = _evaluate(c, attribute, info)
        if hit.ev.reject:
            unit_rejects.append(f"{c.key}: {hit.ev.reject}")
            continue
        if c.test is not None and hit.ev.ok and hit.ev.lo is not None and not c.test(hit.ev.lo, hit.ev.hi if hit.ev.hi is not None else hit.ev.lo):
            hit.ev.ok, hit.ev.cap = False, SHAPE_CAP
            hit.ev.notes.append(c.test_reason)
        live.append(hit)

    if not live:
        reasons = unit_rejects or (["label matches no concept of this category"] if not hits else [])
        if _GENERIC_LABELS.search(primary):
            reasons = ["label too generic to identify a field"]
        return [_row(attribute, category, UNMAPPED, reasons=reasons, info=info)]

    # alias votes: the same concept in another language corroborates, a different concept disagrees
    scored: dict[str, tuple[float, _Hit, list[str]]] = {}
    for hit in live:
        score = hit.score
        reasons = list(hit.ev.notes)  # type: ignore[union-attr]
        ev = hit.ev
        assert ev is not None
        if hit.via != "alias" and hit.concept.groups and hit.group_matched and not hit.concept.group_only:
            score += 0.05
        if hit.concept.categories is not None and cat in hit.concept.categories:
            score += 0.03
        if ev.unit_status in {"explicit", "converted"} and hit.concept.family:
            score += 0.03
        corroborating = disagreeing = 0
        for alias in aliases:
            keys = _label_only_keys(alias, group, cat)
            if hit.concept.key in keys:
                corroborating += 1
            elif keys:
                disagreeing += 1
                reasons.append(f"alias {alias!r} points to {'|'.join(sorted(keys))}")
        score += min(0.04, 0.02 * corroborating)
        if disagreeing:
            score -= 0.10
        score -= ev.penalty
        if ev.cap is not None:
            score = min(score, ev.cap)
        if not ev.ok:
            score = min(score, SHAPE_CAP)
        score = _round(score)
        current = scored.get(hit.concept.key)
        if current is None or score > current[0]:
            scored[hit.concept.key] = (score, hit, reasons + ([f"aliases: {corroborating} agree, {disagreeing} disagree"] if aliases else []))

    ranked = sorted(scored.values(), key=lambda item: item[0], reverse=True)
    top_score, top_hit, top_reasons = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    if top_score >= MAP_MIN and top_hit.ev.ok and (len(ranked) == 1 or top_score - second >= MARGIN):  # type: ignore[union-attr]
        return _mapped_rows(attribute, category, info, top_hit, top_score, top_reasons, aliases, group)
    candidates = tuple(
        Candidate(h.concept.key, s, tuple(dict.fromkeys(r + ([f"label matched by {h.via}"] if h.via in {"weak", "generic"} else []))))
        for s, h, r in ranked if s >= CANDIDATE_MIN
    )
    if not candidates:
        return [_row(attribute, category, UNMAPPED, reasons=["all candidates below the candidate threshold"], info=info)]
    why = []
    if len(ranked) > 1 and top_score >= MAP_MIN and top_score - second < MARGIN:
        why.append("several keys with close confidence")
    if top_score < MAP_MIN:
        why.append(f"best confidence {top_score} < {MAP_MIN}")
    return [_row(attribute, category, AMBIGUOUS, candidates=candidates, reasons=why, info=info)]


def _row(attribute: NormalizedAttribute, category: CategoryResult, status: str, *, candidates: tuple[Candidate, ...] = (),
         reasons: Iterable[str] = (), info: ValueInfo | None = None) -> MappedField:
    return MappedField(attribute, status, category.category_id, candidates=candidates, reasons=tuple(reasons),
                       evidence=tuple(_source_records(attribute)))


def _mapped_rows(attribute: NormalizedAttribute, category: CategoryResult, info: ValueInfo, hit: _Hit, score: float,
                 reasons: list[str], aliases: list[str], group: str) -> list[MappedField]:
    c, ev = hit.concept, hit.ev
    assert ev is not None
    method = ev.method or {"label": "label_pattern", "generic": "generic_label_by_category", "weak": "weak_label_pattern",
                           "group_only": "group_context", "alias": "alias_label"}[hit.via]
    if hit.group_matched and hit.via != "group_only":
        method = "label_pattern+group_context" if method == "label_pattern" else method + "+group_context"
    if c.test is not None:
        method += "+value_test"
    signals = [_signal("label", f"{hit.label!r} ~ {hit.match!r}", hit.score)]
    if hit.group_matched:
        signals.append(_signal("group_path", group, 0.05))
    if c.categories is not None:
        signals.append(_signal("category", f"{category.category_id} ({category.confidence})", 0.03))
    if ev.unit_status:
        signals.append(_signal("unit", f"{ev.unit_status}: {ev.unit or 'none'}", 0.03 if ev.unit_status in {"explicit", "converted"} else 0))
    for note in reasons:
        signals.append(_signal("note", note))
    if ev.conversion:
        signals.append(_signal("conversion", str(ev.conversion.get("method", "")) or "as_written"))
    base = dict(
        category=category.category_id, status=MAPPED, confidence=score, unit_status=ev.unit_status,
        evidence=tuple(signals) + tuple(_source_records(attribute)), reasons=tuple(reasons), conversion=ev.conversion,
    )
    rows = [MappedField(attribute, canonical_key=c.key, canonical_label=c.label, normalized_value=ev.value, unit=ev.unit,
                        value_type=c.vtype if c.vtype != "measure" else ("range" if ev.lo != ev.hi else "number"),
                        value_min=ev.lo, value_max=ev.hi, mapping_method=method, **base)]  # type: ignore[arg-type]
    if c.axis_split and ev.parts:
        order = _axis_order(attribute)
        if order:
            for key, part in zip(order, ev.parts):
                target = next(x for x in CONCEPTS if x.key == key)
                text, _ = format_number(part)
                rows.append(MappedField(
                    attribute, canonical_key=key, canonical_label=target.label, normalized_value=text, unit=ev.unit, value_type="number",
                    value_min=float(part), value_max=float(part), mapping_method="dimension_split", derived_from=c.key,
                    **{**base, "confidence": _round(score - 0.02),
                       "evidence": (_signal("dimension_order", f"label declares axis order {'/'.join(k[:-3] for k in order)}"),) + base["evidence"]},  # type: ignore[operator]
                ))
        else:
            rows[0] = MappedField(**{**{k: getattr(rows[0], k) for k in rows[0].__dataclass_fields__},
                                     "context": {"axis_order": "unknown; composite kept, not split"}})
    for companion in _COMPANIONS.get(c.key, ()):
        embedded = [m for m in _embedded(info.text) if companion.family in families_of(m.unit)] if info.kind == "text" else []
        if len(embedded) != 1:
            continue
        sub = _evaluate(companion, attribute, ValueInfo("measure", info.text, measure=embedded[0], unit=embedded[0].unit))
        if sub.reject or not sub.ok:
            continue
        rows.append(MappedField(
            attribute, canonical_key=companion.key, canonical_label=companion.label, normalized_value=sub.value, unit=sub.unit,
            value_type="range" if sub.lo != sub.hi else "number", value_min=sub.lo, value_max=sub.hi,
            mapping_method="compound_value_split", derived_from=c.key,
            **{**base, "confidence": _round(score - 0.10), "unit_status": sub.unit_status, "conversion": sub.conversion,
               "evidence": (_signal("compound_value", f"{companion.key} found inside the value of {c.key}"),) + base["evidence"]},  # type: ignore[operator]
        ))
    return rows


def _component_pass(fields: list[MappedField]) -> list[MappedField]:
    """A repeated measurement block in one section (mouse + receiver) must not overwrite the main product's fields."""
    component_keys = {c.key for c in CONCEPTS if c.component}
    groups: dict[tuple[str, str, str, str], list[int]] = defaultdict(list)
    for index, f in enumerate(fields):
        if f.status == MAPPED and f.canonical_key in component_keys and not f.derived_from:
            first = f.attribute.evidence[0]
            groups[(first.source_url, first.section, first.method, f.canonical_key)].append(index)
    out = list(fields)
    for members in groups.values():
        members.sort(key=lambda i: fields[i].attribute.evidence[0].raw_index)
        for order, index in enumerate(members[1:], start=2):
            f = fields[index]
            data = {k: getattr(f, k) for k in f.__dataclass_fields__}
            data.update(
                canonical_key=f"secondary_component_{f.canonical_key}", canonical_label=f"Secondary component {f.canonical_label.lower()}",
                mapping_method="repeated_block_context", confidence=_round(f.confidence - 0.10),
                context={**f.context, "component_block": order, "primary_key": f.canonical_key},
                evidence=(_signal("repeated_block", f"{f.canonical_key} repeated in the same section (occurrence {order}); main product keeps the first"),) + f.evidence,
            )
            out[index] = MappedField(**data)
    return out


# ---------------------------------------------------------------------------
# Collisions (kept, never resolved)
# ---------------------------------------------------------------------------

def _member(f: MappedField) -> dict[str, object]:
    first = f.attribute.evidence[0]
    return {
        "attribute_id": f.attribute.id, "label": f.attribute.qualified_label, "normalized_value": f.normalized_value, "unit": f.unit,
        "original": (f.conversion or {}).get("original_value") or f.attribute.display or f.attribute.value,
        "source_url": first.source_url, "location": first.location, "confidence": f.confidence,
    }


def detect_collisions(fields: Iterable[MappedField]) -> tuple[Collision, ...]:
    by_key: dict[str, list[MappedField]] = defaultdict(list)
    for f in fields:
        if f.status == MAPPED:
            by_key[f.canonical_key].append(f)
    out: list[Collision] = []
    for key, members in sorted(by_key.items()):
        distinct = {m.attribute.id for m in members}
        if len(distinct) < 2:
            continue
        rows = tuple(_member(m) for m in members)
        if CARDINALITY.get(key) == "list":
            out.append(Collision(key, "list_field", rows))
            continue
        values = {(m.normalized_value.casefold(), m.unit) for m in members}
        if len(values) == 1:
            originals = {(m.attribute.display or m.attribute.value).casefold() for m in members}
            kind = "same_value" if len(originals) == 1 else "equivalent_after_conversion" if any(m.conversion for m in members) else "same_value"
        else:
            kind = "different_values"
        out.append(Collision(key, kind, rows))
    return tuple(out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _AttributeText:
    name: str
    value: str
    evidence: str


def detect_product_category(result: NormalizationResult) -> CategoryResult:
    attributes = [
        _AttributeText(" ".join([a.label, *a.label_aliases]), a.value, " ".join(a.group_path))
        for a in result.attributes if a.cls == SPEC
    ]
    return detect_category(attributes=attributes, product_texts=[result.product_name])


def map_result(result: NormalizationResult, category: CategoryResult | None = None) -> CanonicalMappingResult:
    category = category or detect_product_category(result)
    fields: list[MappedField] = []
    other = 0
    for attribute in result.attributes:
        if attribute.cls != SPEC:
            other += 1
            continue
        rows = map_attribute(attribute, category)
        if len(rows) == 1 and rows[0].status == UNMAPPED:
            reason = not_a_spec_reason(attribute)
            if reason:
                rows = [MappedField(attribute, NOT_A_SPEC, category.category_id, reasons=(reason,), evidence=tuple(_source_records(attribute)))]
        fields.extend(rows)
    fields = _component_pass(fields)
    return CanonicalMappingResult(result.product_name, result.brand, result.model, category, tuple(fields), detect_collisions(fields), other)
