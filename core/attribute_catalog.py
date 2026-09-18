"""Stage 32: language-neutral catalog + dynamic schema extension.

The category schema (``core.schema``) is what a category is *expected* to have,
not what it may contain. When an official source states a valid technical
characteristic the schema does not yet know, the characteristic is not dropped:
it becomes a canonical attribute and the schema is extended for that run.

Rules
* canonical names are English snake_case identifiers, never a localized label;
* one entity = one canonical attribute: synonyms are alias-normalized here
  (``max brightness`` / ``maximum brightness`` / ``peak brightness`` ->
  ``peak_brightness``);
* an unknown label still becomes a (dynamic) canonical attribute derived from
  its own normalized text; it is never discarded for being unknown.

Nothing in this module knows a brand, a product or a value.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from core.normalize import attribute_label_variants, normalize_attribute_label
from core.schema import AttributeDefinition


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    canonical: str
    label: str
    aliases: tuple[str, ...] = ()
    value_type: str = "text"


CATALOG: tuple[CatalogEntry, ...] = (
    # display
    CatalogEntry("display_ppi", "Pixel density", ("ppi", "pixel density", "pixels per inch")),
    CatalogEntry("display_aspect_ratio", "Display aspect ratio", ("aspect ratio", "screen ratio")),
    CatalogEntry("cover_glass", "Cover glass", ("display glass", "screen glass", "protective glass")),
    CatalogEntry("hdr_support", "HDR support", ("hdr",), "boolean"),
    CatalogEntry("hdr_brightness", "HDR brightness", ("hdr peak brightness",)),
    CatalogEntry("peak_brightness", "Peak brightness",
                 ("maximum brightness", "max brightness", "brightness peak", "peak display brightness")),
    CatalogEntry("contrast_ratio", "Contrast ratio", ("contrast",)),
    CatalogEntry("color_depth", "Color depth", ("colour depth", "bit depth")),
    CatalogEntry("number_of_colors", "Number of colors", ("colors count", "display colors", "colours")),
    # battery
    CatalogEntry("battery_capacity_min", "Minimum battery capacity", ("minimum battery",)),
    CatalogEntry("fast_charging", "Fast charging", ("quick charge", "quick charging")),
    CatalogEntry("wireless_charging", "Wireless charging", ("qi charging",), "boolean"),
    CatalogEntry("wireless_charging_standard", "Wireless charging standard"),
    CatalogEntry("battery_life", "Battery life", ("battery endurance",)),
    CatalogEntry("battery_life_power_saving", "Battery life (power saving)"),
    CatalogEntry("battery_features", "Battery features"),
    # processor / security / software
    CatalogEntry("security_coprocessor", "Security coprocessor", ("security chip",)),
    CatalogEntry("security_features", "Security features"),
    CatalogEntry("software_update_support", "Software update support", ("update support", "os updates")),
    # camera
    CatalogEntry("max_zoom", "Max zoom", ("maximum zoom", "zoom range", "super res zoom")),
    CatalogEntry("rear_camera_features", "Rear camera features"),
    CatalogEntry("front_camera_features", "Front camera features"),
    CatalogEntry("front_camera_aperture", "Front camera aperture"),
    CatalogEntry("front_camera_field_of_view", "Front camera field of view"),
    CatalogEntry("front_camera_autofocus", "Front camera autofocus", (), "boolean"),
    CatalogEntry("camera_features", "Camera features"),
    CatalogEntry("editing_features", "Photo editing features"),
    # video
    CatalogEntry("rear_video_recording", "Rear video recording"),
    CatalogEntry("front_video_recording", "Front video recording"),
    CatalogEntry("rear_video_features", "Rear video features"),
    CatalogEntry("front_video_features", "Front video features"),
    CatalogEntry("slow_motion_video", "Slow-motion video"),
    CatalogEntry("hdr_video_recording", "HDR video recording"),
    CatalogEntry("video_formats", "Video formats"),
    CatalogEntry("video_features", "Video features"),
    CatalogEntry("video_audio_features", "Video audio features"),
    # body / misc lists
    CatalogEntry("materials_and_durability", "Materials and durability"),
    CatalogEntry("authentication", "Authentication", ("biometrics", "unlock methods")),
    CatalogEntry("safety_features", "Safety features"),
    CatalogEntry("sensors", "Sensors"),
    CatalogEntry("buttons", "Buttons"),
    CatalogEntry("speakers", "Speakers", ("loudspeaker",)),
    CatalogEntry("microphone_count", "Microphones", ("number of microphones",)),
    CatalogEntry("media_features", "Audio features"),
    # connectivity
    CatalogEntry("wifi_bands", "Wi-Fi bands"),
    CatalogEntry("wifi_mimo", "Wi-Fi MIMO"),
    CatalogEntry("nfc", "NFC", (), "boolean"),
    CatalogEntry("uwb", "Ultra-wideband (UWB)", ("ultra wideband",), "boolean"),
    CatalogEntry("gnss", "GNSS / positioning", ("gps", "positioning", "navigation satellites")),
    CatalogEntry("gnss_dual_band", "Dual-band GNSS", (), "boolean"),
    CatalogEntry("wireless_features", "Wireless features"),
    CatalogEntry("esim", "eSIM", (), "boolean"),
    # network
    CatalogEntry("network_generations", "Network generations"),
    CatalogEntry("network_5g_type", "5G type"),
    CatalogEntry("model_number", "Model number", ("regulatory model number",)),
    CatalogEntry("gsm_bands", "GSM bands"),
    CatalogEntry("umts_bands", "UMTS/HSPA bands"),
    CatalogEntry("lte_bands", "LTE bands"),
    CatalogEntry("nr_sub6_bands", "5G Sub-6 bands"),
    CatalogEntry("nr_mmwave_bands", "5G mmWave bands"),
    # accessibility
    CatalogEntry("hearing_aid_compatible", "Hearing aid compatible", (), "boolean"),
    CatalogEntry("conversational_gain", "Conversational gain"),
    CatalogEntry("accessibility_features", "Accessibility features"),
)

_BY_CANONICAL = {entry.canonical: entry for entry in CATALOG}
_BY_ALIAS: dict[str, str] = {}
for _entry in CATALOG:
    for _alias in (_entry.canonical.replace("_", " "), _entry.label, *_entry.aliases):
        _BY_ALIAS.setdefault(normalize_attribute_label(_alias), _entry.canonical)

_LENS_PART = re.compile(r"^(?P<role>[a-z]+)_camera_(?P<part>[a-z_]+)$")


def slug(label: str) -> str:
    """Stable canonical identifier for a label: lower-case ascii-ish snake_case."""
    return re.sub(r"\s+", "_", normalize_attribute_label(label)).strip("_") or "unnamed_attribute"


def resolve_canonical(label: str) -> str:
    """One canonical name per entity: alias-normalize, else derive from the label."""
    for key in attribute_label_variants(label):
        if key in _BY_ALIAS:
            return _BY_ALIAS[key]
    return slug(label)


def lookup_canonical(label: str) -> str | None:
    """The catalog canonical for a label, or None when the label is not catalogued."""
    for key in attribute_label_variants(label):
        if key in _BY_ALIAS:
            return _BY_ALIAS[key]
    return None


def label_for(canonical: str) -> str:
    entry = _BY_CANONICAL.get(canonical)
    if entry:
        return entry.label
    return canonical.replace("_", " ").strip().capitalize()


def definition_for(canonical: str, label: str | None = None, *, boolean: bool = False) -> AttributeDefinition:
    """Schema definition for a catalog or purely dynamic canonical attribute."""
    entry = _BY_CANONICAL.get(canonical)
    text = label or label_for(canonical)
    aliases = tuple(dict.fromkeys((text, *(entry.aliases if entry else ()))))
    return AttributeDefinition(
        canonical_name=canonical,
        aliases=list(aliases),
        scope="category_specific",
        value_type="boolean" if boolean or (entry and entry.value_type == "boolean") else "text",
        attribute_scope="model_level",
        priority="medium",
        expected=False,
        notes="Dynamically added from an official source (Stage 32).",
    )


def extension_definitions(
    hints: Iterable[tuple[str, str | None, bool]],
    known: Iterable[str],
) -> list[AttributeDefinition]:
    """Definitions for every hinted canonical name the schema does not have."""
    seen = set(known)
    added: list[AttributeDefinition] = []
    for canonical, label, boolean in hints:
        if canonical in seen:
            continue
        seen.add(canonical)
        added.append(definition_for(canonical, label, boolean=boolean))
    return added


def lens_attribute_parts(canonical: str) -> tuple[str, str] | None:
    """(role, part) for a per-lens attribute such as ``wide_camera_aperture``."""
    match = _LENS_PART.match(canonical)
    if match and match.group("role") in {
        "wide", "ultrawide", "telephoto", "periscope", "macro", "main", "depth",
    }:
        return match.group("role"), match.group("part")
    return None


def catalog_canonicals() -> tuple[str, ...]:
    return tuple(_BY_CANONICAL)
