import json
import pytest
from paper_fetch.runtime import RuntimeContext
from paper_fetch.workflow.types import PaperFetchFailure
from tests.support.original_downloads import DOI, install_download_boundary

from paper_fetch.service import download_paper_pdfs


def test_download_paper_pdfs_facade_forwards_required_arguments(monkeypatch):
    import paper_fetch.workflow.downloads as downloads

    expected = {"schema_version": 2}
    seen = {}

    def fake(query, *, output_dir, include_si, context):
        seen.update(query=query, output_dir=output_dir, include_si=include_si)
        assert context is not None
        return expected

    monkeypatch.setattr(downloads, "download_paper_pdfs", fake)
    assert (
        download_paper_pdfs("10.1234/example", output_dir="./papers", include_si=False)
        == expected
    )
    assert seen == {
        "query": "10.1234/example",
        "output_dir": "./papers",
        "include_si": False,
    }


@pytest.mark.parametrize("missing", ["format", "structure", "size", "role", "identity"])
def test_missing_validation_never_publishes(monkeypatch, tmp_path, missing):
    validation = dict.fromkeys(
        ("format", "structure", "size", "role", "identity"), True
    )
    validation.pop(missing)
    client, _ = install_download_boundary(monkeypatch, validation=validation)
    with RuntimeContext(clients={"acs": client}) as runtime:
        with pytest.raises(PaperFetchFailure, match="validation facts"):
            download_paper_pdfs(DOI, output_dir=str(tmp_path), context=runtime)
    assert not list(tmp_path.rglob("*.pdf"))


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("empty", "not_found"),
        ("unsupported", "unsupported"),
        ("exception", "failed"),
        ("not_requested", "not_requested"),
    ],
)
def test_discovery_states_are_distinct(monkeypatch, tmp_path, kind, expected):
    client, providers = install_download_boundary(monkeypatch)

    def discover(*a, **k):
        if kind in {"exception", "not_requested"}:
            raise ValueError(
                "should not run" if kind == "not_requested" else "token=secret"
            )
        return {
            "status": "not_supported" if kind == "unsupported" else "ok",
            "assets": [],
            "browser_context_seed": {"cookies": "secret"},
        }

    monkeypatch.setattr(providers, "discover_supplementary_files", discover)
    with RuntimeContext(clients={"acs": client}) as runtime:
        result = download_paper_pdfs(
            DOI,
            output_dir=str(tmp_path),
            include_si=kind != "not_requested",
            context=runtime,
        )
    assert result["si_discovery"]["status"] == expected
    assert result["status"] == (
        "partial" if kind in {"unsupported", "exception"} else "ok"
    )
    assert not result["is_error"]
    assert "secret" not in json.dumps(result)


def test_missing_attachment_terminal_result_is_partial(monkeypatch, tmp_path):
    client, providers = install_download_boundary(monkeypatch)
    monkeypatch.setattr(
        providers,
        "discover_supplementary_files",
        lambda *a, **k: {"status": "ok", "assets": [{"id": "si-1"}, {"id": "si-2"}]},
    )
    monkeypatch.setattr(
        providers,
        "download_supplementary_files",
        lambda *a, **k: {"assets": [], "asset_failures": []},
    )
    with RuntimeContext(clients={"acs": client}) as runtime:
        result = download_paper_pdfs(DOI, output_dir=str(tmp_path), context=runtime)
    assert result["status"] == "partial"
    assert [item["id"] for item in result["supplementary"]] == ["si-1", "si-2"]
    assert all(item["status"] == "failed" for item in result["supplementary"])


@pytest.mark.parametrize("strength", ["strong", "weak"])
def test_provider_access_boundary_strength(monkeypatch, tmp_path, strength):
    import paper_fetch.workflow.downloads as downloads
    from paper_fetch.publisher_identity import ProviderIdentityCandidate
    from paper_fetch.providers.base import ProviderFailure

    client, providers = install_download_boundary(monkeypatch)
    primary = providers.download_primary_pdf
    attempted = []
    other = object()
    monkeypatch.setattr(
        downloads,
        "build_official_provider_candidate_evidence",
        lambda *a, **k: [
            ProviderIdentityCandidate("wiley", "domain", strength),
            ProviderIdentityCandidate("acs", "doi", "strong"),
        ],
    )

    def attempt(selected, *args, **kwargs):
        attempted.append(selected)
        if selected is other:
            raise ProviderFailure("no_access", "Access blocked")
        return primary(selected, *args, **kwargs)

    monkeypatch.setattr(providers, "download_primary_pdf", attempt)
    with RuntimeContext(clients={"acs": client, "wiley": other}) as runtime:
        if strength == "strong":
            with pytest.raises(ProviderFailure, match="Access blocked"):
                download_paper_pdfs(DOI, output_dir=str(tmp_path), context=runtime)
            assert attempted == [other]
        else:
            result = download_paper_pdfs(DOI, output_dir=str(tmp_path), context=runtime)
            assert result["main_pdf"]["provider"] == "acs"
            assert attempted == [other, client]


def test_si_download_exception_has_terminal_items_and_keeps_main(monkeypatch, tmp_path):
    client, providers = install_download_boundary(monkeypatch)
    monkeypatch.setattr(
        providers,
        "discover_supplementary_files",
        lambda *a, **k: {"status": "ok", "assets": [{"id": "one"}, {"id": "two"}]},
    )

    def fail(*a, **k):
        raise RuntimeError("private storage state")

    monkeypatch.setattr(providers, "download_supplementary_files", fail)
    with RuntimeContext(clients={"acs": client}) as runtime:
        result = download_paper_pdfs(DOI, output_dir=str(tmp_path), context=runtime)
    assert result["main_pdf"]["status"] == "downloaded"
    assert result["status"] == "partial"
    assert {item["id"] for item in result["supplementary"]} == {"one", "two"}
    assert all(item["status"] == "failed" for item in result["supplementary"])
    assert "private storage" not in str(result)


@pytest.mark.parametrize("stage", ["si_discovery", "si_download"])
@pytest.mark.parametrize(
    "kind,code,category",
    [
        ("dns", "dns_error", "dns_error"),
        ("challenge", "cloudflare_challenge", "cloudflare_challenge"),
        ("http", "http_error", "http_error"),
        ("deadline", "request_deadline_exceeded", "timeout"),
        ("browser_deadline", "request_deadline_exceeded", "timeout"),
        ("internal", "supplementary_internal_error", "internal_error"),
    ],
)
def test_si_failure_preserves_stage_category_budget_without_secrets(
    monkeypatch, tmp_path, stage, kind, code, category
):
    import time
    from paper_fetch.http import RequestFailure
    from paper_fetch.providers.browser_runtime import BrowserRuntimeFailure

    client, providers = install_download_boundary(monkeypatch)
    monkeypatch.setattr(
        providers,
        "discover_supplementary_files",
        lambda *a, **k: {
            "status": "ok",
            "assets": [{"id": "si", "url": "https://pubs.acs.org/data"}],
        },
    )

    def fail(*a, context, **k):
        if kind in {"deadline", "browser_deadline"}:
            context.deadline_monotonic = time.monotonic() - 1
            if kind == "browser_deadline":
                raise BrowserRuntimeFailure(
                    "browser_connect_timeout",
                    "Budget exhausted",
                    details={"trace": {"deadline_exhausted": True}},
                )
            raise TimeoutError("private storage state")
        if kind == "dns":
            raise RequestFailure(
                None,
                "DNS failed",
                error_category="dns_error",
                headers={"Authorization": "secret"},
            )
        if kind == "http":
            raise RequestFailure(503, "HTTP 503", body=b"private storage state")
        if kind == "challenge":
            raise BrowserRuntimeFailure(
                "cloudflare_challenge", "Challenge", details={"cookies": "secret"}
            )
        raise RuntimeError("private storage state")

    monkeypatch.setattr(
        providers,
        "discover_supplementary_files"
        if stage == "si_discovery"
        else "download_supplementary_files",
        fail,
    )
    with RuntimeContext(clients={"acs": client}) as runtime:
        result = download_paper_pdfs(DOI, output_dir=str(tmp_path), context=runtime)
    assert (
        result["main_pdf"]["status"] == "downloaded" and result["status"] == "partial"
    )
    info = (
        result["si_discovery"]
        if stage == "si_discovery"
        else result["supplementary"][0]
    )
    assert (info["stage"], info["code"], info["error_category"]) == (
        stage,
        code,
        category,
    )
    assert result["si_discovery"]["status"] == (
        "failed" if stage == "si_discovery" else "ok"
    )
    if kind == "http":
        assert info["http_status"] == 503
    phases = result["si_discovery"]["diagnostics"]["phases"]
    assert phases[stage]["duration_ms"] >= 0
    if kind in {"deadline", "browser_deadline"}:
        assert phases[stage]["remaining_after_seconds"] == 0
    events = [e for e in result["trace"] if e["component"] == stage]
    assert len(events) == 1 and events[0]["code"] == code
    assert "secret" not in json.dumps(result) and "private storage" not in json.dumps(
        result
    )
