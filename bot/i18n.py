"""Stage 31.1: the two user-facing result languages (RU/EN) for the bot.

Only display text is localized here -- canonical identifiers and source
URLs are never translated.  Unknown canonical names are rendered by a
deterministic semantic-token fallback; if that fallback cannot translate a
label safely, the original technical label is preserved.
"""

from __future__ import annotations

import re

from typing import Literal


Language = Literal["ru", "en"]
LANGUAGES: tuple[Language, ...] = ("ru", "en")
DEFAULT_LANGUAGE: Language = "ru"

LANGUAGE_CHOICE_LABELS: dict[Language, str] = {
    "ru": "\U0001f1f7\U0001f1fa Русский",
    "en": "\U0001f1ec\U0001f1e7 English",
}


def _t(ru: str, en: str) -> dict[Language, str]:
    return {"ru": ru, "en": en}


LANGUAGE_PROMPT = _t(
    "На каком языке показать результат?",
    "Which language should the result be shown in?",
)

QUALITY_LABELS: dict[str, dict[Language, str]] = {
    "verified": _t("✅ Данные подтверждены", "✅ Data confirmed"),
    "partial": _t("\U0001f7e1 Данные подтверждены частично", "\U0001f7e1 Data partially confirmed"),
    "insufficient": _t("⚠️ Недостаточно данных для проверки", "⚠️ Not enough data to verify"),
    "conflicted": _t("❗ Источники расходятся по части характеристик", "❗ Sources disagree on some attributes"),
}

CONFIDENCE_LABELS: dict[str, dict[Language, str]] = {
    "high": _t("высокая", "high"),
    "medium": _t("средняя", "medium"),
    "low": _t("низкая", "low"),
}

ERROR_MESSAGES: dict[str, dict[Language, str]] = {
    "invalid_request": _t(
        "Не удалось распознать запрос: {message}",
        "Could not understand the request: {message}",
    ),
    "workflow_failure": _t(
        "Не удалось получить данные о товаре: {message}. Попробуйте ещё раз позже.",
        "Could not retrieve product data: {message}. Please try again later.",
    ),
    "internal_error": _t(
        "Произошла непредвиденная ошибка. Попробуйте позже или сообщите администратору.",
        "An unexpected error occurred. Please try again later or contact the administrator.",
    ),
}

JOB_STATE_LABELS: dict[str, dict[Language, str]] = {
    "queued": _t("в очереди", "queued"),
    "running": _t("выполняется", "running"),
    "completed": _t("завершено", "completed"),
    "failed": _t("не удалось", "failed"),
    "cancelled": _t("отменено", "cancelled"),
}

UI = {
    "official_source": _t("официальный источник", "official source"),
    "secondary_source": _t("сторонний источник", "third-party source"),
    "section_sources": _t("🔗 Источники", "🔗 Sources"),
    "photos_button": _t("📸 Скачать все фото", "📸 Download all photos"),
    "photos_prompt": _t(
        "Найдено фото: {count} (официальных: {official}, из сторонних источников: {secondary}).",
        "Photos found: {count} (official: {official}, third-party: {secondary}).",
    ),
    "photos_caption": _t("{kind} · {host}", "{kind} · {host}"),
    "found_summary": _t("Найдено: {found}/{total}", "Found: {found}/{total}"),
    "conflicts_warning": _t(
        "⚠️ Есть расхождения в данных: {fields}",
        "⚠️ Conflicting data: {fields}",
    ),
    "section_auxiliary": _t(
        "📎 Дополнительно ({kind}):", "📎 More ({kind}):",
    ),
    "aux_source": _t("Страница товара", "Product page"),
    "aux_review": _t("Обзор", "Review"),
    "aux_opinions": _t("Отзывы", "Opinions"),
    "aux_compare": _t("Сравнение", "Compare"),
    "aux_pictures": _t("Фотографии", "Pictures"),
    "aux_prices": _t("Цены", "Prices"),
    "photos_failed": _t(
        "Не удалось отправить фото, вот ссылки:",
        "Could not send the photos, here are the links:",
    ),
    "photos_expired": _t(
        "Фото больше недоступны. Запустите проверку заново.",
        "The photos are no longer available. Please run the verification again.",
    ),
    "section_confirmed": _t("✅ Подтверждено", "✅ Confirmed"),
    "section_conflicts": _t("❗ Конфликты", "❗ Conflicts"),
    "section_unresolved": _t("▫️ Не определено", "▫️ Unresolved"),
    "conflict_suffix": _t("данные расходятся", "sources disagree"),
    "unconfirmed_suffix": _t("не подтверждено", "unconfirmed"),
    "more_items": _t("… и ещё {count}", "… and {count} more"),
    "why_insufficient": _t("Почему данных недостаточно:", "Why the data is insufficient:"),
    "category_label": _t("Категория", "Category"),
    "quality_label": _t("Качество", "Quality"),
    "coverage_label": _t("Покрытие схемы", "Schema coverage"),
    "confirmed_summary": _t(
        "Подтверждено: {confirmed}/{total} • Не определено: {unresolved} • Конфликты: {conflicts}",
        "Confirmed: {confirmed}/{total} • Unresolved: {unresolved} • Conflicts: {conflicts}",
    ),
    "cache_indicator": _t("♻️ Результат из кэша ({age})", "♻️ Result from cache ({age})"),
    "age_seconds": _t("меньше минуты назад", "less than a minute ago"),
    "age_minutes": _t("{n} мин. назад", "{n} min ago"),
    "age_hours": _t("{n} ч. назад", "{n} h ago"),
    "age_days": _t("{n} дн. назад", "{n} d ago"),
    "accepted": _t("Принял запрос. Проверяю {product}…", "Got it. Verifying {product}…"),
    "started": _t("\U0001f504 Начинаю проверку {product}…", "\U0001f504 Starting verification of {product}…"),
    "duplicate": _t(
        "Запрос на {product} уже выполняется ({state}). Дождитесь результата или используйте /status.",
        "A request for {product} is already running ({state}). Wait for the result or use /status.",
    ),
    "status_none": _t("Активных задач нет.", "No active tasks."),
    "status_header": _t("Ваши активные задачи:", "Your active tasks:"),
    "cancelled_none": _t("Нет активных задач для отмены.", "No active tasks to cancel."),
    "cancelled_one": _t("Отменена 1 задача.", "Cancelled 1 task."),
    "cancelled_many": _t("Отменено задач: {count}.", "Cancelled tasks: {count}."),
    "start_message": _t(
        "Привет! Я проверяю характеристики товара по бренду и модели.\n\n"
        "Отправьте сообщение в формате:\n"
        "ExampleCo Model 200\n"
        "или\n"
        "ExampleCo | Model 200\n\n"
        "Проверка выполняется в фоне; я пришлю результат, когда он будет готов.\n"
        "Команда /help покажет подробности.",
        "Hi! I check a product's specs by brand and model.\n\n"
        "Send a message in the format:\n"
        "ExampleCo Model 200\n"
        "or\n"
        "ExampleCo | Model 200\n\n"
        "Verification runs in the background; I'll send the result once it's ready.\n"
        "Use /help for details.",
    ),
    "help_message": _t(
        "Формат запроса:\n"
        "<бренд> <модель>\n"
        "или\n"
        "<бренд> | <модель> | <артикул (необязательно)>\n\n"
        "Примеры:\n"
        "ExampleCo Model 200\n"
        "ExampleCo | Model 200\n"
        "ExampleCo | Model 200 | ART-7\n\n"
        "Проверка товара выполняется в фоне и может занять несколько минут; "
        "я пришлю сообщение, когда результат будет готов.\n\n"
        "Команды:\n"
        "/status — показать ваши текущие (queued/running) задачи\n"
        "/cancel — отменить ваши активные задачи\n"
        "/export — скачать CSV с результатом последней проверки",
        "Request format:\n"
        "<brand> <model>\n"
        "or\n"
        "<brand> | <model> | <article (optional)>\n\n"
        "Examples:\n"
        "ExampleCo Model 200\n"
        "ExampleCo | Model 200\n"
        "ExampleCo | Model 200 | ART-7\n\n"
        "Verification runs in the background and may take a few minutes; "
        "I'll send a message once the result is ready.\n\n"
        "Commands:\n"
        "/status — show your current (queued/running) tasks\n"
        "/cancel — cancel your active tasks\n"
        "/export — download a CSV with the last verification result",
    ),
    "parse_error": _t(
        "Не удалось понять запрос. Отправьте бренд и модель, например:\n"
        "ExampleCo Model 200\n"
        "или ExampleCo | Model 200",
        "Could not understand the request. Send a brand and model, for example:\n"
        "ExampleCo Model 200\n"
        "or ExampleCo | Model 200",
    ),
    "shutting_down": _t(
        "Бот перезапускается, попробуйте отправить запрос через минуту.",
        "The bot is restarting, please try sending the request again in a minute.",
    ),
    "not_found": _t("Не найдено", "Not found"),
    "export_no_result": _t(
        "У вас пока нет готового результата проверки для экспорта. "
        "Сначала отправьте запрос на проверку товара.",
        "You don't have a finished verification result to export yet. "
        "Send a product verification request first.",
    ),
}

IDENTITY_FIELD_LABELS: dict[str, dict[Language, str]] = {
    "brand": _t("бренд", "brand"),
    "model": _t("модель", "model"),
}

# Stage 31.1: the wide, one-product-per-row /export CSV's fixed leading
# columns (identity + source summary) -- canonical attribute columns are
# appended after these using ATTRIBUTE_DISPLAY_NAMES/display_name().
EXPORT_HEADERS: dict[str, dict[Language, str]] = {
    "brand": _t("Бренд", "Brand"),
    "model": _t("Модель", "Model"),
    "article": _t("Артикул", "Article"),
    "category": _t("Категория", "Category"),
    "official_sources": _t("Официальные источники", "Official Sources"),
    "secondary_sources": _t("Прочие источники", "Secondary Sources"),
}


def export_header(key: str, language: Language) -> str:
    return translate(EXPORT_HEADERS[key], language)

# Localized display names for canonical schema fields (core.schema). A
# canonical_name missing here falls back to a title-cased rendering of the
# name in the requested language's own convention (see
# bot.i18n.display_name), so an unlisted/discovered field never breaks.
ATTRIBUTE_DISPLAY_NAMES: dict[str, dict[Language, str]] = {
    "brand": _t("Бренд", "Brand"),
    "model": _t("Модель", "Model"),
    "color": _t("Цвет", "Color"),
    "manufacturer_article": _t("Артикул производителя", "Manufacturer Article"),
    "product_code": _t("Код товара", "Product Code"),
    "sku": _t("Артикул (SKU)", "SKU"),
    "gtin": _t("Штрихкод (GTIN)", "GTIN"),
    "country_of_origin": _t("Страна производства", "Country of Origin"),
    "warranty": _t("Гарантия", "Warranty"),
    "product_dimensions": _t("Габариты изделия", "Product Dimensions"),
    "package_dimensions": _t("Габариты упаковки", "Package Dimensions"),
    "net_weight": _t("Вес нетто", "Net Weight"),
    "gross_weight": _t("Вес брутто", "Gross Weight"),
    "package_contents": _t("Комплектация", "Package Contents"),
    "display_size": _t("Размер экрана", "Display Size"),
    "display_type": _t("Тип экрана", "Display Type"),
    "display_resolution": _t("Разрешение экрана", "Display Resolution"),
    "refresh_rate": _t("Частота обновления", "Refresh Rate"),
    "processor": _t("Процессор", "Processor"),
    "gpu": _t("Видеоядро (GPU)", "GPU"),
    "ram": _t("Оперативная память", "RAM"),
    "storage": _t("Встроенная память", "Storage"),
    "rear_camera": _t("Основная камера", "Rear Camera"),
    "front_camera": _t("Фронтальная камера", "Front Camera"),
    "battery_capacity": _t("Ёмкость аккумулятора", "Battery Capacity"),
    "charging_power": _t("Мощность зарядки", "Charging Power"),
    "wifi": _t("Wi-Fi", "Wi-Fi"),
    "bluetooth": _t("Bluetooth", "Bluetooth"),
    "sim": _t("SIM-карта", "SIM"),
    "ip_rating": _t("Класс защиты (IP)", "IP Rating"),
    "usb": _t("USB", "USB"),
    "operating_system": _t("Операционная система", "Operating System"),
    "ports": _t("Порты", "Ports"),
    "wireless": _t("Беспроводные модули", "Wireless"),
}


# Stage 32: names for canonical attributes added by the dynamic catalog
# (core.attribute_catalog). Keys are the language-neutral canonical names.
_EXTENSION_NAMES = {
    "display_ppi": ("Плотность пикселей", "Pixel density"),
    "display_aspect_ratio": ("Соотношение сторон экрана", "Display aspect ratio"),
    "cover_glass": ("Защитное стекло", "Cover glass"),
    "hdr_support": ("Поддержка HDR", "HDR support"),
    "hdr_brightness": ("Яркость в режиме HDR", "HDR brightness"),
    "peak_brightness": ("Пиковая яркость", "Peak brightness"),
    "contrast_ratio": ("Контрастность", "Contrast ratio"),
    "color_depth": ("Глубина цвета", "Color depth"),
    "number_of_colors": ("Количество цветов", "Number of colors"),
    "battery_capacity_min": ("Минимальная ёмкость аккумулятора", "Minimum battery capacity"),
    "fast_charging": ("Быстрая зарядка", "Fast charging"),
    "wireless_charging": ("Беспроводная зарядка", "Wireless charging"),
    "wireless_charging_standard": ("Стандарт беспроводной зарядки", "Wireless charging standard"),
    "battery_life": ("Время работы от аккумулятора", "Battery life"),
    "battery_life_power_saving": ("Время работы в режиме экономии", "Battery life (power saving)"),
    "battery_features": ("Функции аккумулятора", "Battery features"),
    "security_coprocessor": ("Сопроцессор безопасности", "Security coprocessor"),
    "security_features": ("Функции защиты данных", "Security features"),
    "software_update_support": ("Срок поддержки обновлений", "Software update support"),
    "max_zoom": ("Максимальный зум", "Max zoom"),
    "rear_camera_features": ("Функции основной камеры", "Rear camera features"),
    "front_camera_features": ("Функции фронтальной камеры", "Front camera features"),
    "front_camera_aperture": ("Диафрагма фронтальной камеры", "Front camera aperture"),
    "front_camera_field_of_view": ("Угол обзора фронтальной камеры", "Front camera field of view"),
    "front_camera_autofocus": ("Автофокус фронтальной камеры", "Front camera autofocus"),
    "camera_features": ("Функции камеры", "Camera features"),
    "editing_features": ("Функции редактирования фото", "Photo editing features"),
    "rear_video_recording": ("Видеозапись основной камерой", "Rear video recording"),
    "front_video_recording": ("Видеозапись фронтальной камерой", "Front video recording"),
    "rear_video_features": ("Видеофункции основной камеры", "Rear video features"),
    "front_video_features": ("Видеофункции фронтальной камеры", "Front video features"),
    "slow_motion_video": ("Замедленная съёмка", "Slow-motion video"),
    "hdr_video_recording": ("Запись HDR-видео", "HDR video recording"),
    "video_formats": ("Форматы видео", "Video formats"),
    "video_features": ("Функции видео", "Video features"),
    "video_audio_features": ("Функции записи звука", "Video audio features"),
    "materials_and_durability": ("Материалы и прочность", "Materials and durability"),
    "authentication": ("Аутентификация", "Authentication"),
    "safety_features": ("Экстренные функции и безопасность", "Safety features"),
    "sensors": ("Датчики", "Sensors"),
    "buttons": ("Кнопки", "Buttons"),
    "speakers": ("Динамики", "Speakers"),
    "microphone_count": ("Количество микрофонов", "Microphones"),
    "media_features": ("Аудиофункции", "Audio features"),
    "wifi_bands": ("Диапазоны Wi-Fi", "Wi-Fi bands"),
    "wifi_mimo": ("Wi-Fi MIMO", "Wi-Fi MIMO"),
    "nfc": ("NFC", "NFC"),
    "uwb": ("Сверхширокополосная связь (UWB)", "Ultra-wideband (UWB)"),
    "gnss": ("Спутниковая навигация", "GNSS / positioning"),
    "gnss_dual_band": ("Двухдиапазонный GNSS", "Dual-band GNSS"),
    "wireless_features": ("Беспроводные функции", "Wireless features"),
    "esim": ("eSIM", "eSIM"),
    "network_generations": ("Поколения сети", "Network generations"),
    "network_5g_type": ("Тип 5G", "5G type"),
    "model_number": ("Номер модели", "Model number"),
    "gsm_bands": ("Диапазоны GSM", "GSM bands"),
    "umts_bands": ("Диапазоны UMTS/HSPA", "UMTS/HSPA bands"),
    "lte_bands": ("Диапазоны LTE", "LTE bands"),
    "nr_sub6_bands": ("Диапазоны 5G Sub-6", "5G Sub-6 bands"),
    "nr_mmwave_bands": ("Диапазоны 5G mmWave", "5G mmWave bands"),
    "hearing_aid_compatible": ("Совместимость со слуховыми аппаратами", "Hearing aid compatible"),
    "conversational_gain": ("Усиление речи (conversational gain)", "Conversational gain"),
    "accessibility_features": ("Специальные возможности", "Accessibility features"),
}
_SMALL_WORDS = {"of", "and", "for", "with", "per", "in"}


def _title_en(text: str) -> str:
    """Title Case like the existing English attribute names; acronyms and
    mixed-case words (HDR, GNSS, eSIM, Wi-Fi) are kept as written."""
    words = []
    for index, word in enumerate(text.split(" ")):
        if index and word in _SMALL_WORDS:
            words.append(word)
        elif any(char.isupper() for char in word[1:]) or word[:1].isdigit():
            words.append(word)
        else:
            stripped = word.lstrip("(")
            words.append(word[:len(word) - len(stripped)] + stripped[:1].upper() + stripped[1:])
    return " ".join(words)


ATTRIBUTE_DISPLAY_NAMES.update({key: _t(ru, _title_en(en)) for key, (ru, en) in _EXTENSION_NAMES.items()})

# Per-lens attributes ("wide_camera_aperture") are composed, not listed.
_LENS_ROLE_NAMES = {
    "wide": ("широкоугольной камеры", "Wide camera"),
    "ultrawide": ("сверхширокоугольной камеры", "Ultrawide camera"),
    "telephoto": ("телеобъектива", "Telephoto camera"),
    "periscope": ("перископной камеры", "Periscope camera"),
    "macro": ("макрокамеры", "Macro camera"),
    "main": ("основной камеры", "Main camera"),
    "depth": ("датчика глубины", "Depth camera"),
}
_LENS_PART_NAMES = {
    "aperture": ("Диафрагма", "aperture"),
    "field_of_view": ("Угол обзора", "field of view"),
    "sensor_size": ("Размер сенсора", "sensor size"),
    "autofocus": ("Автофокус", "autofocus"),
    "optical_zoom": ("Оптический зум", "optical zoom"),
    "phase_detection": ("Фазовый автофокус", "phase detection"),
}
_LENS_ATTRIBUTE_RE = re.compile(
    r"^(?P<role>" + "|".join(_LENS_ROLE_NAMES) + r")_camera_(?P<part>" + "|".join(_LENS_PART_NAMES) + r")$"
)


def _lens_attribute_name(canonical_name: str, language: "Language") -> str | None:
    match = _LENS_ATTRIBUTE_RE.match(canonical_name)
    if not match:
        return None
    role_ru, role_en = _LENS_ROLE_NAMES[match["role"]]
    part_ru, part_en = _LENS_PART_NAMES[match["part"]]
    return f"{part_ru} {role_ru}" if language == "ru" else _title_en(f"{role_en} {part_en}")


def translate(catalog: dict[str, str], language: Language) -> str:
    return catalog.get(language, catalog.get(DEFAULT_LANGUAGE, ""))


def ui(key: str, language: Language, **kwargs: object) -> str:
    template = translate(UI[key], language)
    return template.format(**kwargs) if kwargs else template


def quality_label(status: str, language: Language) -> str:
    catalog = QUALITY_LABELS.get(status)
    return translate(catalog, language) if catalog else status


def confidence_label(value: str, language: Language) -> str:
    catalog = CONFIDENCE_LABELS.get(value)
    return translate(catalog, language) if catalog else value


def job_state_label(state: str, language: Language) -> str:
    catalog = JOB_STATE_LABELS.get(state)
    return translate(catalog, language) if catalog else state


def error_template(kind: str, language: Language) -> str:
    catalog = ERROR_MESSAGES.get(kind, ERROR_MESSAGES["internal_error"])
    return translate(catalog, language)


def identity_field_label(field: str, language: Language) -> str | None:
    catalog = IDENTITY_FIELD_LABELS.get(field)
    return translate(catalog, language) if catalog else None


def _fallback_display_name(canonical_name: str) -> str:
    return canonical_name.replace("_", " ").strip() or canonical_name


# Stage 32.1: aliases which are useful outside any one product/category but
# do not belong to the static schema catalog.  This is the second lookup tier
# after ATTRIBUTE_DISPLAY_NAMES and before token composition.  It deliberately
# contains concepts, not Pixel values.
_CANONICAL_ALIAS_DISPLAY_NAMES: dict[str, dict[Language, str]] = {
    "haptic_engine": _t("Вибромотор", "Haptic Engine"),
    "haptic_levels": _t("Уровни виброотклика", "Haptic Levels"),
    "camera_sensor_shift": _t("Сдвиг сенсора камеры", "Camera Sensor Shift"),
}

# Deterministic RU fallback for future dynamic attributes.  We only compose a
# label when every semantic token is known; partial translations such as
# "Датчик calibration" are worse than retaining the original technical name.
_RU_LABEL_TOKENS = {
    "adaptive": "адаптивный",
    "audio": "аудио",
    "battery": "аккумулятор",
    "camera": "камера",
    "charging": "зарядка",
    "color": "цвет",
    "depth": "глубина",
    "display": "экран",
    "engine": "механизм",
    "feature": "функция",
    "features": "функции",
    "front": "фронтальная",
    "haptic": "тактильный",
    "level": "уровень",
    "levels": "уровни",
    "material": "материал",
    "materials": "материалы",
    "maximum": "максимальный",
    "minimum": "минимальный",
    "mode": "режим",
    "modes": "режимы",
    "network": "сеть",
    "power": "мощность",
    "rear": "основная",
    "sensor": "датчик",
    "sensors": "датчики",
    "shift": "сдвиг",
    "size": "размер",
    "speed": "скорость",
    "support": "поддержка",
    "technology": "технология",
    "type": "тип",
    "video": "видео",
    "voltage": "напряжение",
    "wireless": "беспроводная",
}
_RU_LABEL_STEMS_GENITIVE = {
    "audio": "аудио",
    "battery": "аккумулятора",
    "camera": "камеры",
    "camera_sensor": "сенсора камеры",
    "charging": "зарядки",
    "color": "цвета",
    "display": "экрана",
    "haptic": "виброотклика",
    "material": "материала",
    "network": "сети",
    "sensor": "датчика",
    "video": "видео",
    "wireless_charging": "беспроводной зарядки",
}
_RU_LABEL_SUFFIXES = {
    "features": "Функции",
    "levels": "Уровни",
    "mode": "Режим",
    "modes": "Режимы",
    "power": "Мощность",
    "size": "Размер",
    "speed": "Скорость",
    "support": "Поддержка",
    "type": "Тип",
}


def _generic_display_name(canonical_name: str, language: Language) -> str | None:
    technical = _fallback_display_name(canonical_name)
    if language == "en":
        return _title_en(technical)
    tokens = [token for token in canonical_name.strip("_").split("_") if token]
    if not tokens or any(token not in _RU_LABEL_TOKENS for token in tokens):
        return None
    if len(tokens) == 1:
        rendered = _RU_LABEL_TOKENS[tokens[0]]
        return rendered[:1].upper() + rendered[1:]
    stem = "_".join(tokens[:-1])
    if stem in _RU_LABEL_STEMS_GENITIVE and tokens[-1] in _RU_LABEL_SUFFIXES:
        return f"{_RU_LABEL_SUFFIXES[tokens[-1]]} {_RU_LABEL_STEMS_GENITIVE[stem]}"
    return None


def display_name(canonical_name: str, language: Language, *, fallback: str | None = None) -> str:
    """The localized label for a canonical field, in either language.

    Resolution order is intentionally stable: explicit schema catalog,
    composed catalog aliases (for example per-lens fields), semantic dynamic
    aliases, deterministic token fallback, then the original technical label.
    """
    catalog = ATTRIBUTE_DISPLAY_NAMES.get(canonical_name)
    if catalog:
        return translate(catalog, language)
    if (composed := _lens_attribute_name(canonical_name, language)) is not None:
        return composed
    alias = _CANONICAL_ALIAS_DISPLAY_NAMES.get(canonical_name)
    if alias:
        return translate(alias, language)
    if (generic := _generic_display_name(canonical_name, language)) is not None:
        return generic
    return fallback if fallback is not None else _fallback_display_name(canonical_name)


def normalize_language(value: object) -> Language:
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def language_prompt() -> str:
    """Shown before every verification starts; language-neutral by design."""
    return " / ".join(LANGUAGE_PROMPT.values())
