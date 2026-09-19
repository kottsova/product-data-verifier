"""Stage 33.0 discovery-only application service.

This boundary deliberately exposes only source discovery.  It never imports
or invokes fetching, extraction, schema, validation, export, or localization.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import threading
import time
from typing import Literal

from core.budget import WallClockBudget
from core.discovery import (
    Candidate,
    DiscoveryOutcome,
    DiscoveryRuntimeConfig,
    ResilientSearchSession,
    discover_with_status,
)


DiscoveryGroup = Literal["official", "dealer", "secondary", "rejected"]
DisplayMatch = Literal["exact", "probable", "weak", "rejected"]


@dataclass(frozen=True, slots=True)
class DiscoverySource:
    url: str
    domain: str
    source_type: str
    title: str
    model_match: DisplayMatch
    authority: str
    reason: str
    group: DiscoveryGroup

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DiscoveryDebugResult:
    product_name: str
    brand: str
    model: str
    market: str
    status: Literal["PASS", "PARTIAL", "FAIL"]
    exact_official_found: bool
    official: tuple[DiscoverySource, ...]
    dealers: tuple[DiscoverySource, ...]
    secondary: tuple[DiscoverySource, ...]
    rejected: tuple[DiscoverySource, ...]
    runtime_seconds: float
    search_status: str
    attempted_queries: tuple[str, ...]
    providers: tuple[str, ...]
    provider_failures: tuple[dict[str, object], ...]
    candidate_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["official"] = [item.to_dict() for item in self.official]
        data["dealers"] = [item.to_dict() for item in self.dealers]
        data["secondary"] = [item.to_dict() for item in self.secondary]
        data["rejected"] = [item.to_dict() for item in self.rejected]
        return data


def parse_discovery_product_name(text: str) -> tuple[str, str] | None:
    """Resolve a name-only input into a conservative brand/model split.

    Pipe syntax remains available for arbitrary multi-word brands.  Plain text
    handles leading ``The`` brands and ampersand brand names generically; all
    other names follow the bot's established first-token brand convention.
    """
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    if "|" in normalized:
        parts = [" ".join(part.split()) for part in normalized.split("|")]
        if len(parts) >= 2 and parts[0] and parts[1]:
            return parts[0], parts[1]
        return None
    tokens = normalized.split()
    if len(tokens) < 2:
        return None
    brand_width = 1
    if tokens[0].casefold() == "the" and len(tokens) >= 3:
        brand_width = 2
    elif len(tokens) >= 4 and tokens[1] == "&":
        brand_width = 3
    return " ".join(tokens[:brand_width]), " ".join(tokens[brand_width:])


def _display_match(candidate: Candidate, *, rejected: bool = False) -> DisplayMatch:
    if rejected:
        return "rejected"
    relation = str(candidate.get("relevance_relation") or "")
    if relation == "exact":
        return "exact"
    if relation == "likely_variant":
        return "probable"
    return "weak"


def _authority(candidate: Candidate) -> tuple[str, DiscoveryGroup]:
    source_type = str(candidate.get("source_type") or "other")
    verified = candidate.get("authority_status") == "verified"
    if verified and source_type == "official_document":
        return "official support", "official"
    if verified and source_type == "manufacturer":
        return "manufacturer", "official"
    if verified and source_type == "retailer":
        return "authorized dealer", "dealer"
    return "secondary", "secondary"


def _candidate_view(candidate: Candidate, *, rejected: bool = False) -> DiscoverySource:
    authority, group = _authority(candidate)
    if rejected:
        group = "rejected"
    reasons = list(candidate.get("relevance_reasons") or ())
    authority_reason = str(candidate.get("authority_reason") or "").strip()
    if authority_reason:
        reasons.append(authority_reason)
    if not reasons:
        reasons.append(str(candidate.get("product_match_evidence") or "candidate retained"))
    return DiscoverySource(
        url=str(candidate.get("url") or ""),
        domain=str(candidate.get("domain") or ""),
        source_type=str(candidate.get("source_type") or "other"),
        title=str(candidate.get("title") or ""),
        model_match=_display_match(candidate, rejected=rejected),
        authority=authority,
        reason="; ".join(dict.fromkeys(reason for reason in reasons if reason)),
        group=group,
    )


def _result_from_outcome(
    product_name: str,
    brand: str,
    model: str,
    market: str,
    outcome: DiscoveryOutcome,
    runtime_seconds: float,
) -> DiscoveryDebugResult:
    grouped: dict[DiscoveryGroup, list[DiscoverySource]] = {
        "official": [], "dealer": [], "secondary": [], "rejected": [],
    }
    for candidate in outcome.candidates:
        view = _candidate_view(candidate)
        grouped[view.group].append(view)
    for candidate in outcome.rejected_candidates:
        grouped["rejected"].append(_candidate_view(candidate, rejected=True))

    known_rejected_urls = {item.url for item in grouped["rejected"]}
    for entry in outcome.trace.entries:
        if entry.outcome != "rejected" or not entry.canonical_url:
            continue
        if entry.canonical_url in known_rejected_urls:
            continue
        known_rejected_urls.add(entry.canonical_url)
        domain = entry.canonical_url.split("/", 3)[2] if "/" in entry.canonical_url else ""
        grouped["rejected"].append(DiscoverySource(
            entry.canonical_url, domain, "other", entry.title, "rejected",
            "secondary", entry.reason, "rejected",
        ))

    exact_official = any(item.model_match == "exact" for item in grouped["official"])
    if exact_official:
        status: Literal["PASS", "PARTIAL", "FAIL"] = "PASS"
    elif grouped["official"] or any(item.model_match == "exact" for item in grouped["secondary"]):
        status = "PARTIAL"
    else:
        status = "FAIL"

    attempts = outcome.provider_attempts
    providers = tuple(dict.fromkeys(item.provider for item in attempts))
    failures = tuple({
        "provider": item.provider,
        "query": item.query,
        "status": item.status,
        "message": item.message,
        "duration_seconds": item.duration_seconds,
    } for item in attempts if item.status != "success")
    trace = outcome.trace
    return DiscoveryDebugResult(
        product_name=product_name,
        brand=brand,
        model=model,
        market=market,
        status=status,
        exact_official_found=exact_official,
        official=tuple(grouped["official"]),
        dealers=tuple(grouped["dealer"]),
        secondary=tuple(grouped["secondary"]),
        rejected=tuple(grouped["rejected"]),
        runtime_seconds=round(runtime_seconds, 3),
        search_status=outcome.search_status,
        attempted_queries=tuple(outcome.attempted_queries),
        providers=providers,
        provider_failures=failures,
        candidate_counts={
            "provider_raw": trace.provider_raw_result_count,
            "collected": trace.collected_result_count,
            "normalized": trace.normalized_result_count,
            "unique": trace.unique_candidate_count,
            "duplicates": trace.duplicate_count,
            "accepted": trace.accepted_candidate_count,
            "rejected": trace.rejected_candidate_count,
            "official": len(grouped["official"]),
            "dealer": len(grouped["dealer"]),
            "secondary": len(grouped["secondary"]),
        },
    )


class DiscoveryDebugService:
    """Synchronous discovery service shared by Telegram and diagnostics."""

    def __init__(self, *, wall_clock_budget_seconds: float = 60.0) -> None:
        if wall_clock_budget_seconds <= 0:
            raise ValueError("wall_clock_budget_seconds must be positive")
        self.wall_clock_budget_seconds = float(wall_clock_budget_seconds)
        self._last_by_chat: dict[int, DiscoveryDebugResult] = {}
        self._lock = threading.Lock()

    def discover_name(
        self, product_name: str, *, market: str = "global", chat_id: int | None = None,
    ) -> DiscoveryDebugResult:
        identity = parse_discovery_product_name(product_name)
        if identity is None:
            raise ValueError("brand and model are required")
        brand, model = identity
        started = time.monotonic()
        budget = WallClockBudget(self.wall_clock_budget_seconds)
        runtime = DiscoveryRuntimeConfig()
        with ResilientSearchSession(market, config=runtime, budget=budget) as session:
            outcome = discover_with_status(
                brand, model, market=market, searcher=session.search_with_status,
            )
        result = _result_from_outcome(
            " ".join(product_name.split()), brand, model, market, outcome,
            time.monotonic() - started,
        )
        if chat_id is not None:
            with self._lock:
                self._last_by_chat[chat_id] = result
        return result

    def last_result(self, chat_id: int) -> DiscoveryDebugResult | None:
        with self._lock:
            return self._last_by_chat.get(chat_id)
