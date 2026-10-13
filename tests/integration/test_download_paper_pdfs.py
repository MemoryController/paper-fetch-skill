"""Service/MCP persistence contracts against controlled provider boundaries."""

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading

from mcp import Client
import pytest

from paper_fetch.artifacts import ArtifactStore
from paper_fetch.http import RequestCancelledError
from paper_fetch.mcp._deps import default_mcp_deps
from paper_fetch.mcp.download_tool import download_paper_pdfs_tool_async
from paper_fetch.runtime import RuntimeContext
from paper_fetch.service import download_paper_pdfs
from paper_fetch.workflow.types import PaperFetchFailure
from tests.support.original_downloads import DOI, MAIN_BYTES, install_download_boundary


def test_original_bytes_hash_partial_and_idempotence(monkeypatch, tmp_path):
    client, providers = install_download_boundary(monkeypatch)
    monkeypatch.setattr(
        providers,
        "discover_supplementary_files",
        lambda *a, **k: {
            "status": "ok",
            "assets": [{"id": "csv"}, {"id": "zip"}, {"id": "missing"}],
            "browser_context_seed": {"cookies": "private-secret"},
            "source_url": "https://pubs.acs.org/si?token=private-secret",
        },
    )

    def supplementary(*args, context, **kwargs):
        destination = args[4]
        assets = []
        for identity, data in (
            ("csv", b"a,b\n1,2\n"),
            ("zip", b"PK\x03\x04original-archive"),
        ):
            path = destination / (identity + "." + identity)
            context.artifact_store.write_bytes_file(
                path, data, overwrite=False, commit_guard=context.commit_guard
            )
            assets.append(
                dict(
                    id=identity,
                    status="downloaded",
                    path=str(path),
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    format=identity,
                    source_url="https://pubs.acs.org/si?token=private-secret",
                    validation={
                        "format": True,
                        "size": True,
                        "source": True,
                        "structure": None,
                    },
                )
            )
        return {
            "assets": assets,
            "asset_failures": [
                {"id": "missing", "status": "failed", "code": "no_access"}
            ],
        }

    monkeypatch.setattr(providers, "download_supplementary_files", supplementary)
    # The old fulltext path must not run.
    monkeypatch.setattr(
        "paper_fetch.service.fetch_paper",
        lambda *a, **k: pytest.fail("conversion path"),
    )
    with RuntimeContext(clients={"acs": client}) as runtime:
        result = download_paper_pdfs(DOI, output_dir=str(tmp_path), context=runtime)
        again = download_paper_pdfs(DOI, output_dir=str(tmp_path), context=runtime)
    assert result["status"] == "partial" and not result["is_error"]
    assert result["acceptance"]["overall"] == "degraded"
    assert result["main_pdf"] == again["main_pdf"]
    assert Path(result["main_pdf"]["path"]).read_bytes() == MAIN_BYTES
    for item in result["supplementary"][:2]:
        body = Path(item["path"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == item["sha256"]
    assert result["supplementary"][2]["status"] == "failed"
    assert "private-secret" not in json.dumps(result)
    assert "browser_context_seed" not in json.dumps(result)
    assert not list(tmp_path.rglob("*.md")) and not list(tmp_path.rglob("*.json"))


def test_conflict_and_existing_identical_file_commit_guard(monkeypatch, tmp_path):
    client, _ = install_download_boundary(monkeypatch)
    with RuntimeContext(clients={"acs": client}) as runtime:
        first = download_paper_pdfs(
            DOI, output_dir=str(tmp_path), include_si=False, context=runtime
        )
        path = Path(first["main_pdf"]["path"])
        path.write_bytes(b"different")
        with pytest.raises(PaperFetchFailure, match="overwrite"):
            download_paper_pdfs(DOI, output_dir=str(tmp_path), context=runtime)
        assert path.read_bytes() == b"different"
        path.write_bytes(MAIN_BYTES)
        original = ArtifactStore.write_bytes_file

        def fenced(store, *args, **kwargs):
            runtime.fence_commits()
            return original(store, *args, **kwargs)

        monkeypatch.setattr(ArtifactStore, "write_bytes_file", fenced)
        with pytest.raises(RequestCancelledError):
            download_paper_pdfs(DOI, output_dir=str(tmp_path), context=runtime)
        assert path.read_bytes() == MAIN_BYTES


def test_registered_mcp_download_end_to_end(monkeypatch, tmp_path):
    import paper_fetch.mcp.server as server

    client, _ = install_download_boundary(monkeypatch)

    def service(query, **kwargs):
        kwargs["context"].clients = {"acs": client}
        return download_paper_pdfs(query, **kwargs)

    monkeypatch.setattr(
        server,
        "default_mcp_deps",
        lambda: replace(default_mcp_deps(), service_download_paper_pdfs=service),
    )

    async def run():
        async with Client(server.build_server()) as session:
            tools = await session.list_tools()
            tool = next(
                tool for tool in tools.tools if tool.name == "download_paper_pdfs"
            )
            assert not tool.annotations.read_only_hint
            result = await session.call_tool(
                "download_paper_pdfs",
                {"query": DOI, "output_dir": str(tmp_path), "include_si": False},
            )
            assert not result.is_error
            payload = result.structured_content
            assert payload["schema_version"] == 2
            assert payload["si_discovery"]["status"] == "not_requested"
            assert Path(payload["main_pdf"]["path"]).read_bytes() == MAIN_BYTES

    asyncio.run(run())


def test_mcp_cancel_fences_late_commit_and_closes_on_owner(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    facts = {}
    original_close = RuntimeContext.close_camoufox_for_current_thread

    def close(runtime):
        facts["close_thread"] = threading.get_ident()
        original_close(runtime)

    monkeypatch.setattr(RuntimeContext, "close_camoufox_for_current_thread", close)

    def service(query, *, context, **kwargs):
        facts["worker_thread"] = threading.get_ident()
        started.set()
        release.wait(5)
        try:
            store = ArtifactStore.from_download_dir(
                tmp_path, commit_guard=context.commit_guard
            )
            store.write_bytes_file(
                tmp_path / "late.pdf",
                MAIN_BYTES,
                overwrite=False,
                commit_guard=context.commit_guard,
            )
        except RequestCancelledError:
            facts["fenced"] = True
            raise
        finally:
            finished.set()

    async def run():
        task = asyncio.create_task(
            download_paper_pdfs_tool_async(
                query=DOI,
                output_dir=str(tmp_path),
                deps=replace(default_mcp_deps(), service_download_paper_pdfs=service),
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.02)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert finished.wait(2) and facts["fenced"]
    assert facts["close_thread"] == facts["worker_thread"]
    assert not (tmp_path / "late.pdf").exists()


@pytest.mark.parametrize("provider", ["wiley", "elsevier"])
def test_service_and_mcp_missing_raw_backend_requires_action(
    monkeypatch, tmp_path, provider
):
    import builtins
    from types import SimpleNamespace
    from unittest.mock import Mock
    from paper_fetch.providers.original_downloads import (
        download_primary_pdf as real_primary,
    )
    from paper_fetch.providers.wiley import WileyClient
    from paper_fetch.providers.elsevier import ElsevierClient
    import paper_fetch.workflow.downloads as workflow

    _, providers = install_download_boundary(monkeypatch)
    monkeypatch.setattr(providers, "download_primary_pdf", real_primary)
    monkeypatch.setattr(
        workflow,
        "build_official_provider_candidate_evidence",
        lambda *a, **k: [SimpleNamespace(provider=provider, strength="strong")],
    )
    transport = Mock()
    transport.request.return_value = {
        "status_code": 200,
        "body": b"%PDF-",
        "headers": {"content-type": "application/pdf"},
        "url": "https://api.publisher.test/main.pdf",
    }
    env = {"WILEY_TDM_CLIENT_TOKEN": "secret", "ELSEVIER_API_KEY": "secret"}
    client = (WileyClient if provider == "wiley" else ElsevierClient)(transport, env)
    original_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "pymupdf":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    from paper_fetch.providers.base import ProviderFailure

    with RuntimeContext(clients={provider: client}) as runtime:
        with pytest.raises(ProviderFailure) as caught:
            download_paper_pdfs(
                DOI, output_dir=str(tmp_path), include_si=False, context=runtime
            )
    assert caught.value.code == "not_configured"
    assert "PyMuPDF is required" in str(caught.value)

    def service(query, **kwargs):
        kwargs["context"].clients = {provider: client}
        return download_paper_pdfs(query, **kwargs)

    result = asyncio.run(
        download_paper_pdfs_tool_async(
            query=DOI,
            output_dir=str(tmp_path),
            include_si=False,
            deps=replace(default_mcp_deps(), service_download_paper_pdfs=service),
        )
    )
    payload = result.structured_content
    assert result.is_error
    assert payload["code"] == "not_configured"
    assert "PyMuPDF is required" in payload["reason"]
    assert payload["acceptance"]["overall"] == "action_required"
    assert not list(tmp_path.rglob("*.pdf"))


@pytest.mark.parametrize("missing_structure", [False, True])
def test_acs_real_si_publisher_format_through_service_and_mcp(
    monkeypatch, tmp_path, missing_structure
):
    from types import SimpleNamespace
    from unittest.mock import Mock
    import pymupdf
    from paper_fetch.providers import _pdf_common as pdf
    from paper_fetch.providers.original_downloads import (
        download_primary_pdf as real_primary,
        download_supplementary_files as real_supplementary,
    )
    from paper_fetch.providers.acs import AcsClient
    from paper_fetch.providers.browser_workflow.shared import (
        default_browser_workflow_deps,
    )
    from paper_fetch.providers.browser_workflow.originals import (
        bind_supplementary_assets,
    )
    from paper_fetch.extraction.html.assets import (
        download_assets as real_download_assets,
    )

    _, providers = install_download_boundary(monkeypatch)
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Original research article\n" + DOI)
        main_bytes = document.tobytes()
    source = "https://pubs.acs.org/doi/" + DOI
    assets = bind_supplementary_assets(
        [
            {"url": "https://pubs.acs.org/si/data.pdf", "filename_hint": "data.pdf"},
            {"url": "https://pubs.acs.org/si/data.csv", "filename_hint": "data.csv"},
        ],
        DOI,
        source,
    )
    bodies = {assets[0]["url"]: main_bytes, assets[1]["url"]: b"x,y\n1,2\n"}
    fetcher = Mock(
        side_effect=lambda url, asset: {
            "status_code": 200,
            "headers": {"content-type": "application/octet-stream"},
            "body": bodies[url],
            "url": url,
        }
    )

    def download_assets(*args, **kwargs):
        result = real_download_assets(*args, **kwargs)
        if missing_structure:
            # Corrupt only the real producer fact, never invent format fields.
            next(item for item in result["assets"] if item["actual_format"] == "pdf")[
                "validation"
            ]["structure"] = False
        return result

    deps = replace(
        default_browser_workflow_deps(),
        load_runtime_config=Mock(return_value=SimpleNamespace()),
        ensure_runtime_ready=Mock(),
        _build_shared_browser_file_fetcher=Mock(return_value=fetcher),
        download_assets=download_assets,
    )
    client = AcsClient(Mock(), {}, deps=deps)

    def primary(*args, **kwargs):
        result = pdf.validate_original_pdf_bytes(
            pdf_bytes=main_bytes,
            source_url=source,
            final_url=source,
            expected_identity={"doi": DOI},
        )
        return replace(
            result, diagnostics={**result.diagnostics, "route": "browser_pdf"}
        )

    monkeypatch.setattr(client, "download_primary_pdf", primary)
    monkeypatch.setattr(
        client,
        "discover_supplementary_files",
        lambda *a, **k: {"status": "ok", "assets": assets, "source_url": source},
    )
    monkeypatch.setattr(providers, "download_primary_pdf", real_primary)
    monkeypatch.setattr(providers, "download_supplementary_files", real_supplementary)
    # Restore discovery seam too, so the actual client local method is consumed.
    # install_download_boundary replaced the module attribute; reload is avoided.
    monkeypatch.setattr(
        providers,
        "discover_supplementary_files",
        lambda selected, *a, **k: selected.discover_supplementary_files(*a, **k),
    )

    def service(query, **kwargs):
        kwargs["context"].clients = {"acs": client}
        return download_paper_pdfs(query, **kwargs)

    with RuntimeContext(clients={"acs": client}) as runtime:
        payload = download_paper_pdfs(DOI, output_dir=str(tmp_path), context=runtime)
    mcp_result = asyncio.run(
        download_paper_pdfs_tool_async(
            query=DOI,
            output_dir=str(tmp_path),
            deps=replace(default_mcp_deps(), service_download_paper_pdfs=service),
        )
    )
    for result in (payload, mcp_result.structured_content):
        si_pdf, si_csv = result["supplementary"]
        if missing_structure:
            assert result["status"] == "partial"
            assert si_pdf["status"] == "failed"
            assert si_pdf["code"] == "invalid_published_facts"
        else:
            assert result["status"] == "ok"
            assert si_pdf["format"] == "pdf"
            assert si_pdf["validation"]["structure"] is True
            assert Path(si_pdf["path"]).read_bytes() == main_bytes
        assert si_csv["format"] == "csv"
        assert Path(si_csv["path"]).read_bytes() == bodies[assets[1]["url"]]
        assert si_csv["sha256"] == hashlib.sha256(bodies[assets[1]["url"]]).hexdigest()
