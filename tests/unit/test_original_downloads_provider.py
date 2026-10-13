from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from paper_fetch.providers import _pdf_common as pdf
from paper_fetch.providers import original_downloads as originals
from paper_fetch.providers.acs import AcsClient
from paper_fetch.providers.wiley import WileyClient
from paper_fetch.providers.elsevier import ElsevierClient
from paper_fetch.extraction.html.assets.originals import validate_supplementary_payload


def test_primary_seam_revalidates_provider_bytes_without_conversion(monkeypatch):
    result = pdf.PdfFetchResult(
        "https://publisher.test/article",
        "https://publisher.test/pdf",
        b"mock raw bytes",
        "",
    )
    validator = Mock(return_value=result)
    monkeypatch.setattr(originals, "validate_original_pdf_bytes", validator)
    client = SimpleNamespace(download_primary_pdf=Mock(return_value=result))
    assert originals.download_primary_pdf(client, "10.1021/example", {}) is result
    assert validator.call_args.kwargs["expected_identity"] == {"doi": "10.1021/example"}


def test_primary_seam_rejects_converted_text():
    client = SimpleNamespace(
        download_primary_pdf=lambda *a, **kw: pdf.PdfFetchResult(
            "source", "final", b"raw", "converted"
        )
    )
    with pytest.raises(ValueError, match="converted"):
        originals.download_primary_pdf(client, "10.1021/example", {})


def test_missing_capabilities_are_explicitly_unsupported():
    assert (
        originals.discover_supplementary_files(object(), "10.1/x", {})["status"]
        == "not_supported"
    )
    outcome = originals.download_supplementary_files(
        object(), "10.1/x", {}, {"assets": []}, Path(".")
    )
    assert outcome["asset_failures"][0]["code"] == "NOT_SUPPORTED"
    with pytest.raises(NotImplementedError, match="NOT_SUPPORTED"):
        originals.download_primary_pdf(object(), "10.1/x", {})


@pytest.mark.parametrize(
    "url,reason",
    [
        ("https://pubs.acs.org/supplementary/example", "pdf_supplement_only"),
        ("https://publisher.test/previewpdf/example", "pdf_preview_only"),
    ],
)
def test_raw_validation_rejects_non_article_role_before_backend(
    tmp_path, monkeypatch, url, reason
):
    monkeypatch.setattr(
        pdf, "_pdf_page_count", Mock(side_effect=AssertionError("backend forbidden"))
    )
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-mocked")
    with pytest.raises(pdf.PdfFetchFailure) as caught:
        pdf._validate_pdf_path(
            path,
            pdf_bytes=b"%PDF-mocked",
            source_url=url,
            final_url=url,
            suggested_filename=None,
            expected_identity={},
        )
    assert caught.value.kind == reason


def test_raw_validation_checks_structure_and_identity_without_renderer(
    tmp_path, monkeypatch
):
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-mocked")
    monkeypatch.setattr(pdf, "_pdf_page_count", lambda path: 2)
    monkeypatch.setattr(
        pdf, "_pdf_identity_evidence", lambda path: {"doi": "10.1021/right"}
    )
    monkeypatch.setattr(
        pdf,
        "render_pdf_markdown_result",
        Mock(side_effect=AssertionError("conversion forbidden")),
    )
    facts = pdf._validate_pdf_path(
        path,
        pdf_bytes=b"%PDF-mocked",
        source_url="https://publisher.test/pdf",
        final_url="https://publisher.test/pdf",
        suggested_filename=None,
        expected_identity={"doi": "10.1021/right"},
    )
    assert facts["identity"]["status"] == "match"
    assert all(facts["validation"].values())
    with pytest.raises(pdf.PdfFetchFailure, match="match"):
        pdf._validate_pdf_path(
            path,
            pdf_bytes=b"%PDF-mocked",
            source_url="https://publisher.test/pdf",
            final_url="https://publisher.test/pdf",
            suggested_filename=None,
            expected_identity={"doi": "10.1021/wrong"},
        )


def test_raw_bytes_mode_does_not_enter_renderer(monkeypatch):
    validator = Mock(return_value="validated original")
    monkeypatch.setattr(pdf, "validate_original_pdf_bytes", validator)
    monkeypatch.setattr(
        pdf,
        "_render_pdf_markdown_result_with_cache",
        Mock(side_effect=AssertionError("conversion forbidden")),
    )
    assert (
        pdf.pdf_fetch_result_from_bytes(
            artifact_dir=None,
            source_url="s",
            final_url="f",
            pdf_bytes=b"raw",
            raw_only=True,
        )
        == "validated original"
    )


def test_missing_raw_backend_is_explicit(tmp_path, monkeypatch):
    import builtins

    original_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "pymupdf":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(pdf.PdfFetchFailure) as caught:
        pdf.validate_original_pdf_bytes(
            pdf_bytes=b"%PDF-", source_url="s", final_url="f"
        )
    assert caught.value.kind == "missing_pymupdf"


@pytest.mark.parametrize(
    "payload,name",
    [
        (b"<html>error</html>", "data.csv"),
        (b"PKxx", "archive.zip"),
        (b"verify you are human", "data.txt"),
        (b"%PDF-", "data.pdf"),
    ],
)
def test_si_validation_rejects_error_or_unknown_file(payload, name, monkeypatch):
    monkeypatch.setattr(
        pdf,
        "validate_original_pdf_bytes",
        Mock(side_effect=ValueError("invalid_pdf_structure")),
    )
    with pytest.raises(ValueError):
        validate_supplementary_payload(payload, name)


def test_si_csv_is_not_rejected_as_text():
    assert validate_supplementary_payload(b"name,value\nA,12\n", "data.csv") == "csv"


def test_acs_scope_only_canonical_supplement_not_viewer_or_body():
    client = AcsClient(None, {})
    assets = client._original_supplementary_assets(
        """<a href="/x/article-supplement/9/pdf/body.pdf/">body</a>
        <figshare-widget><a href="https://figshare.com/articles/viewer">viewer</a>
        <a href="/acsodf/article-supplement/9/pdf/si.pdf/">Supporting data</a></figshare-widget>""",
        "https://pubs.acs.org/doi/10.1021/example",
    )
    assert [asset["filename_hint"] for asset in assets] == ["si.pdf"]


def test_wiley_scope_only_supporting_panel():
    client = WileyClient(None, {})
    assets = client._original_supplementary_assets(
        """<a href="/doi/suppl/10.1111/x/sup-1.csv">body</a>
        <section class="article-section__supporting"><a data-test="supp-info-link"
        href="/action/downloadSupplement?doi=10.1111/x&amp;file=sup-1.csv">data</a></section>""",
        "https://onlinelibrary.wiley.com/doi/full/10.1111/x",
    )
    assert len(assets) == 1
    assert assets[0]["filename_hint"] == "sup-1.csv"


def test_wiley_primary_uses_tdm_local_raw_owner(monkeypatch):
    from paper_fetch.providers import wiley

    client = WileyClient(None, {"WILEY_TDM_CLIENT_TOKEN": "secret"})
    fetch = Mock(return_value=pdf.PdfFetchResult("source", "final", b"mocked", ""))
    monkeypatch.setattr(wiley, "_fetch_wiley_tdm_pdf_result", fetch)
    assert (
        client.download_primary_pdf("10.1111/example", {}).diagnostics["route"]
        == "tdm_pdf"
    )
    assert fetch.call_args.kwargs["raw_only"] is True


def test_elsevier_entitlement_first_page_is_not_download_success():
    transport = Mock(
        request=Mock(
            return_value={
                "body": b"%PDF-mocked",
                "headers": {"x-els-status": "limited to first page"},
            }
        )
    )
    client = ElsevierClient(transport, {"ELSEVIER_API_KEY": "secret"})
    with pytest.raises(Exception, match="first page"):
        client.download_primary_pdf("10.1016/example", {})
    assert transport.request.call_args.kwargs["query"] == {"view": "FULL"}
    assert transport.request.call_args.kwargs["headers"]["Accept"] == "application/pdf"


@pytest.mark.parametrize("client_type", [AcsClient, WileyClient])
def test_original_main_seed_is_request_client_doi_scoped(monkeypatch, client_type):
    from dataclasses import replace
    from paper_fetch.runtime import RuntimeContext
    from paper_fetch.providers.browser_runtime import BrowserWarmResult
    from paper_fetch.providers.browser_workflow.shared import (
        default_browser_workflow_deps,
    )

    doi = "10.1021/example" if client_type is AcsClient else "10.1002/example"
    url = (
        "https://pubs.acs.org/doi/" + doi
        if client_type is AcsClient
        else "https://onlinelibrary.wiley.com/doi/" + doi
    )
    seed = {
        "browser_final_url": url,
        "browser_cookies": [{"name": "auth", "value": "private"}],
    }
    warm = BrowserWarmResult(
        seed=seed, changed=True, accepted=True, status=200, reason="refreshed"
    )
    html = Mock(
        return_value=SimpleNamespace(
            html=f'<meta name="citation_doi" content="{doi}">',
            final_url=url,
            browser_context_seed=seed,
        )
    )
    deps = replace(
        default_browser_workflow_deps(),
        load_runtime_config=Mock(return_value=SimpleNamespace()),
        ensure_runtime_ready=Mock(),
        warm_browser_context=Mock(return_value=warm),
        fetch_pdf_with_browser=Mock(
            return_value=pdf.PdfFetchResult(url, url, b"raw", "")
        ),
        fetch_html_with_browser=html,
    )
    with RuntimeContext() as context:
        context.initialize_deadline(120)
        deadline = context.deadline_monotonic
        client = client_type(None, {}, deps=deps)
        client.download_primary_pdf(doi, {}, context=context)
        client.discover_supplementary_files(doi, {}, context=context)
        assert (
            html.call_args.kwargs["browser_context_seed"]["browser_cookies"]
            == seed["browser_cookies"]
        )
        assert context.deadline_monotonic == deadline
        # Another client/credential scope must not inherit the first client's seed.
        client_type(None, {}, deps=deps).discover_supplementary_files(
            doi, {}, context=context
        )
        assert "browser_context_seed" not in html.call_args.kwargs
        # Neither should a different identity in the same RuntimeContext.
        html.return_value.html = '<meta name="citation_doi" content="10.1234/other">'
        client.discover_supplementary_files("10.1234/other", {}, context=context)
        assert "browser_context_seed" not in html.call_args.kwargs
    # A new request does not inherit state even when it reuses the client.
    html.return_value.html = f'<meta name="citation_doi" content="{doi}">'
    with RuntimeContext() as other_request:
        client.discover_supplementary_files(doi, {}, context=other_request)
        assert "browser_context_seed" not in html.call_args.kwargs


def test_supplement_source_binding_is_required(tmp_path):
    client = ElsevierClient(Mock(), {})
    with pytest.raises(ValueError, match="binding"):
        client.download_supplementary_files(
            "10.1016/right", {}, {"assets": [{"paper_doi": "10.1016/wrong"}]}, tmp_path
        )


def test_provider_si_report_accounts_for_files_stopped_by_budget():
    from paper_fetch.providers.browser_workflow.originals import (
        bind_supplementary_assets,
        supplementary_download_report,
    )

    assets = bind_supplementary_assets(
        [{"url": "https://publisher.test/data.csv", "filename_hint": "data.csv"}],
        "10.1021/example",
        "https://publisher.test/article",
    )
    report = supplementary_download_report(
        {"assets": [], "asset_failures": []}, assets, "acs"
    )
    failure = report["asset_failures"][0]
    assert failure["id"] == assets[0]["id"]
    assert failure["code"] == "supplementary_not_completed"
    assert "path" not in failure and "sha256" not in failure


@pytest.mark.parametrize(
    "name,payload",
    [
        ("data.json", b"not JSON"),
        ("data.xml", b"not XML"),
        ("data.xml", b"<service-error>RESOURCE_NOT_FOUND</service-error>"),
        ("data.json", b'{"service-error":{"status":"RESOURCE_NOT_FOUND"}}'),
        ("data.rtf", b"plain text"),
        ("data.json", b"[" * 129 + b"0" + b"]" * 129),
        ("data.xml", b'<!DOCTYPE x [<!ENTITY e "bad">]><x>&e;</x>'),
    ],
)
def test_original_structured_text_rejects_invalid_or_service_payload(name, payload):
    with pytest.raises(ValueError):
        validate_supplementary_payload(payload, name)


@pytest.mark.parametrize(
    "name,payload,format_name",
    [
        ("data.csv", b"condition,value\ncaptcha,1\n", "csv"),
        (
            "data.txt",
            b"The experiment studies access denied and captcha messages.",
            "txt",
        ),
        ("data.xml", b"<data><condition>captcha</condition></data>", "xml"),
        ("data.json", b'{"condition":"captcha","value":1}', "json"),
        ("data.rtf", rb"{\rtf1 original text}", "rtf"),
    ],
)
def test_original_legal_text_is_preserved(name, payload, format_name):
    assert validate_supplementary_payload(payload, name) == format_name


@pytest.mark.parametrize(
    "raw_only,expected", [(True, "not_configured"), (False, "no_result")]
)
def test_elsevier_missing_pdf_backend_only_changes_raw_classification(
    monkeypatch, raw_only, expected
):
    from paper_fetch.providers.elsevier import ElsevierClient
    from paper_fetch.providers.base import ProviderFailure
    from unittest.mock import Mock

    transport = Mock()
    transport.request.return_value = {
        "status_code": 200,
        "body": b"%PDF-",
        "headers": {"content-type": "application/pdf"},
    }
    monkeypatch.setattr(
        "paper_fetch.providers.elsevier.pdf_fetch_result_from_response",
        Mock(
            side_effect=pdf.PdfFetchFailure(
                "missing_pymupdf", "PyMuPDF is required for original PDF validation."
            )
        ),
    )
    client = ElsevierClient(transport, {"ELSEVIER_API_KEY": "secret"})
    with pytest.raises(ProviderFailure) as caught:
        client._fetch_official_pdf_result("10.1016/example", raw_only=raw_only)
    assert caught.value.code == expected
    assert "PyMuPDF" in str(caught.value)
