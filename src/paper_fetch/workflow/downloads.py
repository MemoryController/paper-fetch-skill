"""Independent orchestration for validated original publisher files."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
import hashlib
import time
from pathlib import Path
from collections.abc import Iterator, Mapping
from typing import Any

from ..artifacts import ArtifactStore
from ..http import RequestCancelledError, RequestFailure
from ..http.cache import redact_text_for_diagnostics, redact_url_for_diagnostics
from ..providers.base import ProviderFailure
from ..providers.browser_runtime import BrowserRuntimeFailure
from ..resolve.query import StructuredResolveRequest
from ..runtime import RuntimeContext
from ..tracing import TraceContext, TraceEvent, project_source_trail_trace, trace_event
from ..utils import sanitize_filename
from .acceptance import evaluate_download_acceptance
from .metadata import fetch_metadata_for_resolved_query
from .resolution import resolve_paper
from .routing import build_official_provider_candidate_evidence
from .types import FetchStrategy, PaperFetchFailure


def public_download_data(value: Any) -> Any:
    """Remove internal authorization state and redact diagnostic strings."""
    if isinstance(value, Mapping):
        return {
            str(k): public_download_data(v)
            for k, v in value.items()
            if str(k).lower()
            not in {
                "browser_context_seed",
                "storage_state",
                "cookies",
                "cookie",
                "token",
                "authorization",
                "headers",
            }
        }
    if isinstance(value, (list, tuple)):
        return [public_download_data(v) for v in value]
    if isinstance(value, str):
        return (
            redact_url_for_diagnostics(value)
            if value.startswith(("https://", "http://"))
            else redact_text_for_diagnostics(value)
        )
    return value


_FILE_FIELDS = {
    "id",
    "status",
    "path",
    "size_bytes",
    "sha256",
    "content_type",
    "format",
    "declared_format",
    "provider",
    "route",
    "source_url",
    "final_url",
    "code",
    "reason",
    "stage",
    "error_category",
    "http_status",
}


def _file_result(item: Mapping[str, Any], *, role: str) -> dict[str, Any]:
    result = {
        key: item[key]
        for key in _FILE_FIELDS
        if key in item and isinstance(item[key], (str, int, type(None)))
    }
    result["role"] = role
    validation = item.get("validation") or {}
    result["validation"] = {
        key: validation[key]
        for key in ("format", "structure", "size", "source", "role", "identity")
        if key in validation
        and (validation[key] is None or isinstance(validation[key], bool))
    }
    if result.get("status") not in {"downloaded", "failed", "skipped"}:
        result["status"] = "failed"
        result.setdefault("code", "invalid_terminal_status")
    if result.get("status") != "downloaded":
        for key in ("path", "size_bytes", "sha256"):
            result.pop(key, None)
    return public_download_data(result)


def _failed(
    asset: Mapping[str, Any], code: str, *, failure: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return _file_result(
        {
            "id": asset.get("id"),
            "status": "failed",
            "code": code,
            "reason": "Supplementary file was not published.",
            "source_url": asset.get("source_url") or asset.get("url"),
            **dict(failure or {}),
        },
        role="supplementary",
    )


def _remaining_budget(context: RuntimeContext) -> float | None:
    deadline = context.deadline_monotonic
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _phase_failure(
    error: Exception, context: RuntimeContext, stage: str
) -> dict[str, Any]:
    # Do not expose arbitrary exception repr/details (which can contain auth state).
    info: dict[str, Any] = {
        "stage": stage,
        "code": "supplementary_internal_error",
        "reason": "Unexpected failure in original supplementary processing.",
        "error_category": "internal_error",
    }
    if isinstance(error, RequestCancelledError):
        info.update(code="request_cancelled", error_category="cancelled")
    elif isinstance(error, ProviderFailure):
        info.update(
            code=error.code,
            reason=error.message,
            error_category=error.error_category or error.code,
            http_status=error.http_status,
        )
    elif isinstance(error, BrowserRuntimeFailure):
        info.update(code=error.kind, reason=error.message, error_category=error.kind)
        trace = error.details.get("trace")
        if (
            error.kind == "browser_connect_timeout"
            and isinstance(trace, Mapping)
            and trace.get("deadline_exhausted") is True
            and _remaining_budget(context) == 0
        ):
            info.update(
                code="request_deadline_exceeded",
                error_category="timeout",
                reason="Request budget was exhausted before supplementary navigation.",
            )
    elif isinstance(error, RequestFailure):
        category = str(error.error_category or "http_error")
        info.update(
            code=error.reason_code or category,
            reason=str(error),
            error_category=category,
            http_status=error.status_code,
        )
    elif isinstance(error, TimeoutError):
        info.update(
            code="request_deadline_exceeded"
            if _remaining_budget(context) == 0
            else "supplementary_timeout",
            reason="Original supplementary processing timed out.",
            error_category="timeout",
        )
    return public_download_data(info)


@contextmanager
def _download_phase(
    context: RuntimeContext,
    provider: str,
    stage: str,
    phases: dict[str, Any],
) -> Iterator[None]:
    started, started_at = time.monotonic(), time.time()
    before = _remaining_budget(context)
    failure: dict[str, Any] = {}
    try:
        yield
    except Exception as error:
        failure = _phase_failure(error, context, stage)
        raise
    finally:
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        phases[stage] = {
            "duration_ms": elapsed_ms,
            "remaining_before_seconds": before,
            "remaining_after_seconds": _remaining_budget(context),
        }
        context.fetch_trace.append(
            trace_event(
                "download",
                stage,
                "fail" if failure else "ok",
                code=failure.get("code"),
                message=failure.get("reason"),
                context=TraceContext(
                    provider=provider,
                    route=None if stage == "primary_pdf" else "assets",
                    error_category=failure.get("error_category"),
                    http_status=failure.get("http_status"),
                    started_at=started_at,
                    finished_at=time.time(),
                    duration_ms=elapsed_ms,
                ),
            )
        )


def download_paper_pdfs(
    query: str | StructuredResolveRequest,
    *,
    output_dir: str | Path,
    include_si: bool = True,
    context: RuntimeContext,
) -> dict[str, Any]:
    if output_dir is None or not str(output_dir).strip():
        raise ValueError("output_dir is required")
    from ..providers.original_downloads import (
        download_primary_pdf,
        discover_supplementary_files,
        download_supplementary_files,
    )

    context.raise_if_cancelled()
    resolved = resolve_paper(query, context=context)
    if not resolved.doi:
        raise PaperFetchFailure(
            "ambiguous" if resolved.candidates else "not_supported",
            "A verified DOI is required.",
            candidates=resolved.candidates,
        )
    clients = dict(context.get_clients())
    strategy = FetchStrategy()
    metadata, _, source_trail = fetch_metadata_for_resolved_query(
        resolved, clients=clients, strategy=strategy, context=context
    )
    # DOI digest prevents distinct identities colliding after filename sanitization.
    root = Path(output_dir).expanduser().resolve()
    destination = root / (
        sanitize_filename(resolved.doi)[:100]
        + "_"
        + hashlib.sha256(resolved.doi.lower().encode()).hexdigest()[:12]
    )
    context.download_dir = destination
    context.artifact_store = ArtifactStore.from_download_dir(
        destination,
        artifact_mode=context.artifact_mode,
        commit_guard=context.commit_guard,
    )
    candidates = build_official_provider_candidate_evidence(
        resolved, routing_metadata=metadata, strategy=strategy
    )
    result = None
    selected_provider = None
    last_error = None
    phases: dict[str, Any] = {}
    for candidate in candidates:
        client = clients.get(candidate.provider)
        if client is None:
            continue
        context.raise_if_cancelled()
        try:
            with _download_phase(context, candidate.provider, "primary_pdf", phases):
                result = download_primary_pdf(
                    client, resolved.doi, metadata, context=context
                )
            selected_provider = candidate.provider
            break
        except NotImplementedError:
            source_trail.append(f"download:{candidate.provider}_not_supported")
            continue
        except ProviderFailure as error:
            source_trail.append(f"download:{candidate.provider}_{error.code}")
            last_error = error.with_updates(
                source_trail=source_trail + error.source_trail,
                trace=project_source_trail_trace(
                    source_trail, context.fetch_trace + error.trace
                ),
            )
            if error.code == "no_access" and candidate.strength == "strong":
                raise last_error from error
    if result is None:
        if last_error is not None:
            raise last_error
        raise PaperFetchFailure(
            "not_supported",
            "No existing provider supports an original PDF route.",
            source_trail=source_trail,
        )
    assert selected_provider is not None
    data = result.pdf_bytes
    facts = result.diagnostics
    validation = facts.get("validation") or {}
    digest = hashlib.sha256(data).hexdigest()
    if (
        not data
        or result.markdown_text
        or not all(
            validation.get(key) is True
            for key in ("format", "structure", "size", "role", "identity")
        )
        or facts.get("pdf_sha256") != digest
        or facts.get("pdf_bytes") != len(data)
        or not facts.get("route")
        or not facts.get("pdf_pages")
        or not facts.get("identity")
    ):
        raise PaperFetchFailure(
            "error",
            "Missing or inconsistent original PDF validation facts.",
            source_trail=source_trail,
        )
    filename = (
        sanitize_filename(Path(result.suggested_filename or "main.pdf").stem) + ".pdf"
    )
    target = destination / filename
    try:
        context.artifact_store.write_bytes_file(
            target, data, overwrite=False, commit_guard=context.commit_guard
        )
    except FileExistsError as error:
        raise PaperFetchFailure(
            "artifact_conflict",
            "Refusing to overwrite different original PDF bytes.",
            source_trail=source_trail,
        ) from error
    source_trail.append(f"download:{selected_provider}_primary_pdf_ok")
    main = _file_result(
        dict(
            id="main_pdf",
            status="downloaded",
            path=str(target),
            size_bytes=len(data),
            sha256=digest,
            provider=selected_provider,
            route=facts["route"],
            source_url=result.source_url,
            final_url=result.final_url,
            format="pdf",
            declared_format="pdf",
            content_type="application/pdf",
            validation=validation,
        ),
        role="main_pdf",
    )
    discovery_public: dict[str, Any] = {"status": "not_requested"}
    items = []
    if include_si:
        discovered = []
        stage = "si_discovery"
        try:
            with _download_phase(context, selected_provider, stage, phases):
                discovery = discover_supplementary_files(
                    clients[selected_provider], resolved.doi, metadata, context=context
                )
                discovered = [dict(asset) for asset in discovery.get("assets") or []]
                for index, asset in enumerate(discovered):
                    if not asset.get("id"):
                        asset["id"] = "unidentified_" + str(index + 1)
                if any(
                    str(asset["id"]).startswith("unidentified_") for asset in discovered
                ):
                    raise ValueError("Missing stable supplementary identity")
                discovery_public = {
                    "status": "unsupported"
                    if discovery["status"] == "not_supported"
                    else "ok"
                    if discovered
                    else "not_found",
                    "source_url": discovery.get("source_url"),
                }
            if discovery["status"] == "ok" and discovered:
                stage = "si_download"
                with _download_phase(context, selected_provider, stage, phases):
                    downloaded = download_supplementary_files(
                        clients[selected_provider],
                        resolved.doi,
                        metadata,
                        discovery,
                        destination,
                        context=context,
                    )
                by_id = {
                    str(item.get("id")): item
                    for item in list(downloaded["assets"])
                    + list(downloaded["asset_failures"])
                }
                for asset in discovered:
                    item = by_id.get(str(asset.get("id")))
                    if item is None:
                        items.append(_failed(asset, "missing_terminal_result"))
                        continue
                    item = _file_result(item, role="supplementary")
                    if item.get("status") == "downloaded":
                        path = Path(item.get("path") or "").resolve()
                        valid = item["validation"]
                        try:
                            if not path.is_relative_to(destination) or not all(
                                valid.get(k) is True
                                for k in ("format", "size", "source")
                            ):
                                raise ValueError("invalid published facts")
                            if (
                                item.get("format") == "pdf"
                                and valid.get("structure") is not True
                            ):
                                raise ValueError("unvalidated SI PDF")
                            body = path.read_bytes()
                            if (
                                not body
                                or len(body) != item.get("size_bytes")
                                or hashlib.sha256(body).hexdigest()
                                != item.get("sha256")
                            ):
                                raise ValueError("inconsistent published facts")
                        except (OSError, ValueError):
                            item = _failed(asset, "invalid_published_facts")
                    items.append(item)
        except RequestCancelledError:
            raise
        except Exception as error:
            failure = _phase_failure(error, context, stage)
            if stage == "si_discovery":
                discovery_public = {"status": "failed", **failure}
            items = [
                _failed(asset, failure["code"], failure=failure) for asset in discovered
            ]
        discovery_public["diagnostics"] = {"phases": phases}
    context.raise_if_cancelled()
    acceptance = evaluate_download_acceptance(
        main, items, discovery_status=discovery_public["status"]
    )
    context.fetch_trace = project_source_trail_trace(source_trail, context.fetch_trace)
    trace = []
    for event in context.fetch_trace:
        if is_dataclass(event) and not isinstance(event, type):
            trace.append(asdict(event))
        else:
            model_dump = getattr(event, "model_dump", None)
            trace.append(model_dump(mode="json") if callable(model_dump) else {})
    trace = [
        {
            key: value
            for key, value in event.items()
            if key in TraceEvent.__dataclass_fields__
        }
        for event in trace
    ]
    return public_download_data(
        dict(
            schema_version=2,
            status="ok" if acceptance.overall == "complete" else "partial",
            is_error=False,
            paper={"doi": resolved.doi, "title": metadata.get("title")},
            main_pdf=main,
            supplementary=items,
            si_discovery=discovery_public,
            acceptance=acceptance.model_dump(mode="json"),
            source_trail=source_trail,
            trace=trace,
            warnings=[]
            if acceptance.overall == "complete"
            else ["Supplementary download is incomplete."],
        )
    )
