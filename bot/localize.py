"""Stage 32.1: semantic RU presentation of canonical attribute values.

Presentation only: the service DTOs and the cache keep the source's own
values. In RU mode a value's *units and connective prose* are localized
("6.3 in" -> "6,3 дюйма", "16 GB" -> "16 ГБ", "Yes" -> "Да"); product,
chip, port and standard names are never touched ("Google Tensor G4",
"Mali-G715 MC7", "USB Type-C 3.2", "Wi-Fi 7", "Bluetooth 5.3", "IP68",
model/SKU identifiers). EN mode returns values unchanged.
"""

from __future__ import annotations

import re

from bot.i18n import Language


CATEGORY_NAMES_RU = {
    "unknown": "Не определена",
    "cooktop": "Варочная панель",
    "smartphone": "Смартфон",
    "laptop": "Ноутбук",
    "sewing machine": "Швейная машина",
    "air fryer": "Аэрогриль",
    "wet/dry vacuum": "Моющий пылесос",
}

_YES_NO_RU = {"yes": "Да", "no": "Нет", "true": "Да", "false": "Нет"}

# Metric / digital units that follow a number. Case-sensitive on purpose:
# "5G" (network), "4g" and other lookalikes must never be read as units.
_UNITS_RU = {
    "GB": "ГБ", "TB": "ТБ", "MB": "МБ", "KB": "КБ",
    "Hz": "Гц", "kHz": "кГц", "MHz": "МГц", "GHz": "ГГц",
    "mAh": "мА·ч", "Ah": "А·ч", "Wh": "Вт·ч",
    "W": "Вт", "kW": "кВт", "V": "В",
    "mm": "мм", "cm": "см", "kg": "кг", "g": "г", "ml": "мл",
    "MP": "Мп", "PPI": "ppi", "ppi": "ppi",
    "pixels": "пикселей", "pixel": "пиксель", "px": "пикселей",
    "nits": "нит", "nit": "нит", "dB": "дБ", "FPS": "кадр/с", "fps": "кадр/с",
    "h": "ч", "s": "с",
}
# Units whose Russian word declines with the number: (1, 2-4, 5+).
_PLURAL_UNITS_RU = {
    "years": ("год", "года", "лет"), "year": ("год", "года", "лет"),
    "minutes": ("минута", "минуты", "минут"), "hours": ("час", "часа", "часов"),
}
_NUMBER_UNIT_RE = re.compile(
    r"(?<![\w.,])(?P<num>\d+(?:[.,]\d+)?(?:/\d+(?:[.,]\d+)?)*\+?)(?P<gap>\s?)"
    r"(?P<unit>" + "|".join(sorted(map(re.escape, (*_UNITS_RU, *_PLURAL_UNITS_RU)), key=len, reverse=True)) + r")\b"
)
_BIT_RE = re.compile(r"(?<![\w.,])(\d+)-bit\b", re.I)
_MILLION_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?) (million|billion|thousand)\b", re.I)
_MAGNITUDES_RU = {"million": "млн", "billion": "млрд", "thousand": "тыс."}
_INCH_RE = re.compile(
    r"(?<![\w.,])(?P<num>\d+(?:[.,]\d+)?)\s?(?:inches|inch|in|\"|″|”)(?![\w])"
)
_RESOLUTION_RE = re.compile(r"(?<![\w.,])(?P<w>\d{3,5}) ?x ?(?P<h>\d{3,5})(?![\w])", re.I)
_DIM_SEP_RE = re.compile(r"(?<=[\d.,]) ?x ?(?=\d)")

_CAMERA_ROLES_RU = {
    "ultrawide": "сверхширокоугольная",
    "wide": "широкоугольная",
    "telephoto": "телефото",
    "periscope": "перископная",
    "macro": "макро",
    "main": "основная",
    "depth": "датчик глубины",
}
_CAMERA_ROLE_RE = re.compile(
    r"(?<=Мп )(?P<role>ultrawide|wide|telephoto|periscope|macro|main|depth)\b"
)
_SIM_PHRASES_RU = (
    (re.compile(r"\bSingle Nano SIM and eSIM\b", re.I), "одна nano-SIM и eSIM"),
    (re.compile(r"\bNano SIM and eSIM\b", re.I), "nano-SIM и eSIM"),
)


_GENERIC_COLORS_RU = {
    "black": "Чёрный", "white": "Белый", "green": "Зелёный", "blue": "Синий",
    "red": "Красный", "yellow": "Жёлтый", "gray": "Серый", "grey": "Серый",
    "silver": "Серебристый", "gold": "Золотистый", "purple": "Фиолетовый",
    "pink": "Розовый", "orange": "Оранжевый", "brown": "Коричневый",
}

_CAMERA_ITEMS_RU = {
    "night sight": "Ночной режим",
    "night mode": "Ночной режим",
    "macro focus": "Макрофокус",
    "portrait mode": "Портретный режим",
    "astrophotography": "Астрофотография",
    "pro controls": "Профессиональные настройки",
    "add me": "Добавь меня",
    "face unblur": "Устранение размытия лица",
    "long exposure": "Длинная выдержка",
    "action pan": "Панорамирование в движении",
    "real tone": "Real Tone",  # official/proprietary name
    "panorama": "Панорама",
    "top shot": "Лучший кадр",
    "frequent faces": "Частые лица",
    "stereo recording": "Стереозапись",
    "speech enhancement": "Улучшение речи",
    "wind noise reduction": "Подавление шума ветра",
    "audio zoom": "Аудиозум",
    "noise suppression": "Шумоподавление",
    "spatial audio": "Пространственное аудио",
    "night video": "Ночная видеосъёмка",
    "high-res (up to 50mp)": "Высокое разрешение (до 50 Мп)",
    "super res zoom up to 30x": "Super Res Zoom до 30×",
    "super res zoom video up to 20x": "Super Res Zoom Video до 20×",
    "multi-zone ldaf (laser detect auto focus) sensor": "Многозонный датчик LDAF (лазерная автофокусировка)",
    "optical + electronic image stabilization on wide and telephoto": "Оптическая и электронная стабилизация на широкоугольной камере и телеобъективе",
    "dual exposure on wide camera": "Двойная экспозиция на широкоугольной камере",
    "optical image stabilization for video": "Оптическая стабилизация видео",
    "fused video stabilization": "Комбинированная стабилизация видео",
    "4k timelapse with stabilization": "Таймлапс 4K со стабилизацией",
    "astrophotography timelapse": "Астрофотографический таймлапс",
    "night sight timelapse": "Ночной таймлапс",
}

_SENSORS_RU = {
    "accelerometer": "Акселерометр",
    "gyroscope": "Гироскоп",
    "gyrometer": "Гироскоп",
    "proximity sensor": "Датчик приближения",
    "ambient light sensor": "Датчик освещённости",
    "barometer": "Барометр",
    "magnetometer": "Магнитометр",
    "temperature sensor": "Датчик температуры",
    "spectral and flicker sensor": "Датчик спектра и мерцания",
}

_AUTHENTICATION_RU = {
    "fingerprint": "Отпечаток пальца",
    "fingerprint unlock": "Разблокировка по отпечатку пальца",
    "face unlock": "Разблокировка по лицу",
    "pin": "PIN-код",
    "pattern": "Графический ключ",
    "password": "Пароль",
}

_CHARGING_RU = {
    "wired charging": "Проводная зарядка",
    "wireless charging": "Беспроводная зарядка",
    "fast charging": "Быстрая зарядка",
    "reverse wireless charging": "Обратная беспроводная зарядка",
}

_MATERIALS_RU = {
    "aluminum": "Алюминий",
    "aluminium": "Алюминий",
    "glass": "Стекло",
    "stainless steel": "Нержавеющая сталь",
    "aluminium frame with matte glass back": "Алюминиевая рамка с задней панелью из матового стекла",
    "aluminum frame with matte glass back": "Алюминиевая рамка с задней панелью из матового стекла",
    "fingerprint-resistant coating": "Олеофобное покрытие",
    "made with 20% recycled materials": "Изготовлено с использованием 20% переработанных материалов",
    "100% plastic-free packaging": "Упаковка без пластика",
}

_PACKAGE_ITEMS_RU = {
    "sim tool": "Инструмент для SIM-карты",
    "sim eject tool": "Инструмент для извлечения SIM-карты",
    "documentation": "Документация",
    "quick start guide": "Краткое руководство",
    "power adapter": "Адаптер питания",
    "charger": "Зарядное устройство",
}

_GENERIC_TECH_RU = {
    "power button": "Кнопка питания",
    "volume controls": "Кнопки регулировки громкости",
    "emergency sos": "Экстренный вызов SOS",
    "crisis alerts": "Оповещения о кризисных ситуациях",
    "car crash detection": "Распознавание автомобильной аварии",
    "safety check": "Проверка безопасности",
    "emergency location service": "Служба экстренного определения местоположения",
    "emergency contacts & medical info": "Экстренные контакты и медицинская информация",
    "android earthquake alerts warning": "Оповещения Android о землетрясениях",
}

_FEATURE_CANONICALS = {
    "camera_features", "rear_camera_features", "front_camera_features",
    "editing_features", "rear_video_features", "front_video_features",
    "video_features", "video_audio_features", "media_features",
}
_SEMANTIC_LIST_CANONICALS = {
    "color", "package_contents", "materials_and_durability", "sensors",
    "authentication", "charging_types", "charging_type", "buttons",
    "safety_features", "wireless_features", *_FEATURE_CANONICALS,
}


def _lookup_item(item: str, catalog: dict[str, str]) -> str:
    """Translate a complete semantic item; unknown/proprietary items survive."""
    key = " ".join(item.strip().split()).casefold()
    return catalog.get(key, item.strip())


def _localize_package_item(item: str) -> str:
    normalized = " ".join(item.strip().split())
    known = _PACKAGE_ITEMS_RU.get(normalized.casefold())
    if known:
        return known
    cable = re.fullmatch(
        r"(?:(?P<length>\d+(?:[.,]\d+)?)\s*m\s+)?(?P<kind>.+?)\s+cable(?P<detail>\s*\([^)]*\))?",
        normalized, re.I,
    )
    if not cable:
        return normalized
    kind = re.sub(r"\s+to\s+", "—", cable["kind"], flags=re.I)
    length = f" длиной {_decimal(cable['length'], 'ru')} м" if cable["length"] else ""
    return f"Кабель {kind}{length}{cable['detail'] or ''}"


def _localize_material_item(item: str) -> str:
    normalized = " ".join(item.strip().split())
    known = _MATERIALS_RU.get(normalized.casefold())
    if known:
        return known
    recycled = re.fullmatch(r"Made with (?:at least )?(\d+)% recycled materials(?: based on product weight)?", normalized, re.I)
    if recycled:
        return f"Изготовлено с использованием не менее {recycled[1]}% переработанных материалов"
    aluminum = re.fullmatch(r"The alumini?um in the housing is (\d+)% recycled content", normalized, re.I)
    if aluminum:
        return f"Алюминий корпуса на {aluminum[1]}% состоит из переработанного материала"
    # Keep trademarked material names intact while localizing their ordinary
    # structural description as one recognized clause.
    branded_back = re.fullmatch(
        r"(Corning Gorilla Glass [\w ]+) silky matte back with polished finish metal frame",
        normalized, re.I,
    )
    if branded_back:
        return f"Задняя панель из {branded_back[1]} с шелковисто-матовым покрытием и полированной металлической рамкой"
    return normalized


def _localize_semantic_items(canonical_name: str, text: str) -> str:
    if canonical_name == "color" or canonical_name.endswith("_color"):
        translate_item = lambda item: _lookup_item(item, _GENERIC_COLORS_RU)
    elif canonical_name == "package_contents":
        translate_item = _localize_package_item
    elif "material" in canonical_name:
        translate_item = _localize_material_item
    elif canonical_name == "sensors" or canonical_name.endswith("_sensors"):
        translate_item = lambda item: _lookup_item(item, _SENSORS_RU)
    elif canonical_name == "authentication":
        # Authentication sources commonly mix ';' and ',' in one list.
        return "; ".join(
            ", ".join(_lookup_item(part, _AUTHENTICATION_RU) for part in item.split(","))
            for item in text.split(";")
        )
    elif canonical_name in {"charging_types", "charging_type"}:
        translate_item = lambda item: _lookup_item(item, _CHARGING_RU)
    elif canonical_name in _FEATURE_CANONICALS:
        translate_item = lambda item: _lookup_item(item, _CAMERA_ITEMS_RU)
    elif canonical_name in {"buttons", "safety_features", "wireless_features"}:
        translate_item = lambda item: _lookup_item(item, _GENERIC_TECH_RU)
    else:
        return text
    return "; ".join(translate_item(item) for item in text.split(";"))


def category_name(name: str, language: Language) -> str:
    if language != "ru":
        return name
    return CATEGORY_NAMES_RU.get(name.strip().casefold(), name)


def _decimal(number: str, language: Language) -> str:
    return number.replace(".", ",") if language == "ru" else number


def _inch_word(number: str) -> str:
    """RU plural of "дюйм" for the number as written."""
    if "," in number or "." in number:
        return "дюйма"
    n = int(number)
    if 11 <= n % 100 <= 14:
        return "дюймов"
    return {1: "дюйм", 2: "дюйма", 3: "дюйма", 4: "дюйма"}.get(n % 10, "дюймов")


def _plural_ru(number: str, forms: tuple[str, str, str]) -> str:
    digits = re.sub(r"\D", "", number.split(",")[0].split(".")[0])
    n = int(digits) if digits else 0
    if "," in number or "." in number:
        return forms[1]
    if 11 <= n % 100 <= 14:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def _unit_replacement(match: re.Match[str]) -> str:
    number = _decimal(match["num"], "ru")
    unit = match["unit"]
    if unit in _PLURAL_UNITS_RU:
        return f"{number} {_plural_ru(match['num'], _PLURAL_UNITS_RU[unit])}"
    return f"{number} {_UNITS_RU[unit]}"


def localize_value(
    canonical_name: str,
    text: str,
    unit: str | None,
    language: Language,
) -> str:
    """Presentation text for a value (already joined with its unit).

    ``text`` is the value as the pipeline holds it (for example
    ``"1280 x 2856 pixels"`` or ``"128 GB / 256 GB"``).
    """
    if language != "ru":
        return text
    stripped = text.strip()
    if stripped.casefold() in _YES_NO_RU:
        return _YES_NO_RU[stripped.casefold()]
    if (
        canonical_name in {"charging_type", "charging_types", "fast_charging", "wireless_charging"}
        and stripped.casefold() in _CHARGING_RU
    ):
        return _CHARGING_RU[stripped.casefold()]

    # Formats and protocol/name lists are identifiers, not natural-language
    # feature lists.  They remain byte-for-byte intact.
    if canonical_name in {"video_formats"}:
        return text
    result = _localize_semantic_items(canonical_name, text)
    # Item localization is atomic by design.  Do not subsequently run a unit
    # regex over an unknown item: it may be a proprietary name containing a
    # number/unit-like token. Known items carry their localized units in the
    # semantic catalog itself.
    if (
        canonical_name in _SEMANTIC_LIST_CANONICALS
        or canonical_name.endswith(("_color", "_sensors"))
        or "material" in canonical_name
    ):
        return result
    if canonical_name in {"display_size", ""}:
        result = _INCH_RE.sub(
            lambda m: f"{_decimal(m['num'], language)} {_inch_word(m['num'])}", result,
        )
    result = _RESOLUTION_RE.sub(lambda m: f"{m['w']} × {m['h']}", result)
    if canonical_name.endswith("dimensions"):
        result = _DIM_SEP_RE.sub(" × ", result)
        result = re.sub(r"(?<=\d)\.(?=\d)", ",", result)
    result = _NUMBER_UNIT_RE.sub(_unit_replacement, result)
    result = _BIT_RE.sub(lambda m: f"{m[1]}-бит", result)
    result = _MILLION_RE.sub(lambda m: f"{_decimal(m[1], language)} {_MAGNITUDES_RU[m[2].casefold()]}", result)
    if canonical_name == "fast_charging":
        result = re.sub(r"\bup to\b", "до", result, flags=re.I)
        result = re.sub(r"\bin about\b", "примерно за", result, flags=re.I)
    if canonical_name in {"rear_video_recording", "front_video_recording"}:
        result = re.sub(r"(?<=\d[Kk]) at ", ", ", result)
        result = re.sub(r"(?<=\dp) at ", ", ", result)
    if canonical_name in {"rear_camera", "front_camera"}:
        result = _CAMERA_ROLE_RE.sub(lambda m: _CAMERA_ROLES_RU[m["role"]], result)
    if canonical_name == "speakers":
        result = re.sub(r"\bStereo\b", "Стерео", re.sub(r"\bMono\b", "Моно", result))
    if canonical_name == "sim":
        for pattern, replacement in _SIM_PHRASES_RU:
            result = pattern.sub(replacement, result)
    # A bare decimal ("2.5") reads with a comma in RU; version-like values
    # ("Bluetooth 5.3", "USB Type-C 3.2", "Android 14") are left alone because
    # they are not a bare number.
    if re.fullmatch(r"\d+\.\d+", result.strip()):
        result = result.replace(".", ",")
    return result
