"""Stage 31.1: the two user-facing result languages (RU/EN) for the bot.

Only display text is localized here -- technical values, model numbers,
units, and source URLs are never translated (see bot.formatters /
bot.export, which pass those through untouched). Unknown canonical names
(discovered attributes) fall back to a title-cased rendering of the
canonical name in either language, since we have no translation for
extraction leftovers by construction.
"""

from __future__ import annotations

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
    "operating_system": _t("Операционная система", "Operating System"),
    "ports": _t("Порты", "Ports"),
    "wireless": _t("Беспроводные модули", "Wireless"),
}


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


def display_name(canonical_name: str, language: Language, *, fallback: str | None = None) -> str:
    """The localized label for a canonical field, in either language.

    ``fallback`` lets a caller that already has an upstream-computed name
    (core.profile._display_name's title-cased canonical_name, echoed onto
    ServiceAttribute.display_name) prefer it over the generic replace-and-
    strip fallback when no explicit translation is listed -- so an unlisted
    canonical field (a category we haven't localized yet, or a test fixture)
    keeps showing its existing name instead of a re-derived one.
    """
    catalog = ATTRIBUTE_DISPLAY_NAMES.get(canonical_name)
    if catalog:
        return translate(catalog, language)
    if fallback is not None:
        return fallback
    return _fallback_display_name(canonical_name)


def normalize_language(value: object) -> Language:
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def language_prompt() -> str:
    """Shown before every verification starts; language-neutral by design."""
    return " / ".join(LANGUAGE_PROMPT.values())
