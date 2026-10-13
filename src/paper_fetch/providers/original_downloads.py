"""Narrow provider-owned seam for downloading original publisher files.

This module deliberately does not route by publisher or fall back to the
full-text waterfall. A caller passes the provider client selected by the
existing resolver; unsupported capabilities remain explicitly unsupported.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from dataclasses import replace
from typing import Any

from ..runtime import RuntimeContext
from ._pdf_common import PdfFetchFailure, PdfFetchResult, validate_original_pdf_bytes
from .base import ProviderFailure


def download_primary_pdf(
    client: Any,
    doi: str,
    metadata: Mapping[str, Any],
    *,
    context: RuntimeContext | None = None,
) -> PdfFetchResult:
    """Fetch and validate the original PDF without article conversion."""
    downloader = getattr(client, "download_primary_pdf", None)
    if not callable(downloader):
        raise NotImplementedError("NOT_SUPPORTED: provider has no raw PDF capability")
    try:
        try:
            import pymupdf  # noqa: F401
        except ImportError as exc:
            raise PdfFetchFailure(
                "missing_pymupdf", "PyMuPDF is required for original PDF validation."
            ) from exc
        result = downloader(doi, metadata, context=context)
        if not isinstance(result, PdfFetchResult):
            raise TypeError("provider raw PDF capability must return PdfFetchResult")
        if result.markdown_text:
            raise ValueError(
                "provider raw PDF capability returned converted article text"
            )
        validated = validate_original_pdf_bytes(
            pdf_bytes=result.pdf_bytes,
            source_url=result.source_url,
            final_url=result.final_url,
            expected_identity={**dict(metadata), "doi": doi},
            suggested_filename=result.suggested_filename,
        )
    except PdfFetchFailure as exc:
        if exc.kind == "missing_pymupdf":
            raise ProviderFailure("not_configured", str(exc)) from exc
        raise
    if context is not None:
        context.raise_if_cancelled()
    route = result.diagnostics.get("route")
    return (
        replace(validated, diagnostics={**validated.diagnostics, "route": route})
        if route
        else validated
    )


def discover_supplementary_files(
    client: Any,
    doi: str,
    metadata: Mapping[str, Any],
    *,
    context: RuntimeContext | None = None,
) -> dict[str, Any]:
    """Discover explicit publisher-scoped supplementary objects."""
    discover = getattr(client, "discover_supplementary_files", None)
    if not callable(discover):
        return {
            "status": "not_supported",
            "assets": [],
            "source_url": None,
            "browser_context_seed": None,
            "diagnostics": {"code": "NOT_SUPPORTED"},
        }
    result = discover(doi, metadata, context=context)
    if not isinstance(result, Mapping) or result.get("status") not in {
        "ok",
        "not_supported",
    }:
        raise TypeError("provider discovery must return a supported status mapping")
    return dict(result)


def download_supplementary_files(
    client: Any,
    doi: str,
    metadata: Mapping[str, Any],
    discovery: Mapping[str, Any],
    output_dir: Path,
    *,
    context: RuntimeContext | None = None,
) -> dict[str, Any]:
    """Download discovered original assets using the provider's authorized path."""
    download = getattr(client, "download_supplementary_files", None)
    if not callable(download):
        return {
            "assets": [],
            "asset_failures": [
                {
                    "status": "not_supported",
                    "code": "NOT_SUPPORTED",
                    "reason": "provider has no original supplementary downloader",
                }
            ],
        }
    result = download(doi, metadata, discovery, output_dir, context=context)
    if not isinstance(result, Mapping) or not {"assets", "asset_failures"}.issubset(
        result
    ):
        raise TypeError("provider supplementary downloader returned an invalid result")
    return dict(result)
