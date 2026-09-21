"""Externally enforced wall-clock limit for one discovery call (Stage 36.7).

``DiscoveryDebugService`` budgets are cooperative: a provider that never
returns (a hung socket, a Playwright call, a slow browser shutdown) cannot be
stopped by a Python thread or ``ThreadPoolExecutor``.  ``run_isolated`` instead
runs the unchanged pipeline in a supervised child process, checkpoints every
provider attempt, result and fetched page to disk as it happens, and terminates
the child's whole process tree (Job Object / process group) at
``budget + grace``.

A hard stop is not a failure: the checkpoints are *replayed* through the very
same ``_discover_inline`` pipeline (same ranking, authority and identity
rules, no new trust logic) with unfinished work reported as a structured
``hard_deadline`` attempt.  Nothing found before the stalled call is lost.
"""

from __future__ import annotations

import base64
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import pickle
import shutil
import sys
import tempfile
import threading
import time
import traceback
from typing import Callable

from core.discovery import ProviderAttempt, ProviderQueryOutcome
from core.process_containment import ContainedProcess, pid_alive

POLL_SECONDS = 0.05
# After a complete result is checkpointed the worker only has to tear down its
# providers; this bounds how long we wait for that before killing the tree.
RESULT_EXIT_WAIT_SECONDS = 2.0
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SessionKey = tuple[str, tuple[str, ...]]


# --------------------------------------------------------------------------
# worker side: durable checkpoints
# --------------------------------------------------------------------------
class Recorder:
    """Append-only checkpoint log; each event survives a kill of the process."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._file = open(directory / "events.log", "ab", buffering=0)
        self._lock = threading.Lock()

    def _write(self, event: str, **fields: object) -> None:
        payload = pickle.dumps({"e": event, "t": time.time(), **fields}, protocol=4)
        line = base64.b64encode(payload) + b"\n"
        with self._lock:
            self._file.write(line)

    def session_event(self, kind: str, payload: object) -> None:
        self._write("session", kind=kind, payload=payload)

    def fetch_start(self, url: str) -> None:
        self._write("fetch_start", url=url)

    def fetch_done(self, url: str, page: object, record: dict) -> None:
        self._write("fetch_done", url=url, page=page, record=record)

    def document(self, url: str, document: object) -> None:
        try:
            pickle.dumps(document)
        except Exception:  # noqa: BLE001 - an unpicklable reader result is simply not replayed
            return
        self._write("document", url=url, document=document)

    def result_ready(self, result: object) -> None:
        tmp = self._dir / "result.pkl.tmp"
        tmp.write_bytes(pickle.dumps(result, protocol=4))
        os.replace(tmp, self._dir / "result.pkl")


def _load_hooks(spec: str | None) -> dict:
    if not spec:
        return {}
    module_name, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module_name), attr)() or {}


def _worker_main(directory: Path) -> int:
    args = json.loads((directory / "args.json").read_text(encoding="utf-8"))
    recorder = Recorder(directory)
    try:
        from services.discovery_debug import DiscoveryDebugService

        hooks = _load_hooks(args.get("worker_hooks"))
        # The parent's clock started before this interpreter did.
        remaining = max(0.5, args["budget_seconds"] - (time.time() - args["started_epoch"]))
        service = DiscoveryDebugService(
            wall_clock_budget_seconds=remaining, isolation="inline",
            providers=hooks.get("providers"), document_reader=hooks.get("document_reader"),
        )
        service._discover_inline(
            args["name"], market=args["market"], product_category=args["product_category"],
            recorder=recorder,
        )
    except BaseException:  # noqa: BLE001 - the parent reports it as a structured stop
        (directory / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        sys.stderr.flush()
        os._exit(3)
    sys.stdout.flush()
    sys.stderr.flush()
    # Skip interpreter/atexit teardown: the Playwright driver's pipe closing
    # after the result is saved is what produced the old post-run EPIPE.
    os._exit(0)


# --------------------------------------------------------------------------
# parent side: replaying checkpoints
# --------------------------------------------------------------------------
class ReplayState:
    """Everything the worker recorded before it finished or was killed."""

    def __init__(self) -> None:
        self.completed: dict[_SessionKey, list[ProviderQueryOutcome]] = {}
        self.inflight_key: _SessionKey | None = None
        self.inflight_attempts: list[ProviderAttempt] = []
        self.inflight_results: list[object] = []
        self.inflight_provider: tuple[str, float | None, float] | None = None
        self.pages: dict[str, object] = {}
        self.page_records: dict[str, dict] = {}
        self.fetch_started: dict[str, float] = {}
        self.documents: dict[str, object] = {}
        self.last_event_time: float | None = None
        self.completed_query_count = 0

    def apply(self, event: dict) -> None:
        self.last_event_time = event["t"]
        kind = event["e"]
        if kind == "session":
            sub, payload = event["kind"], event["payload"]
            if sub == "query_start":
                self.inflight_key = payload
                self.inflight_attempts, self.inflight_results = [], []
                self.inflight_provider = None
            elif sub == "provider_start":
                self.inflight_provider = (payload[0], payload[1], event["t"])
            elif sub == "attempts":
                self.inflight_attempts.extend(payload)
                self.inflight_provider = None
            elif sub == "results":
                self.inflight_results.extend(payload)
            elif sub == "query_end" and self.inflight_key is not None:
                self.completed.setdefault(self.inflight_key, []).append(ProviderQueryOutcome(
                    tuple(self.inflight_results), tuple(self.inflight_attempts),
                ))
                self.completed_query_count += 1
                self.inflight_key = None
                self.inflight_provider = None
        elif kind == "fetch_start":
            self.fetch_started[event["url"]] = event["t"]
        elif kind == "fetch_done":
            self.fetch_started.pop(event["url"], None)
            self.pages[event["url"]] = event["page"]
            self.page_records[event["url"]] = event["record"]
        elif kind == "document":
            self.documents[event["url"]] = event["document"]

    # -- what was in flight when the worker stopped -------------------------
    def stalled_call(self, now: float) -> dict[str, object]:
        if self.fetch_started:
            url, started = min(self.fetch_started.items(), key=lambda item: item[1])
            return {"kind": "page_fetch", "target": url, "stalled_seconds": round(now - started, 3)}
        if self.inflight_key is not None:
            if self.inflight_provider is not None:
                name, timeout, started = self.inflight_provider
                return {
                    "kind": "provider", "target": name, "query": self.inflight_key[0],
                    "provider_timeout_seconds": timeout,
                    "stalled_seconds": round(now - started, 3),
                }
            return {"kind": "provider", "target": "unknown", "query": self.inflight_key[0],
                    "stalled_seconds": None}
        return {"kind": "between_calls", "target": "", "stalled_seconds": None}

    def session(self) -> "_ReplaySession":
        return _ReplaySession(self)

    # -- pipeline replay hooks (see DiscoveryDebugService._discover_inline) --
    def page(self, url: str) -> object:
        return self.pages.get(url)

    def fetch_meta(self, url: str) -> dict[str, object]:
        record = self.page_records.get(url)
        if record is not None:
            return {"duration_seconds": record.get("duration_seconds", 0.0),
                    "attempts": record.get("attempts", [])}
        if url in self.fetch_started:
            return {"status": "hard_deadline", "duration_seconds": 0.0,
                    "attempts": [{"url": url, "status": "hard_deadline_in_flight"}]}
        return {"status": "hard_deadline", "duration_seconds": 0.0,
                "attempts": [{"url": url, "status": "not_started_before_hard_deadline"}]}

    def document(self, url: str) -> object:
        return self.documents.get(url)


class _ReplaySession:
    def __init__(self, state: ReplayState) -> None:
        self._state = state
        self._inflight_used = False

    def __enter__(self) -> "_ReplaySession":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def search_with_status(
        self, query: str, *, skip_providers: frozenset[str] = frozenset(),
    ) -> ProviderQueryOutcome:
        key = (query, tuple(sorted(skip_providers)))
        queue = self._state.completed.get(key)
        if queue:
            return queue.pop(0)
        if key == self._state.inflight_key and not self._inflight_used:
            self._inflight_used = True
            state = self._state
            stalled = state.stalled_call(time.time())
            provider = state.inflight_provider[0] if state.inflight_provider else "unknown"
            attempt = ProviderAttempt(
                provider=provider, query=query, status="timeout",
                message=f"Hard wall-clock limit reached while {provider} had not answered; "
                        "its process tree was terminated. Earlier results are retained.",
                timeout_seconds=stalled.get("provider_timeout_seconds"),
                duration_seconds=stalled.get("stalled_seconds") or 0.0,
                timed_out=True, budget_exhausted=True, failure_reason="hard_deadline",
            )
            return ProviderQueryOutcome(
                tuple(state.inflight_results), (*state.inflight_attempts, attempt),
            )
        return ProviderQueryOutcome((), (ProviderAttempt(
            provider="hard_deadline", query=query, status="timeout",
            message="Not started: hard wall-clock limit was reached first.",
            timed_out=True, budget_exhausted=True, failure_reason="hard_deadline",
        ),))


def read_events(path: Path) -> list[dict]:
    events: list[dict] = []
    if not path.is_file():
        return events
    for line in path.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        try:
            events.append(pickle.loads(base64.b64decode(line, validate=True)))
        except Exception:  # noqa: BLE001 - a line torn by the kill is skipped
            continue
    return events


# --------------------------------------------------------------------------
# parent side: supervision
# --------------------------------------------------------------------------
def _stderr_summary(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"epipe": False, "excerpt": ""}
    return {"epipe": "EPIPE" in text, "excerpt": text.strip()[-300:]}


def run_isolated(
    product_name: str, *, market: str, product_category: str, budget_seconds: float,
    grace_seconds: float, worker_hooks: str | None = None,
    on_worker_started: Callable[[ContainedProcess], None] | None = None,
):
    """Run one discovery under a hard external limit of ``budget + grace``."""
    from services.discovery_debug import DiscoveryDebugService, parse_discovery_product_name

    if parse_discovery_product_name(product_name) is None:
        raise ValueError("brand and model are required")
    started = time.monotonic()
    started_epoch = time.time()
    hard_limit = budget_seconds + grace_seconds
    directory = Path(tempfile.mkdtemp(prefix="pdv-discovery-"))
    (directory / "args.json").write_text(json.dumps({
        "name": product_name, "market": market, "product_category": product_category,
        "budget_seconds": budget_seconds, "started_epoch": started_epoch,
        "worker_hooks": worker_hooks,
    }), encoding="utf-8")
    env = dict(os.environ)
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PDV_DISCOVERY_WORKER="1")
    stdout = open(directory / "stdout.log", "wb")
    stderr = open(directory / "stderr.log", "wb")
    process: ContainedProcess | None = None
    try:
        process = ContainedProcess(
            [sys.executable, "-u", "-m", "services.discovery_isolation", str(directory)],
            cwd=str(_PROJECT_ROOT), env=env, stdout=stdout, stderr=stderr,
        )
        if on_worker_started is not None:
            on_worker_started(process)
        result_path = directory / "result.pkl"
        result_seen_at: float | None = None
        stop_reason: str | None = None
        while True:
            now = time.monotonic()
            if result_seen_at is None and result_path.is_file():
                result_seen_at = now
            if process.poll() is not None:
                break
            if result_seen_at is not None and now - result_seen_at >= RESULT_EXIT_WAIT_SECONDS:
                stop_reason = "cleanup_exceeded_grace"
                break
            if now - started >= hard_limit:
                stop_reason = "hard_deadline"
                break
            time.sleep(POLL_SECONDS)
        stop_at = time.monotonic()
        exit_code = process.poll()
        pids = process.descendant_pids()
        leftover = process.kill_tree()
        terminated = exit_code is None
        killed_at = time.monotonic()
        process.close()
        stdout.close()
        stderr.close()
        stderr_info = _stderr_summary(directory / "stderr.log")
        state = ReplayState()
        for event in read_events(directory / "events.log"):
            state.apply(event)
        error_text = ""
        if (directory / "error.txt").is_file():
            error_text = (directory / "error.txt").read_text(encoding="utf-8", errors="replace")

        result = None
        if result_path.is_file():
            try:
                result = pickle.loads(result_path.read_bytes())
            except Exception:  # noqa: BLE001 - fall back to replay of the checkpoints
                result = None
        complete = result is not None
        if not complete:
            if stop_reason is None:
                stop_reason = "worker_exited_without_result"
            stalled = state.stalled_call(time.time())
            service = DiscoveryDebugService(
                wall_clock_budget_seconds=budget_seconds, isolation="inline",
            )
            result = service._discover_inline(
                product_name, market=market, product_category=product_category, replay=state,
            )
        else:
            stalled = {"kind": "post_result_cleanup" if stop_reason else "none", "target": "",
                       "stalled_seconds": None}
        elapsed = round(time.monotonic() - started, 3)
        isolation = {
            "mode": "process",
            "hard_stop": not complete,
            "result_complete": complete,
            "stop_reason": stop_reason,
            "stopped_in": stalled,
            "budget_seconds": budget_seconds,
            "grace_seconds": grace_seconds,
            "hard_limit_seconds": hard_limit,
            "wall_seconds": elapsed,
            "worker_running_seconds": round(stop_at - started, 3),
            "kill_seconds": round(killed_at - stop_at, 3),
            "process_tree_terminated": terminated,
            "worker_exit_code": exit_code,
            "worker_error": error_text.strip().splitlines()[-1][:300] if error_text.strip() else "",
            "contained_process_count": len(pids),
            "contained_pids": sorted(pids),
            "leftover_process_count": len(leftover),
            "job_contained": process.contained,
            "worker_stderr_epipe": stderr_info["epipe"],
            "worker_stderr_excerpt": stderr_info["excerpt"],
            "queries_completed": state.completed_query_count,
            "pages_recorded": len(state.pages),
            "pages_in_flight": len(state.fetch_started),
        }
        performance = {
            **result.performance,
            "runtime_seconds": elapsed,
            "budget_overrun_seconds": round(max(0.0, elapsed - budget_seconds), 3),
            "timeout_can_interrupt_active_requests": True,
            "isolation": isolation,
        }
        return replace(result, runtime_seconds=elapsed, performance=performance)
    finally:
        if process is not None and process.poll() is None:  # defensive: never leak
            process.kill_tree()
            process.close()
        for handle in (stdout, stderr):
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(directory, ignore_errors=True)


__all__ = ["run_isolated", "Recorder", "ReplayState", "read_events", "pid_alive"]


if __name__ == "__main__":
    sys.exit(_worker_main(Path(sys.argv[1])))
