from __future__ import annotations

import asyncio
from dataclasses import replace

from paper_fetch.mcp._deps import default_mcp_deps
from paper_fetch.mcp.download_tool import download_paper_pdfs_tool_async
from paper_fetch.providers.base import ProviderFailure, FailureDiagnostics


def test_download_async_injection_progress_and_error_redaction():
    seen = []

    class Context:
        async def report_progress(self, **kwargs):
            seen.append(kwargs["progress"])

        async def log(self, *args, **kwargs):
            pass

    def download(query, *, output_dir, include_si, context):
        assert output_dir == "./papers" and include_si
        raise ProviderFailure(
            "no_access",
            "Blocked https://publisher.test/pdf?token=secret",
            diagnostics=FailureDiagnostics(details={"internal_seed": "secret"}),
        )

    result = asyncio.run(
        download_paper_pdfs_tool_async(
            query="10.1021/example",
            output_dir="./papers",
            ctx=Context(),
            deps=replace(default_mcp_deps(), service_download_paper_pdfs=download),
        )
    )
    assert result.is_error
    assert result.structured_content["code"] == "no_access"
    assert "secret" not in str(result.structured_content)
    assert seen == [0, 1, 2]
