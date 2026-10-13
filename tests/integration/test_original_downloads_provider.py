"""Actual PDF backend and controlled browser/file publication contracts."""

from dataclasses import replace
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
import zipfile

import pymupdf
import pytest

from paper_fetch.providers import _pdf_common as pdf
from paper_fetch.providers import _pdf_fallback as fallback
from paper_fetch.providers.acs import AcsClient
from paper_fetch.providers.wiley import WileyClient
from paper_fetch.providers.elsevier import ElsevierClient
from paper_fetch.providers.browser_workflow.shared import default_browser_workflow_deps
from paper_fetch.providers.browser_workflow.originals import bind_supplementary_assets
from paper_fetch.extraction.html.assets import (
    AssetDownloadOptions,
    SUPPLEMENTARY_KIND,
    download_assets,
)
from paper_fetch.runtime import RuntimeContext


def make_pdf(doi="10.1021/example", opening="Original research article"):
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), opening + "\n" + doi)
        document.set_metadata({"title": opening, "subject": doi})
        return document.tobytes()


@pytest.mark.parametrize("route", ["response", "request", "download"])
def test_browser_bytes_entries_validate_original_without_renderer(
    route, tmp_path, monkeypatch
):
    payload = make_pdf()
    monkeypatch.setattr(
        pdf,
        "_render_pdf_markdown_result_with_cache",
        Mock(side_effect=AssertionError("conversion forbidden")),
    )
    url = "https://pubs.acs.org/doi/pdf/10.1021/example"
    common: dict[str, Any] = dict(
        artifact_dir=tmp_path, source_url=url, final_url=url, raw_only=True
    )
    request = fallback.PdfRequestContext(expected_identity={"doi": "10.1021/example"})
    if route == "download":
        download = SimpleNamespace(
            save_as=lambda path: Path(path).write_bytes(payload),
            suggested_filename="article.pdf",
        )
        result = fallback._download_to_pdf_result(
            download, expected_identity=request.expected_identity, **common
        )
    else:
        response = SimpleNamespace(
            headers={"content-type": "application/pdf"}, body=lambda: payload, url=url
        )
        if route == "response":
            result = fallback._response_to_pdf_result(
                response, request=request, **common
            )
        else:
            page = SimpleNamespace(
                request=SimpleNamespace(get=lambda *args, **kwargs: response)
            )
            result = fallback._refetch_pdf_with_browser_request(
                page, request=request, **common
            )
    assert result is not None
    assert result.pdf_bytes == payload
    assert result.markdown_text == ""
    assert result.diagnostics["identity"]["status"] == "match"
    assert result.diagnostics["validation"] == {
        "format": True,
        "structure": True,
        "size": True,
        "role": True,
        "identity": True,
    }
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "payload,doi,opening,url,reason",
    [
        (
            b"%PDF-",
            "10.1021/example",
            "article",
            "https://publisher.test/pdf",
            "invalid_pdf_structure",
        ),
        (
            None,
            "10.1021/wrong",
            "article",
            "https://publisher.test/pdf",
            "identity_mismatch",
        ),
        (
            None,
            "10.1021/example",
            "Supporting Information",
            "https://publisher.test/pdf",
            "pdf_supplement_only",
        ),
        (
            None,
            "10.1021/example",
            "article",
            "https://pubs.acs.org/x/article-supplement/9/pdf/si/",
            "pdf_supplement_only",
        ),
    ],
)
def test_actual_raw_pdf_rejects_invalid_identity_and_supplement(
    payload, doi, opening, url, reason
):
    with pytest.raises(pdf.PdfFetchFailure) as caught:
        pdf.validate_original_pdf_bytes(
            pdf_bytes=payload or make_pdf(doi, opening),
            source_url=url,
            final_url=url,
            expected_identity={"doi": "10.1021/example"},
        )
    assert caught.value.kind == reason


@pytest.mark.parametrize(
    "client_type,doi",
    [(AcsClient, "10.1021/example"), (WileyClient, "10.1111/example")],
)
def test_browser_provider_primary_active_raw_mode(
    client_type, doi, tmp_path, monkeypatch
):
    payload = make_pdf(doi)
    context = RuntimeContext(env={}, transport=Mock(), download_dir=tmp_path)

    def acquire(candidates, **kwargs):
        assert isinstance(kwargs["artifact_dir"], Path)
        assert kwargs["raw_only"] is True
        assert kwargs["request"].runtime is context
        return pdf.pdf_fetch_result_from_bytes(
            artifact_dir=None,
            source_url=candidates[0],
            final_url=candidates[0],
            pdf_bytes=payload,
            expected_identity=kwargs["request"].expected_identity,
            raw_only=kwargs["raw_only"],
        )

    deps = replace(
        default_browser_workflow_deps(),
        load_runtime_config=Mock(return_value=SimpleNamespace()),
        ensure_runtime_ready=Mock(),
        warm_browser_context=Mock(return_value={}),
        fetch_pdf_with_browser=acquire,
        bootstrap_browser_workflow=Mock(
            side_effect=AssertionError("article bootstrap forbidden")
        ),
    )
    monkeypatch.setattr(
        pdf,
        "_render_pdf_markdown_result_with_cache",
        Mock(side_effect=AssertionError("conversion forbidden")),
    )
    client = client_type(Mock(), {}, deps=deps)
    result = client.download_primary_pdf(doi, {}, context=context)
    assert result.pdf_bytes == payload and result.markdown_text == ""
    assert result.diagnostics["route"] == "browser_pdf"
    assert all(result.diagnostics["validation"].values())


@pytest.mark.parametrize(
    "client_type,doi,env",
    [
        (WileyClient, "10.1111/example", {"WILEY_TDM_CLIENT_TOKEN": "secret"}),
        (ElsevierClient, "10.1016/example", {"ELSEVIER_API_KEY": "secret"}),
    ],
)
def test_authorized_api_primary_retains_original_bytes(
    client_type, doi, env, tmp_path, monkeypatch
):
    payload = make_pdf(doi)
    transport = Mock()
    transport.request.return_value = {
        "status_code": 200,
        "headers": {"content-type": "application/pdf"},
        "body": payload,
        "url": "https://api.publisher.test/article.pdf",
    }
    client = client_type(transport, env)
    monkeypatch.setattr(
        pdf,
        "_render_pdf_markdown_result_with_cache",
        Mock(side_effect=AssertionError("conversion forbidden")),
    )
    result = client.download_primary_pdf(
        doi,
        {},
        context=RuntimeContext(env=env, transport=transport, download_dir=tmp_path),
    )
    assert result is not None
    assert result.pdf_bytes == payload
    assert result.markdown_text == ""
    assert result.diagnostics["pdf_sha256"] == hashlib.sha256(payload).hexdigest()
    assert result.diagnostics["route"] == (
        "tdm_pdf" if client_type is WileyClient else "pdf_api"
    )
    assert all(result.diagnostics["validation"].values())


def original_asset_download(tmp_path, assets, responses, context=None):
    context = context or RuntimeContext(env={}, transport=Mock(), download_dir=tmp_path)

    def fetch(url, asset):
        return {
            "status_code": 200,
            "headers": {"content-type": "application/octet-stream"},
            "body": responses[url],
            "url": url,
        }

    return download_assets(
        SUPPLEMENTARY_KIND,
        Mock(),
        article_id="10.1021/example",
        assets=assets,
        output_dir=tmp_path,
        user_agent="test",
        options=AssetDownloadOptions(
            original_files=True,
            file_document_fetcher=fetch,
            fetch_policy="browser_only",
            asset_download_concurrency=1,
            asset_budget=context.asset_budget,
            artifact_store=context.artifact_store,
            runtime_context=context,
        ),
    )


def test_multiformat_si_original_hash_partial_and_same_name_isolation(tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("data.csv", "x,y\n1,2")
    assets = bind_supplementary_assets(
        [
            {"url": "https://publisher.test/si/1", "filename_hint": "data.csv"},
            {"url": "https://publisher.test/si/2", "filename_hint": "data.csv"},
            {"url": "https://publisher.test/si/3", "filename_hint": "data.zip"},
            {"url": "https://publisher.test/si/4", "filename_hint": "data.pdf"},
        ],
        "10.1021/example",
        "https://publisher.test/article",
    )
    responses = {
        assets[0]["url"]: b"x,y\n1,2",
        assets[1]["url"]: b"x,y\n3,4",
        assets[2]["url"]: archive.getvalue(),
        assets[3]["url"]: b"<html>error</html>",
    }
    result = original_asset_download(tmp_path, assets, responses)
    assert len(result["assets"]) == 3
    assert len(result["asset_failures"]) == 1
    assert len({item["path"] for item in result["assets"]}) == 3
    for item in result["assets"]:
        payload = responses[item["download_url"]]
        assert Path(item["path"]).read_bytes() == payload
        assert item["sha256"] == hashlib.sha256(payload).hexdigest()
    assert not list(tmp_path.rglob("data.csv"))  # ZIP was not unpacked.


def test_original_si_idempotence_conflict_and_cancellation_fence(tmp_path):
    assets = bind_supplementary_assets(
        [{"url": "https://publisher.test/si", "filename_hint": "data.csv"}],
        "10.1021/example",
        "https://publisher.test/article",
    )
    url = assets[0]["url"]
    first = original_asset_download(tmp_path, assets, {url: b"x,y\n1,2"})
    path = Path(first["assets"][0]["path"])
    assert original_asset_download(tmp_path, assets, {url: b"x,y\n1,2"})["assets"][0][
        "path"
    ] == str(path)
    conflict = original_asset_download(tmp_path, assets, {url: b"x,y\n3,4"})
    assert len(conflict["asset_failures"]) == 1
    assert path.read_bytes() == b"x,y\n1,2"
    context = RuntimeContext(env={}, transport=Mock(), download_dir=tmp_path)
    context.fence_commits()
    from paper_fetch.http import RequestCancelledError

    with pytest.raises(RequestCancelledError):
        original_asset_download(tmp_path, assets, {url: b"x,y\n1,2"}, context=context)
    assert path.read_bytes() == b"x,y\n1,2"


def test_wiley_actual_panel_click_redirect_response_and_download_cleanup(
    tmp_path, monkeypatch
):
    from paper_fetch.providers.browser_workflow.fetchers import file as file_owner

    payload = b"x,y\n1,2\n"
    temporary = tmp_path / "browser.tmp"
    temporary.write_bytes(payload)
    file_url = (
        "https://onlinelibrary.wiley.com/action/downloadSupplement?file=sup-1.csv"
    )
    page = Mock()
    page.url = "https://onlinelibrary.wiley.com/doi/full/10.1111/example"
    initial_request = Mock(
        url=file_url, redirected_from=None, frame=SimpleNamespace(page=page)
    )
    redirected = Mock(
        url="https://publisher.test/authorized/sup-1.csv",
        redirected_from=initial_request,
        frame=SimpleNamespace(page=page),
    )
    response = SimpleNamespace(
        request=redirected,
        status=200,
        url=redirected.url,
        all_headers=lambda: {"content-type": "text/csv"},
        headers={"content-type": "text/csv"},
    )
    download = SimpleNamespace(
        url=redirected.url,
        path=lambda: str(temporary),
        cancel=Mock(),
        delete=lambda: temporary.unlink(),
    )
    link = page.locator.return_value.first.locator.return_value.first
    link.is_visible.return_value = False
    link.get_attribute.side_effect = lambda name, **kwargs: (
        file_url if name == "href" else None
    )
    link.locator.return_value.locator.return_value.first.get_attribute.return_value = (
        "false"
    )
    page_callbacks, context_callbacks = {}, {}
    page.on.side_effect = lambda event, callback: page_callbacks.update(
        {event: callback}
    )
    browser_context = Mock()
    browser_context.on.side_effect = lambda event, callback: context_callbacks.update(
        {event: callback}
    )

    def activate(*args, **kwargs):
        context_callbacks["request"](initial_request)
        context_callbacks["request"](redirected)
        context_callbacks["response"](response)
        page_callbacks["download"](download)

    link.evaluate.side_effect = activate
    from paper_fetch.providers.browser_workflow.fetchers import click as click_owner
    from paper_fetch.providers.browser_workflow.fetchers.context import (
        BrowserDocumentFetcherOptions,
    )

    monkeypatch.setattr(
        click_owner,
        "wait_for_atypon_body_dom_ready",
        lambda *args, **kwargs: SimpleNamespace(ready=True),
    )
    context = RuntimeContext(env={}, transport=Mock(), download_dir=tmp_path)
    fetcher = file_owner._SharedBrowserFileDocumentFetcher(
        browser_context_seed_getter=lambda: {"browser_final_url": page.url},
        seed_urls_getter=lambda: [page.url],
        runtime_context=context,
        browser_options=BrowserDocumentFetcherOptions(
            runtime_config=SimpleNamespace(provider="wiley"),
            file_fetch_mode="page_click",
        ),
    )
    fetcher._context = browser_context
    fetcher._page = page
    fetched = fetcher(file_url, {"filename_hint": "sup-1.csv"})
    assert fetched is not None
    assert fetched["body"] == payload
    assert fetched["_paper_fetch_asset_route_diagnostics"]["mode"] == "page_click"
    assert not temporary.exists()
    link.locator.return_value.locator.return_value.first.click.assert_called()
    assets = bind_supplementary_assets(
        [{"url": file_url, "filename_hint": "sup-1.csv"}], "10.1111/example", page.url
    )
    result = original_asset_download(
        tmp_path, assets, {file_url: fetched["body"]}, context=context
    )
    assert Path(result["assets"][0]["path"]).read_bytes() == payload


@pytest.mark.parametrize(
    "client_type,doi,url,scope",
    [
        (
            AcsClient,
            "10.1021/example",
            "https://pubs.acs.org/doi/10.1021/example",
            '<div class="widget-ArticleDataSupplements"><a href="/x/article-supplement/9/csv/data.csv/">SI</a></div>',
        ),
        (
            WileyClient,
            "10.1111/example",
            "https://onlinelibrary.wiley.com/doi/full/10.1111/example",
            '<section class="article-section__supporting"><a data-test="supp-info-link" href="/action/downloadSupplement?file=sup-1.csv">SI</a></section>',
        ),
    ],
)
def test_provider_local_discovery_and_original_si_persistence(
    client_type, doi, url, scope, tmp_path
):
    html = f'<meta name="citation_doi" content="{doi}">' + scope
    payload = b"x,y\n1,2\n"
    seed = {
        "browser_final_url": url,
        "browser_cookies": [{"name": "auth", "value": "internal-secret"}],
    }
    result = SimpleNamespace(html=html, final_url=url, browser_context_seed=seed)
    html_fetcher = Mock(return_value=result)
    file_fetcher = Mock(
        side_effect=lambda file_url, asset: {
            "status_code": 200,
            "headers": {"content-type": "text/csv"},
            "url": file_url,
            "body": payload,
        }
    )
    file_fetcher_factory = Mock(return_value=file_fetcher)
    deps = replace(
        default_browser_workflow_deps(),
        load_runtime_config=Mock(return_value=SimpleNamespace()),
        ensure_runtime_ready=Mock(),
        fetch_html_with_browser=html_fetcher,
        _build_shared_browser_file_fetcher=file_fetcher_factory,
    )
    context = RuntimeContext(env={}, transport=Mock(), download_dir=tmp_path)
    client = client_type(Mock(), {}, deps=deps)
    discovery = client.discover_supplementary_files(doi, {}, context=context)
    # An unset profile must retain the runtime facade's non-optional default.
    assert client.require_profile().html_readiness is None
    assert "readiness" not in html_fetcher.call_args.kwargs
    assert discovery["diagnostics"]["identity"]["status"] == "match"
    assert len(discovery["assets"]) == 1
    assert discovery["assets"][0]["paper_doi"] == doi
    downloaded = client.download_supplementary_files(
        doi, {}, discovery, tmp_path, context=context
    )
    assert (
        file_fetcher_factory.call_args.kwargs["browser_options"].file_fetch_mode
        == "page_click"
    )
    assert not downloaded["asset_failures"]
    item = downloaded["assets"][0]
    assert Path(item["path"]).read_bytes() == payload
    assert item["sha256"] == hashlib.sha256(payload).hexdigest()
    assert item["content_type"] == "text/csv"
    assert item["route"] == "assets"
    assert item["validation"] == {
        "format": True,
        "size": True,
        "source": True,
        "structure": None,
    }
    file_fetcher.close.assert_called_once()


def test_elsevier_local_xml_discovery_does_not_assemble_body(tmp_path, monkeypatch):
    doi = "10.1016/example"
    body = b"""<full-text-retrieval-response xmlns:dc="http://purl.org/dc/elements/1.1/">
        <coredata><dc:identifier>doi:10.1016/example</dc:identifier></coredata>
        <attachment><attachment-type>MMC</attachment-type><attachment-eid>1-s2.0-EXAMPLE-mmc1</attachment-eid>
        <filename>sup-1.csv</filename><extension>csv</extension></attachment></full-text-retrieval-response>"""
    # XML identity uses prism:doi in the official representation.
    body = body.replace(
        b"<dc:identifier>doi:10.1016/example</dc:identifier>",
        b'<prism:doi xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">10.1016/example</prism:doi>',
    )
    transport = Mock()
    transport.request.return_value = {
        "status_code": 200,
        "headers": {"content-type": "text/xml"},
        "body": body,
        "url": "https://api.elsevier.com/content/article/doi/10.1016/example",
    }
    client = ElsevierClient(transport, {"ELSEVIER_API_KEY": "secret"})
    context = RuntimeContext(env={}, transport=transport, download_dir=tmp_path)
    discovery = client.discover_supplementary_files(doi, {}, context=context)
    assert discovery["diagnostics"]["identity"]["status"] == "match"
    assert len(discovery["assets"]) == 1
    assert discovery["assets"][0]["filename_hint"] == "sup-1.csv"
    assert transport.request.call_args.kwargs["headers"]["Accept"] == "text/xml"


def test_direct_http_stream_enters_raw_validation_without_converter(
    tmp_path, monkeypatch
):
    payload = make_pdf()
    transport = Mock()

    def stream(method, url, path, *, headers, options):
        Path(path).write_bytes(payload)
        return {
            "status_code": 200,
            "headers": {"content-type": "application/pdf"},
            "url": url,
        }

    transport.stream_to_file.side_effect = stream
    transport._streaming_ready = True
    monkeypatch.setattr(
        pdf,
        "_render_pdf_markdown_result_with_cache",
        Mock(side_effect=AssertionError("conversion forbidden")),
    )
    result = fallback.fetch_pdf_over_http(
        transport,
        ["https://pubs.acs.org/doi/pdf/10.1021/example"],
        artifact_dir=tmp_path,
        raw_only=True,
        request=fallback.PdfRequestContext(
            expected_identity={"doi": "10.1021/example"}
        ),
    )
    assert result.pdf_bytes == payload
    assert result.markdown_text == ""
    assert list(tmp_path.iterdir()) == []


def test_si_budget_rejects_before_publish_and_cancel_during_fetch_has_no_late_commit(
    tmp_path,
):
    from paper_fetch.asset_budget import AssetBudget

    assets = bind_supplementary_assets(
        [{"url": "https://publisher.test/data", "filename_hint": "data.csv"}],
        "10.1021/example",
        "https://publisher.test/article",
    )
    context = RuntimeContext(
        env={},
        transport=Mock(),
        download_dir=tmp_path,
        asset_budget=AssetBudget(max_bytes_per_asset=4),
    )
    result = original_asset_download(
        tmp_path, assets, {assets[0]["url"]: b"x,y\n1,2\n"}, context=context
    )
    assert not result["assets"]
    assert result["asset_failures"]
    assert not list(tmp_path.rglob("*.csv"))
    context = RuntimeContext(env={}, transport=Mock(), download_dir=tmp_path)

    def cancel_and_fetch(url, asset):
        context.fence_commits()
        return {
            "status_code": 200,
            "headers": {"content-type": "text/csv"},
            "body": b"x,y\n1,2",
            "url": url,
        }

    result = download_assets(
        SUPPLEMENTARY_KIND,
        Mock(),
        article_id="10.1021/example",
        assets=assets,
        output_dir=tmp_path,
        user_agent="test",
        options=AssetDownloadOptions(
            original_files=True,
            file_document_fetcher=cancel_and_fetch,
            fetch_policy="browser_only",
            asset_budget=context.asset_budget,
            artifact_store=context.artifact_store,
            runtime_context=context,
            asset_download_concurrency=1,
        ),
    )
    assert not list(tmp_path.rglob("*.csv"))


@pytest.mark.parametrize(
    "extension,root", [("docx", "word"), ("xlsx", "xl"), ("pptx", "ppt")]
)
def test_si_office_container_is_retained_without_unpacking(extension, root, tmp_path):
    from paper_fetch.extraction.html.assets.originals import (
        validate_supplementary_payload,
    )

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("[Content_Types].xml", "<Types/>")
        zipped.writestr(root + "/document.xml", "<document/>")
    payload = archive.getvalue()
    assert validate_supplementary_payload(payload, "data." + extension) == extension
    with pytest.raises(ValueError, match="office_container"):
        validate_supplementary_payload(
            payload, "data." + ("xlsx" if extension == "docx" else "docx")
        )
    assets = bind_supplementary_assets(
        [{"url": "https://publisher.test/si", "filename_hint": "data." + extension}],
        "10.1021/example",
        "https://publisher.test/article",
    )
    result = original_asset_download(tmp_path, assets, {assets[0]["url"]: payload})
    assert Path(result["assets"][0]["path"]).read_bytes() == payload
    assert not list(tmp_path.rglob("*.xml"))


@pytest.mark.parametrize(
    "status,name,payload,accepted",
    [
        (403, "data.txt", b"Forbidden", False),
        (200, "data.json", b"upstream broken", False),
        (200, "data.xml", b"upstream broken", False),
        (200, "data.xml", b"<service-error>RESOURCE_NOT_FOUND</service-error>", False),
        (200, "data.rtf", b"not RTF", False),
        (200, "data.csv", b"<html>challenge</html>", False),
        (200, "data.csv", b"condition,value\ncaptcha,1\n", True),
        (200, "data.txt", b"Research on access denied messages.", True),
        (200, "data.xml", b"<data><value>1</value></data>", True),
        (200, "data.json", b'{"value":1}', True),
        (200, "data.rtf", rb"{\rtf1 original text}", True),
    ],
)
def test_original_publish_boundary_rejects_http_and_invalid_text(
    tmp_path, status, name, payload, accepted
):
    from paper_fetch.asset_budget import AssetBudget
    from paper_fetch.artifacts import ArtifactStore
    from paper_fetch.extraction.html.assets.originals import publish_original_supplement

    url = "https://publisher.test/si/" + name
    asset = {
        "id": "si",
        "url": url,
        "filename_hint": name,
        "paper_doi": "10.1021/example",
        "discovery_url": "https://publisher.test/article",
    }
    response = {
        "status_code": status,
        "body": payload,
        "headers": {
            "content-type": {"json": "application/json", "xml": "application/xml"}.get(
                name.split(".")[-1], "text/plain"
            )
        },
    }
    budget = AssetBudget()
    with budget.reserve(declared_bytes=len(payload)) as reservation:
        reservation.consume(len(payload))
        kwargs: dict[str, Any] = dict(
            asset_dir=tmp_path,
            source_url=url,
            final_url=url,
            reservation=reservation,
            artifact_store=ArtifactStore.from_download_dir(tmp_path),
        )
        if accepted:
            result = publish_original_supplement(asset, response, **kwargs)
            assert Path(result["path"]).read_bytes() == payload
            assert result["format"] == name.split(".")[-1]
        else:
            with pytest.raises(ValueError):
                publish_original_supplement(asset, response, **kwargs)
            assert not list(tmp_path.rglob("*.*"))


def test_browser_original_non_success_is_perfile_failure_without_final_file(tmp_path):
    url = "https://publisher.test/si/data.txt"
    assets = bind_supplementary_assets(
        [{"url": url, "filename_hint": "data.txt"}],
        "10.1021/example",
        "https://publisher.test/article",
    )
    with RuntimeContext(download_dir=tmp_path) as context:
        result = download_assets(
            SUPPLEMENTARY_KIND,
            Mock(),
            article_id="10.1021/example",
            assets=assets,
            output_dir=tmp_path,
            user_agent="test",
            options=AssetDownloadOptions(
                original_files=True,
                fetch_policy="browser_only",
                asset_download_concurrency=1,
                file_document_fetcher=lambda *a: {
                    "status_code": 403,
                    "headers": {"content-type": "text/plain"},
                    "body": b"Forbidden",
                    "url": url,
                },
                asset_budget=context.asset_budget,
                artifact_store=context.artifact_store,
                runtime_context=context,
            ),
        )
    assert not result["assets"]
    assert len(result["asset_failures"]) == 1
    assert not list(tmp_path.rglob("*.txt"))
