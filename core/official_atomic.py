"""Stage 32: atomic decomposition of official spec sections.

A parent section ("Display", "Battery and Charging", "5G", ...) is a container,
not one attribute. This module decomposes the lines of one section (of one
model column, see ``core.official_spec_table``) into atomic canonical facts:
one cell = one characteristic.

Every input line ends in the *ledger* with an explicit outcome -- ``accepted``
(produced facts), ``merged`` (redundant restatement of an accepted fact), a
``heading`` marker, or ``rejected`` with a concrete reason -- so nothing is
dropped silently.

Readers work by the meaning of the section heading and by the *shape* of a
line, never by a page-specific selector or a product value. Canonical names
are language-neutral identifiers (see ``core.attribute_catalog``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Callable, Iterable

from core.attribute_catalog import label_for, lookup_canonical, resolve_canonical


@dataclass(frozen=True, slots=True)
class Line:
    text: str
    bold: bool
    index: int


@dataclass(frozen=True, slots=True)
class Fact:
    canonical: str
    label: str
    value: str
    unit: str | None
    snippet: str
    boolean: bool = False
    section: str = ""


# ---------------------------------------------------------------------------
# Section meaning
# ---------------------------------------------------------------------------

_ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("front_camera", re.compile(r"\b(?:front|selfie)\b.*\bcamera|\bcamera\b.*\b(?:front|selfie)\b", re.I)),
    ("rear_camera", re.compile(r"\b(?:rear|back|main)\b.*\bcamera|\bcamera\b.*\b(?:rear|back)\b", re.I)),
    ("display", re.compile(r"\b(?:display|screen)\b", re.I)),
    ("body", re.compile(r"\b(?:dimensions?|weight|size)\b", re.I)),
    ("battery", re.compile(r"\b(?:battery|charging)\b", re.I)),
    ("memory", re.compile(r"\b(?:memory|storage|ram)\b", re.I)),
    ("processor", re.compile(r"\b(?:processors?|chip(?:set)?|cpu|soc)\b", re.I)),
    ("durability", re.compile(r"\b(?:materials?|durability|build|protection|body|design)\b", re.I)),
    ("os", re.compile(r"^(?:operating system|os|software)$", re.I)),
    ("updates", re.compile(r"\bupdates?\b", re.I)),
    ("video", re.compile(r"\bvideo\b", re.I)),
    ("media", re.compile(r"\b(?:media|audio|sound)\b", re.I)),
    ("ports", re.compile(r"\b(?:ports?|connectors?|interfaces?|buttons?)\b", re.I)),
    ("wireless", re.compile(r"\b(?:wireless|connectivity|location|wi-?fi|bluetooth)\b", re.I)),
    ("network", re.compile(r"\b(?:network|cellular|5g|4g|lte|sims?)\b", re.I)),
    ("accessibility", re.compile(r"\baccessibility\b", re.I)),
    ("colors", re.compile(r"^(?:colou?rs?|finish(?:es)?)$", re.I)),
    ("box", re.compile(r"\b(?:in the box|box contents|package contents|what'?s included)\b", re.I)),
)


def section_roles(heading: str) -> set[str]:
    """Meaning(s) of a section heading; a heading may carry several."""
    text = re.sub(r"\s+", " ", heading or "").strip()
    roles = {role for role, pattern in _ROLE_PATTERNS if pattern.search(text)}
    if "front_camera" in roles:
        roles.discard("rear_camera")
    if "updates" in roles:  # "Security and OS updates" is update policy, not the OS
        roles.discard("os")
    return roles


# Groups whose leftover short lines are one structured *list* attribute.
# key: (heading words, sub-heading words) regex -> (canonical, label)
_LIST_TARGETS: tuple[tuple[re.Pattern[str], re.Pattern[str] | None, str], ...] = (
    (re.compile(r"^rear camera$", re.I), None, "rear_camera_features"),
    (re.compile(r"^front camera$", re.I), None, "front_camera_features"),
    (re.compile(r"^camera features$", re.I), re.compile(r"^editing", re.I), "editing_features"),
    (re.compile(r"^camera features$", re.I), None, "camera_features"),
    (re.compile(r"^video$", re.I), re.compile(r"rear", re.I), "rear_video_features"),
    (re.compile(r"^video$", re.I), re.compile(r"front", re.I), "front_video_features"),
    (re.compile(r"^video$", re.I), re.compile(r"audio", re.I), "video_audio_features"),
    (re.compile(r"^video$", re.I), None, "video_features"),
    (re.compile(r"\b(?:materials?|durability)\b", re.I), None, "materials_and_durability"),
    (re.compile(r"^security$", re.I), None, "security_features"),
    (re.compile(r"^authentication$", re.I), None, "authentication"),
    (re.compile(r"^safety$", re.I), None, "safety_features"),
    (re.compile(r"^sensors?$", re.I), None, "sensors"),
    (re.compile(r"^media$|\baudio\b", re.I), None, "media_features"),
    (re.compile(r"^accessibility$", re.I), None, "accessibility_features"),
    (re.compile(r"\bwireless\b|\bconnectivity\b", re.I), None, "wireless_features"),
    (re.compile(r"\bbattery\b", re.I), None, "battery_features"),
    (re.compile(r"\b(?:in the box|box contents|package contents)\b", re.I), None, "package_contents"),
    (re.compile(r"^colou?rs?$", re.I), None, "color"),
)

MAX_LIST_CHARS = 600
MAX_LIST_ITEM_CHARS = 140
MAX_VALUE_CHARS = 300

_LINK_RE = re.compile(r"^(?:https?://|www\.|[a-z0-9-]+\.[a-z]{2,}/)\S*$", re.I)
_POINTER_RE = re.compile(r"^(?:learn more(?: at)?|see|and|or|more|details|read more|click here|[.,;:()])$", re.I)


# ---------------------------------------------------------------------------
# Line handling helpers
# ---------------------------------------------------------------------------


def is_heading(line: Line) -> bool:
    """A bold line is a sub-heading only when it is a short, number-free label
    ("Camera Features", "Audio"); a bold sentence is ordinary content."""
    return line.bold and len(line.text.split()) <= 5 and not re.search(r"\d", line.text)


def merge_continuations(lines: list[Line]) -> list[Line]:
    """Rejoin values a page wrapped over several text nodes.

    ``: Bands n1/2/3`` after a label, and ``/66/71`` or ``, x`` after a list
    continue the previous line; nothing else is ever merged.
    """
    merged: list[Line] = []
    for line in lines:
        text = line.text
        if merged and not is_heading(line) and text[:1] in {":", "/", ","} and not is_heading(merged[-1]):
            joiner = " " if text[:1] == ":" else ""
            previous = merged[-1]
            merged[-1] = Line(f"{previous.text}{joiner}{text}".strip(), previous.bold, previous.index)
            continue
        merged.append(line)
    return merged


def expand_bands(text: str) -> str:
    """``B1/2/3/66`` -> ``B1, B2, B3, B66``; ``1,2,4`` -> ``1, 2, 4``."""
    tokens = [t.strip() for t in re.split(r"[/,]", text) if t.strip()]
    prefix = ""
    out: list[str] = []
    for token in tokens:
        match = re.match(r"^([A-Za-z]+)(\d.*)$", token)
        if match:
            prefix, number = match.group(1), match.group(2)
            out.append(f"{prefix}{number}")
        elif prefix and re.fullmatch(r"\d+", token):
            out.append(f"{prefix}{token}")
        else:
            out.append(token)
    return ", ".join(out)


class _Group:
    def __init__(self, heading: str, sub: str | None, lines: list[Line]):
        self.heading = heading
        self.sub = sub
        self.lines = lines
        self.facts: list[Fact] = []
        self.used: dict[int, set[str]] = {}
        self.notes: dict[int, str] = {}

    def emit(self, line: Line, canonical: str, label: str, value: str,
             unit: str | None = None, *, boolean: bool = False) -> None:
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            return
        self.facts.append(Fact(canonical, label, value, unit, line.text[:140], boolean, self.heading))
        self.used.setdefault(line.index, set()).add(canonical)

    def merge(self, line: Line, reason: str) -> None:
        self.notes.setdefault(line.index, reason)

    def find(self, pattern: re.Pattern[str]) -> tuple[Line, re.Match[str]] | None:
        for line in self.lines:
            match = pattern.search(line.text)
            if match:
                return line, match
        return None

    def find_all(self, pattern: re.Pattern[str]) -> list[tuple[Line, re.Match[str]]]:
        return [(line, m) for line in self.lines if (m := pattern.search(line.text))]

    def unresolved(self) -> list[Line]:
        return [line for line in self.lines if line.index not in self.used and line.index not in self.notes]


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

_SIZE = re.compile(r"(?P<n>\d{1,2}(?:[.,]\d{1,2})?)[\s-]*(?:inch(?:es)?\b|in\b|\"|″|”)", re.I)
_RES = re.compile(r"(?P<w>\d{3,5})\s*[x×]\s*(?P<h>\d{3,5})(?P<rest>[^\n]*)", re.I)
_PANEL = re.compile(
    r"\b(?:LTPO\s+)?(?:Super\s+|Dynamic\s+)?(?:AMOLED|OLED|IPS\s+LCD|LCD|TFT)(?:\s*2X)?\b", re.I,
)
_PPI = re.compile(r"(?P<n>\d{2,4})\s*PPI\b", re.I)
_ASPECT = re.compile(r"(?P<r>\d{1,2}(?:\.\d+)?\s*:\s*\d{1,2}(?:\.\d+)?)\s+aspect ratio", re.I)
_HZ = re.compile(r"(?:(?P<lo>\d{1,3})\s*[-–]\s*)?(?P<hi>\d{2,3})\s?Hz\b", re.I)
_GLASS = re.compile(r"^(?:scratch[- ]resistant\s+)?(?P<g>.+?)\s+cover glass\b", re.I)
_NITS = re.compile(r"(?:up to\s+)?(?P<n>\d[\d,]*)\s*nits?(?:\s*\((?P<kind>[^)]+)\))?", re.I)
_CONTRAST = re.compile(r"(?P<r>[>≥<~]?\s*\d[\d,]*\s*:\s*1)\s+contrast", re.I)
_DEPTH = re.compile(r"(?P<bit>\d{1,2})[- ]bit(?: depth)?(?:\s+for\s+(?P<c>[\d.,]+\s*(?:million|billion|thousand))\s+colou?rs)?", re.I)
_SUPPORT = re.compile(r"^(?P<what>[A-Za-z][\w+\-/ ]{1,28}?)\s+support$", re.I)


def _display(g: _Group) -> None:
    if hit := g.find(_SIZE):
        line, m = hit
        g.emit(line, "display_size", "Display size", m.group("n").replace(",", "."), "in")
    if hit := g.find(_RES):
        line, m = hit
        g.emit(line, "display_resolution", "Display resolution", f"{m.group('w')} x {m.group('h')}", "pixels")
        rest = m.group("rest") or ""
        panel = _PANEL.search(rest)
        if panel:
            g.emit(line, "display_type", "Display type", panel.group(0))
        if ppi := _PPI.search(rest):
            g.emit(line, "display_ppi", "Pixel density", ppi.group("n"), "ppi")
    elif hit := g.find(_PANEL):
        g.emit(hit[0], "display_type", "Display type", hit[1].group(0))
    if hit := g.find(_ASPECT):
        g.emit(hit[0], "display_aspect_ratio", "Display aspect ratio", re.sub(r"\s+", "", hit[1].group("r")))
    rates = g.find_all(_HZ)
    if rates:
        line, m = max(rates, key=lambda item: int(item[1].group("hi")))
        value = f"{m.group('lo')}-{m.group('hi')}" if m.group("lo") else m.group("hi")
        g.emit(line, "refresh_rate", "Refresh rate", value, "Hz")
    if hit := g.find(_GLASS):
        g.emit(hit[0], "cover_glass", "Cover glass", hit[1].group("g"))
    for line in g.lines:
        for m in _NITS.finditer(line.text):
            kind = (m.group("kind") or "").casefold()
            canonical = "hdr_brightness" if "hdr" in kind else "peak_brightness"
            label = "HDR brightness" if canonical == "hdr_brightness" else "Peak brightness"
            g.emit(line, canonical, label, m.group("n").replace(",", ""), "nits")
    if hit := g.find(_CONTRAST):
        g.emit(hit[0], "contrast_ratio", "Contrast ratio", re.sub(r"\s+", "", hit[1].group("r")))
    if hit := g.find(_DEPTH):
        line, m = hit
        g.emit(line, "color_depth", "Color depth", f"{m.group('bit')}-bit")
        if m.group("c"):
            g.emit(line, "number_of_colors", "Number of colors", m.group("c"))
    for line in g.lines:
        support = _SUPPORT.match(line.text)
        if support and line.index not in g.used:
            g.emit(line, resolve_canonical(f"{support.group('what')} support"),
                   f"{support.group('what')} support", "Yes", boolean=True)
    for line in g.lines:
        if line.index not in g.used and re.fullmatch(r"\([^()]{2,24}\)", line.text):
            g.merge(line, "parenthetical qualifier of the preceding display value")


_DIM3 = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)\s*(?P<la>height|width|depth)?\s*[x×]\s*"
    r"(?P<b>\d+(?:\.\d+)?)\s*(?P<lb>height|width|depth)?\s*[x×]\s*"
    r"(?P<c>\d+(?:\.\d+)?)\s*(?P<lc>height|width|depth)?\s*\(?(?P<unit>mm|cm|in(?:ches)?)\b",
    re.I,
)
_WEIGHT = re.compile(r"(?P<n>\d+(?:[.,]\d+)?)\s*(?P<unit>kg|g|oz|lbs?)\b", re.I)


def _body(g: _Group) -> None:
    hits = g.find_all(_DIM3)
    metric = next((h for h in hits if h[1].group("unit").casefold() in {"mm", "cm"}), None)
    chosen = metric or (hits[0] if hits else None)
    if chosen:
        line, m = chosen
        if all((m.group("la"), m.group("lb"), m.group("lc"))):
            order = {m.group("la").casefold(): "a", m.group("lb").casefold(): "b", m.group("lc").casefold(): "c"}
        else:
            order = {"height": "a", "width": "b", "depth": "c"}
        unit = "in" if m.group("unit").casefold().startswith("in") else m.group("unit").casefold()
        for component in ("height", "width", "depth"):
            g.facts.append(Fact(f"product_{component}", f"Product {component}", m.group(order[component]),
                                unit, m.group(0)[:80], False, g.heading))
        g.used.setdefault(line.index, set()).add("product_dimensions")
        for other, _ in hits:
            if other.index != line.index:
                g.merge(other, "same dimensions in another unit")
    weights = g.find_all(_WEIGHT)
    metric_w = next((w for w in weights if w[1].group("unit").casefold() in {"g", "kg"}), None)
    if metric_w:
        line, m = metric_w
        g.emit(line, "net_weight", "Net weight", m.group("n").replace(",", "."), m.group("unit").casefold())
        for other, _ in weights:
            if other.index != line.index:
                g.merge(other, "same weight in another unit")


_MAH = re.compile(r"(?P<n>\d[\d,]*(?:\.\d+)?)\s*mAh\b", re.I)
_WATT = re.compile(r"(?P<n>\d{2,3})\s?W\b")
_FAST = re.compile(r"fast charging\s*[-–—:]\s*(?P<v>.+)$", re.I)
_HOURS = re.compile(r"(?P<n>\d+\+?)[- ]hours?\s+battery life", re.I)


def _battery(g: _Group) -> None:
    typical = [h for h in g.find_all(_MAH) if re.search(r"typical|rated", h[0].text, re.I)]
    plain = [h for h in g.find_all(_MAH) if not re.match(r"\s*(?:minimum|min\.)", h[0].text, re.I)]
    chosen = (typical or plain or [None])[0]
    if chosen:
        line, m = chosen
        g.emit(line, "battery_capacity", "Battery capacity", m.group("n").replace(",", ""), "mAh")
        if minimum := re.search(r"(?:minimum|min\.)\s*(?P<n>\d[\d,]*)\s*mAh", line.text, re.I):
            g.emit(line, "battery_capacity_min", "Minimum battery capacity",
                   minimum.group("n").replace(",", ""), "mAh")
    for index, line in enumerate(g.lines):
        window = " ".join(item.text for item in g.lines[max(0, index - 1):index + 2])
        if "wireless" not in line.text.casefold() and re.search(r"charg", window, re.I) \
                and (m := _WATT.search(line.text)) and "charging_power" not in {f.canonical for f in g.facts}:
            g.emit(line, "charging_power", "Charging power", m.group("n"), "W")
    if hit := g.find(_FAST):
        g.emit(hit[0], "fast_charging", "Fast charging", hit[1].group("v"))
    if hit := g.find(re.compile(r"wireless charging", re.I)):
        line = hit[0]
        g.emit(line, "wireless_charging", "Wireless charging", "Yes", boolean=True)
        if std := re.search(r"\((?P<s>[A-Za-z0-9]+)[- ]certified\)", line.text):
            g.emit(line, "wireless_charging_standard", "Wireless charging standard", std.group("s"))
    for line, m in g.find_all(_HOURS):
        if " with " in line.text.casefold():
            g.emit(line, "battery_life_power_saving", "Battery life (power saving)", m.group("n"), "h")
        else:
            g.emit(line, "battery_life", "Battery life", m.group("n"), "h")


_RAM = re.compile(r"(?P<n>\d{1,3})\s*(?P<unit>GB|MB)\s+(?:of\s+)?(?:RAM|memory)\b", re.I)
_STORAGE_LINE = re.compile(r"^\d+\s?(?:GB|TB)(?:\s*(?:/|,|\||or)\s*\d+\s?(?:GB|TB))*$", re.I)


def _memory(g: _Group) -> None:
    if hit := g.find(_RAM):
        g.emit(hit[0], "ram", "RAM", hit[1].group("n"), hit[1].group("unit").upper())
    for line in g.lines:
        if _STORAGE_LINE.match(line.text):
            options = re.sub(r"\s*(?:/|,|\||\bor\b)\s*", " / ", line.text)
            options = re.sub(r"(\d)\s?(GB|TB)", lambda m: f"{m.group(1)} {m.group(2).upper()}", options, flags=re.I)
            g.emit(line, "storage", "Storage", options)
            break


_CHIP_SKIP = re.compile(r"co-?processor|security|modem|titan|neural|tpu|gpu\b", re.I)


def _processor(g: _Group) -> None:
    for line in g.lines:
        if (m := re.match(r"^(?P<chip>.+?)\s+security\s+(?:co)?processor", line.text, re.I)):
            g.emit(line, "security_coprocessor", "Security coprocessor", m.group("chip"))
            continue
        if _CHIP_SKIP.search(line.text) or len(line.text) > 60 or not re.search(r"[A-Za-z]", line.text):
            continue
        if "processor" not in {f.canonical for f in g.facts}:
            g.emit(line, "processor", "Processor", line.text)


_LENS = re.compile(
    r"(?P<mp>\d{1,3}(?:\.\d)?)\s?MP\s+(?:[\w-]+\s+){0,2}?"
    r"(?P<role>ultra-?wide|wide|telephoto|periscope|macro|main|depth)\b", re.I,
)
_LENS_START = re.compile(r"^" + _LENS.pattern, re.I)
_APERTURE = re.compile(r"[ƒf]/\s*(?P<v>\d(?:\.\d+)?)\s+aperture", re.I)
_FOV = re.compile(r"(?P<v>\d{2,3})\s*°\s*(?:[a-z-]+\s+)?field of view", re.I)
_SENSOR = re.compile(r"(?P<v>\d+/\d+(?:\.\d+)?\"?)\s*(?:image\s+)?sensor size", re.I)
_OPTICAL = re.compile(r"(?P<v>\d+(?:\.\d)?x)\s+optical zoom", re.I)
_ZOOM = re.compile(r"zoom up to\s+(?P<v>\d+x)", re.I)
_MP = re.compile(r"(?P<mp>\d{1,3}(?:\.\d)?)\s?MP\b", re.I)


def _role(text: str) -> str:
    return text.casefold().replace("ultra-wide", "ultrawide")


def _rear_camera(g: _Group) -> None:
    lenses: list[str] = []
    for line, _ in g.find_all(_LENS):
        for m in _LENS.finditer(line.text):
            entry = f"{m.group('mp')} MP {_role(m.group('role'))}"
            if entry not in lenses:
                lenses.append(entry)
        g.used.setdefault(line.index, set()).add("rear_camera")
    if lenses:
        g.facts.append(Fact("rear_camera", "Rear camera", " + ".join(lenses), None,
                            " + ".join(lenses)[:140], False, g.heading))
    current: str | None = None
    for line in g.lines:
        start = _LENS_START.match(line.text)
        if start:
            current = _role(start.group("role"))
            if re.search(r"with autofocus", line.text, re.I):
                g.emit(line, f"{current}_camera_autofocus", f"{current.capitalize()} camera autofocus",
                       "Yes", boolean=True)
            if pd := re.search(r"\b(Octa|Quad|Dual)\s+PD\b", line.text):
                g.emit(line, f"{current}_camera_phase_detection",
                       f"{current.capitalize()} camera phase detection", pd.group(0))
            continue
        if current is None:
            continue
        for pattern, part, label, unit in (
            (_APERTURE, "aperture", "aperture", None),
            (_FOV, "field_of_view", "field of view", "°"),
            (_SENSOR, "sensor_size", "sensor size", None),
            (_OPTICAL, "optical_zoom", "optical zoom", None),
        ):
            if m := pattern.search(line.text):
                value = ("ƒ/" + m.group("v")) if part == "aperture" else m.group("v")
                g.emit(line, f"{current}_camera_{part}", f"{current.capitalize()} camera {label}", value, unit)
    zooms = g.find_all(_ZOOM)
    if zooms:
        best = max(zooms, key=lambda item: int(item[1].group("v")[:-1]))
        g.emit(best[0], "max_zoom", "Max zoom", best[1].group("v"))


def _front_camera(g: _Group) -> None:
    if hit := g.find(_MP):
        g.emit(hit[0], "front_camera", "Front camera", f"{hit[1].group('mp')} MP")
        if re.search(r"with autofocus", hit[0].text, re.I):
            g.emit(hit[0], "front_camera_autofocus", "Front camera autofocus", "Yes", boolean=True)
    if hit := g.find(_APERTURE):
        g.emit(hit[0], "front_camera_aperture", "Front camera aperture", "ƒ/" + hit[1].group("v"))
    if hit := g.find(_FOV):
        g.emit(hit[0], "front_camera_field_of_view", "Front camera field of view", hit[1].group("v"), "°")


_VIDEO_MODE = re.compile(r"(?P<res>\d+[Kp])\s+video recording at\s+(?P<fps>[\d/]+)\s*FPS", re.I)
_SLOMO = re.compile(r"slo-?mo\b.*?up to\s+(?P<n>\d+)\s*FPS", re.I)
_HDR_VIDEO = re.compile(r"(?P<bit>\d+-bit)\s+HDR video", re.I)


def _video(g: _Group) -> None:
    sub = (g.sub or "").casefold()
    modes = [(line, m) for line, m in g.find_all(_VIDEO_MODE)]
    if modes and ("rear" in sub or "front" in sub or not sub):
        side = "front" if "front" in sub else "rear"
        value = "; ".join(f"{m.group('res')} at {m.group('fps')} FPS" for _, m in modes)
        g.emit(modes[0][0], f"{side}_video_recording", f"{side.capitalize()} video recording", value)
        for line, _ in modes[1:]:
            g.used.setdefault(line.index, set()).add(f"{side}_video_recording")
    if hit := g.find(_SLOMO):
        g.emit(hit[0], "slow_motion_video", "Slow-motion video", hit[1].group("n"), "FPS")
    if hit := g.find(_HDR_VIDEO):
        g.emit(hit[0], "hdr_video_recording", "HDR video recording", hit[1].group("bit"))


_IP = re.compile(r"\bIP\s?\d{2}[A-Z]?(?:\s*(?:/|,|and)\s*IP\s?\d{2}[A-Z]?)*", re.I)


def _durability(g: _Group) -> None:
    if hit := g.find(_IP):
        value = re.sub(r"\bIP\s?", "IP", re.sub(r"\s+", " ", hit[1].group(0)), flags=re.I).upper().replace("AND", ",")
        g.emit(hit[0], "ip_rating", "IP rating", value)
    if hit := g.find(_GLASS):
        g.emit(hit[0], "cover_glass", "Cover glass", hit[1].group("g"))


_OS = re.compile(
    r"\b(?P<name>Android|iOS|iPadOS|HarmonyOS|Windows|macOS|Tizen|One UI|HyperOS|MIUI|ColorOS|OxygenOS)\s*(?P<v>\d+(?:\.\d+)?)?"
)


_OS_STATEMENT = re.compile(
    r"^(?:launched with|launches with|ships with|runs|preinstalled|pre-installed|operating system\s*:)\s+(?P<v>.{2,60})$",
    re.I,
)


def _os(g: _Group) -> None:
    if hit := g.find(_OS_STATEMENT):
        g.emit(hit[0], "operating_system", "Operating system", hit[1].group("v").strip(" ."))
    elif hit := g.find(_OS):
        m = hit[1]
        g.emit(hit[0], "operating_system", "Operating system",
               f"{m.group('name')} {m.group('v')}" if m.group("v") else m.group("name"))


_YEARS = re.compile(r"(?P<n>\d+)\s+years?\s+of\b.*\bupdates?", re.I)


def _updates(g: _Group) -> None:
    if hit := g.find(_YEARS):
        g.emit(hit[0], "software_update_support", "Software update support", hit[1].group("n"), "years")


_USB = re.compile(r"\bUSB[\s-]*(?:Type[\s-]*)?C\b\s*(?P<v>\d(?:\.\d)?)?", re.I)
_BUTTON = re.compile(r"\b(?:buttons?|controls?)\b", re.I)


def _ports(g: _Group) -> None:
    if hit := g.find(_USB):
        m = hit[1]
        g.emit(hit[0], "usb", "USB", f"USB Type-C {m.group('v')}" if m.group("v") else "USB Type-C")
    buttons = [line for line in g.lines if _BUTTON.search(line.text) and line.index not in g.used]
    if buttons:
        g.emit(buttons[0], "buttons", "Buttons", "; ".join(line.text for line in buttons))
        for line in buttons[1:]:
            g.used.setdefault(line.index, set()).add("buttons")


_MICS = re.compile(r"(?P<n>\d+)\s+microphones?", re.I)
_SPEAKERS = re.compile(r"^(?P<k>stereo|mono|dual)\s+speakers?", re.I)


def _media(g: _Group) -> None:
    if hit := g.find(_MICS):
        g.emit(hit[0], "microphone_count", "Microphones", hit[1].group("n"))
    if hit := g.find(_SPEAKERS):
        g.emit(hit[0], "speakers", "Speakers", hit[1].group("k").capitalize())


_WIFI = re.compile(r"\bWi-?Fi\s*(?P<gen>\d+[A-Za-z]?)(?:\s*\((?P<std>802\.11[\w/]+)\))?", re.I)
_WIFI_BANDS = re.compile(r"(?P<b>\d[\d.]*\s?GHz(?:\s?\+\s?\d[\d.]*\s?GHz)+)", re.I)
_MIMO = re.compile(r"(?P<m>\d+x\d+(?:\+\d+x\d+)?)\s+MIMO", re.I)
_BT = re.compile(r"\bBluetooth\s*(?:v|version\s*)?(?P<v>\d(?:\.\d)?)", re.I)
_SIM = re.compile(r"\b(?:dual|single|triple)[\s-]*SIM\b(?:\s*\([^)]{1,60}\))?", re.I)
_GNSS_LIST = re.compile(r"^(?:(?:GPS|GLONASS|Galileo|BeiDou|QZSS|NavIC|SBAS)(?:,\s*|$))+$", re.I)


def _sim(g: _Group) -> None:
    if hit := g.find(_SIM):
        g.emit(hit[0], "sim", "SIM", re.sub(r"\s+", " ", hit[1].group(0)).strip())
    if hit := g.find(re.compile(r"\beSIM\b", re.I)):
        g.emit(hit[0], "esim", "eSIM", "Yes", boolean=True)


def _wireless(g: _Group) -> None:
    if hit := g.find(_WIFI):
        line, m = hit
        value = f"Wi-Fi {m.group('gen')}" + (f" ({m.group('std')})" if m.group("std") else "")
        g.emit(line, "wifi", "Wi-Fi", value)
        if bands := _WIFI_BANDS.search(line.text):
            g.emit(line, "wifi_bands", "Wi-Fi bands",
                   re.sub(r"\s*\+\s*", " + ", re.sub(r"(\d)GHz", r"\1 GHz", bands.group("b"))))
        if mimo := _MIMO.search(line.text):
            g.emit(line, "wifi_mimo", "Wi-Fi MIMO", mimo.group("m"))
    if hit := g.find(_BT):
        g.emit(hit[0], "bluetooth", "Bluetooth", f"Bluetooth {hit[1].group('v')}")
    if hit := g.find(re.compile(r"^NFC\b", re.I)):
        g.emit(hit[0], "nfc", "NFC", "Yes", boolean=True)
    if hit := g.find(re.compile(r"ultra-?\s?wide-?band|\bUWB\b", re.I)):
        g.emit(hit[0], "uwb", "Ultra-wideband (UWB)", "Yes", boolean=True)
    if hit := g.find(re.compile(r"dual[- ]band\s+GNSS", re.I)):
        g.emit(hit[0], "gnss_dual_band", "Dual-band GNSS", "Yes", boolean=True)
    for line in g.lines:
        if _GNSS_LIST.match(line.text):
            g.emit(line, "gnss", "GNSS / positioning", "; ".join(re.split(r",\s*", line.text)))
    _sim(g)


_GSM = re.compile(r"^GSM(?:/EDGE)?\s*:\s*(?P<v>.+)$", re.I)
_UMTS = re.compile(r"^UMTS[^:]*:\s*(?:Bands?\s+)?(?P<v>.+)$", re.I)
_LTE = re.compile(r"^LTE\s*:\s*(?:Bands?\s+)?(?P<v>.+)$", re.I)
_NR_SUB6 = re.compile(r"^5G\s+Sub-?\s?6[^:]*:\s*(?:Bands?\s+)?(?P<v>.+)$", re.I)
_NR_MMW = re.compile(r"^5G\s+mmWave\s*:\s*(?:Bands?\s+)?(?P<v>.+)$", re.I)
_NR_TYPE = re.compile(r"^5G\s+(?P<v>[^:]+)$", re.I)
_MODEL_NO = re.compile(r"^Model\s+(?P<v>[A-Za-z0-9-]{3,20})$", re.I)


def _network(g: _Group) -> None:
    generations: list[str] = []
    if hit := g.find(_GSM):
        inner = re.search(r"\((?P<b>[^)]+)\)", hit[1].group("v"))
        g.emit(hit[0], "gsm_bands", "GSM bands", inner.group("b") if inner else hit[1].group("v"))
        generations.append("2G")
    if hit := g.find(_UMTS):
        g.emit(hit[0], "umts_bands", "UMTS/HSPA bands", expand_bands(hit[1].group("v")))
        generations.append("3G")
    if hit := g.find(_LTE):
        g.emit(hit[0], "lte_bands", "LTE bands", expand_bands(hit[1].group("v")))
        generations.extend(["4G", "LTE"])
    five_g = False
    if hit := g.find(_NR_SUB6):
        g.emit(hit[0], "nr_sub6_bands", "5G Sub-6 bands", expand_bands(hit[1].group("v")))
        five_g = True
    if hit := g.find(_NR_MMW):
        g.emit(hit[0], "nr_mmwave_bands", "5G mmWave bands", expand_bands(hit[1].group("v")))
        five_g = True
    for line in g.lines:
        m = _NR_TYPE.match(line.text)
        if m and ":" not in line.text and "5G" not in generations:
            kind = re.sub(r"\bSub\s?-?\s?6\s?GHz\b", "Sub-6 GHz", m.group("v").strip(), flags=re.I)
            g.emit(line, "network_5g_type", "5G type", kind)
            five_g = True
    if five_g:
        generations.append("5G")
    if hit := g.find(_MODEL_NO):
        g.emit(hit[0], "model_number", "Model number", hit[1].group("v"))
    if generations:
        g.facts.append(Fact("network_generations", "Network generations", "; ".join(dict.fromkeys(generations)),
                            None, "derived from the network technologies listed", False, g.heading))
    _sim(g)


_HEARING = re.compile(r"hearing aid[- ]compatible", re.I)
_LABEL_VALUE = re.compile(r"^(?P<label>[A-Za-z][\w /&+\-()]{1,38}[\w)])\s*:\s*(?P<value>\S.*)$")


def _accessibility(g: _Group) -> None:
    if hit := g.find(_HEARING):
        g.emit(hit[0], "hearing_aid_compatible", "Hearing aid compatible", "Yes", boolean=True)
    for line in g.lines:
        m = re.match(r"^Conversational Gain\s*:\s*(?P<v>.+?)(?:\.?\s*See)?$", line.text, re.I)
        if m:
            g.emit(line, "conversational_gain", "Conversational gain", m.group("v").rstrip(". "))


def _colors(g: _Group) -> None:
    items = [line for line in g.lines if len(line.text) <= 40 and not _POINTER_RE.match(line.text)]
    if items:
        g.emit(items[0], "color", "Color", "; ".join(dict.fromkeys(line.text for line in items)))
        for line in items[1:]:
            g.used.setdefault(line.index, set()).add("color")


_READERS: dict[str, Callable[[_Group], None]] = {
    "display": _display, "body": _body, "battery": _battery, "memory": _memory,
    "processor": _processor, "rear_camera": _rear_camera, "front_camera": _front_camera,
    "video": _video, "durability": _durability, "os": _os, "updates": _updates,
    "ports": _ports, "media": _media, "wireless": _wireless, "network": _network,
    "accessibility": _accessibility, "colors": _colors,
}


# ---------------------------------------------------------------------------
# Fallback: nothing structured is dropped silently
# ---------------------------------------------------------------------------


def _list_target(heading: str, sub: str | None) -> str | None:
    for heading_re, sub_re, canonical in _LIST_TARGETS:
        if not heading_re.search(heading):
            continue
        if sub_re is not None and not (sub and sub_re.search(sub)):
            continue
        return canonical
    return None


def _fallback(g: _Group) -> None:
    rest = g.unresolved()
    if not rest:
        return
    target = _list_target(g.heading, g.sub)
    items: list[Line] = []
    for line in rest:
        text = line.text
        if _LINK_RE.match(text) or _POINTER_RE.match(text):
            g.notes[line.index] = "REJECT:link or navigation text, no characteristic"
            continue
        if re.search(r"\b(?:documentation|documents?|manuals?)\b", text, re.I) and len(text) < 60:
            g.notes[line.index] = "REJECT:documentation pointer, no characteristic"
            continue
        if len(text) < 2 or not re.search(r"[A-Za-z0-9]", text):
            g.notes[line.index] = "REJECT:no content"
            continue
        if target is not None and (m := _LABEL_VALUE.match(text)) \
                and (known := lookup_canonical(m.group("label").strip())):
            g.emit(line, known, label_for(known), m.group("value").strip()[:MAX_VALUE_CHARS])
            continue
        if target is None:
            m = _LABEL_VALUE.match(text)
            if m:
                label = m.group("label").strip()
                g.emit(line, resolve_canonical(label), label, m.group("value").strip()[:MAX_VALUE_CHARS])
                continue
            support = _SUPPORT.match(text)
            if support:
                label = f"{support.group('what')} support"
                g.emit(line, resolve_canonical(label), label, "Yes", boolean=True)
                continue
        if len(text) > MAX_LIST_ITEM_CHARS:
            g.notes[line.index] = "REJECT:long marketing prose, not a structured characteristic"
            continue
        items.append(line)
    if not items:
        return
    canonical = target or resolve_canonical(g.sub or g.heading)
    label = (g.sub or g.heading) if target is None else None
    kept: list[Line] = []
    size = 0
    for line in items:
        if size + len(line.text) + 2 > MAX_LIST_CHARS:
            g.notes[line.index] = f"REJECT:list length cap ({MAX_LIST_CHARS} chars) reached"
            continue
        size += len(line.text) + 2
        kept.append(line)
    if kept:
        values = "; ".join(dict.fromkeys(line.text for line in kept))
        g.facts.append(Fact(canonical, label or label_for(canonical), values, None,
                            kept[0].text[:140], False, g.heading))
        for line in kept:
            g.used.setdefault(line.index, set()).add(canonical)


# ---------------------------------------------------------------------------
# Section entry point
# ---------------------------------------------------------------------------


def split_groups(heading: str, lines: list[Line]) -> list[tuple[str | None, list[Line]]]:
    groups: list[tuple[str | None, list[Line]]] = [(None, [])]
    for line in lines:
        if is_heading(line) and line.text.casefold() != heading.casefold():
            groups.append((line.text, []))
        elif is_heading(line):
            continue
        else:
            groups[-1][1].append(line)
    return [group for group in groups if group[1] or group[0]]


def read_section(heading: str, lines: list[Line]) -> tuple[list[Fact], list[dict[str, object]]]:
    """Atomic facts of one section plus the per-line ledger."""
    lines = merge_continuations(lines)
    roles = section_roles(heading)
    facts: list[Fact] = []
    ledger: list[dict[str, object]] = []
    for line in lines:
        if is_heading(line):
            ledger.append({"section": heading, "sub": None, "line": line.text, "outcome": "heading",
                           "attributes": [], "reason": "sub-section heading (structure, not a value)"})
    for sub, group_lines in split_groups(heading, lines):
        group = _Group(heading, sub, group_lines)
        for role in sorted(roles):
            reader = _READERS.get(role)
            if reader:
                reader(group)
        _fallback(group)
        facts.extend(group.facts)
        for line in group_lines:
            canonicals = sorted(group.used.get(line.index, ()))
            note = group.notes.get(line.index)
            if canonicals:
                entry = {"outcome": "accepted", "attributes": canonicals, "reason": ""}
            elif note and note.startswith("REJECT:"):
                entry = {"outcome": "rejected", "attributes": [], "reason": note[7:]}
            elif note:
                entry = {"outcome": "merged", "attributes": [], "reason": note}
            else:
                entry = {"outcome": "rejected", "attributes": [],
                         "reason": "no reader recognised this line's structure"}
            ledger.append({"section": heading, "sub": sub, "line": line.text[:160], **entry})
    return facts, ledger


def dedupe_facts(facts: Iterable[Fact]) -> tuple[list[Fact], list[Fact]]:
    """(kept, duplicates): the first fact per canonical attribute wins."""
    kept: dict[str, Fact] = {}
    duplicates: list[Fact] = []
    for fact in facts:
        if fact.canonical in kept:
            duplicates.append(fact)
        else:
            kept[fact.canonical] = fact
    return list(kept.values()), duplicates
