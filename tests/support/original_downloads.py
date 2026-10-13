"""Small provider-boundary facts for original download contracts."""

import hashlib

from paper_fetch.providers._pdf_common import PdfFetchResult
from paper_fetch.resolve.query import ResolvedQuery

DOI = "10.1021/example"
MAIN_BYTES = b"provider-validated-original-pdf"


def install_download_boundary(monkeypatch, *, data=MAIN_BYTES, validation=None):
    import paper_fetch.workflow.downloads as downloads
    import paper_fetch.providers.original_downloads as providers

    client = object()
    resolved = ResolvedQuery(
        query=DOI,
        query_kind="doi",
        doi=DOI,
        landing_url="https://pubs.acs.org/doi/10.1021/example",
        provider_hint="acs",
        confidence=1.0,
        candidates=[],
        title="Example",
    )
    monkeypatch.setattr(downloads, "resolve_paper", lambda *a, **k: resolved)
    # Crossref is metadata owner, never the PDF client.
    monkeypatch.setattr(
        downloads,
        "fetch_metadata_for_resolved_query",
        lambda *a, **k: ({"title": "Example"}, "crossref", []),
    )
    facts = {
        "validation": validation
        if validation is not None
        else dict.fromkeys(("format", "structure", "size", "role", "identity"), True),
        "pdf_sha256": hashlib.sha256(data).hexdigest(),
        "pdf_bytes": len(data),
        "pdf_pages": 1,
        "identity": {"status": "matched"},
        "route": "pdf",
    }

    def primary(selected, *a, **k):
        assert selected is client
        return PdfFetchResult(
            "https://pubs.acs.org/main?token=secret",
            "https://pubs.acs.org/main?sig=secret",
            data,
            "",
            suggested_filename="../../unsafe name.pdf",
            diagnostics=facts,
        )

    monkeypatch.setattr(providers, "download_primary_pdf", primary)
    monkeypatch.setattr(
        providers,
        "discover_supplementary_files",
        lambda *a, **k: {
            "status": "ok",
            "assets": [],
            "browser_context_seed": {"cookies": "secret"},
        },
    )
    return client, providers
