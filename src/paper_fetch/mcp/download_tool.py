"""Original-file download tool; no fetch envelope or cache sidecars."""

from __future__ import annotations

import asyncio
from pathlib import Path
import threading

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult

from ..runtime import RuntimeContext
from ._deps import MCPDeps, default_mcp_deps
from .batch import report_progress, run_blocking_call
from .log_bridge import PaperFetchLogBridge
from .results import _tool_result, error_payload_from_exception
from .schemas import DownloadPaperPdfsRequest


async def download_paper_pdfs_tool_async(
    *,
    query: str,
    output_dir: str,
    include_si: bool = True,
    ctx: Context | None = None,
    deps: MCPDeps = default_mcp_deps(),
) -> CallToolResult:
    await report_progress(ctx, 0, 2, "Validating original-file download")
    cancelled = threading.Event()
    runtime = None
    try:
        request = DownloadPaperPdfsRequest.model_validate(
            dict(query=query, output_dir=output_dir, include_si=include_si)
        )
        runtime = RuntimeContext(
            env=deps.build_runtime_env(None),
            download_dir=Path(request.output_dir).expanduser(),
            cancel_check=cancelled.is_set,
        )

        def download():
            assert runtime is not None
            try:
                payload = deps.service_download_paper_pdfs(
                    request.query,
                    output_dir=request.output_dir,
                    include_si=request.include_si,
                    context=runtime,
                )
                runtime.raise_if_cancelled()
                return payload
            finally:
                runtime.close_camoufox_for_current_thread()

        await report_progress(ctx, 1, 2, "Downloading original files")
        bridge = (
            PaperFetchLogBridge(ctx=ctx, loop=asyncio.get_running_loop())
            if ctx
            else None
        )
        if bridge is None:
            payload = await run_blocking_call(
                download, cancel_event=cancelled, cancel_fence=runtime.fence_commits
            )
        else:
            with bridge:
                payload = await run_blocking_call(
                    download, cancel_event=cancelled, cancel_fence=runtime.fence_commits
                )
        await report_progress(ctx, 2, 2, "Original-file download complete")
        return _tool_result(payload, is_error=bool(payload.get("is_error")))
    except asyncio.CancelledError:
        cancelled.set()
        if runtime is not None:
            runtime.fence_commits()
        raise
    except Exception as error:
        from ..workflow.downloads import public_download_data

        payload = public_download_data(error_payload_from_exception(error))
        payload["details"] = {}
        payload["candidates"] = [
            {
                key: candidate[key]
                for key in ("doi", "title", "landing_url", "provider")
                if key in candidate
            }
            for candidate in payload.get("candidates") or []
        ]
        from ..tracing import TraceEvent

        payload["trace"] = [
            {
                key: value
                for key, value in event.items()
                if key in TraceEvent.__dataclass_fields__
            }
            for event in payload.get("trace") or []
        ]
        payload["main_pdf"] = {
            "id": "main_pdf",
            "role": "main_pdf",
            "status": "failed",
            "code": payload["code"],
            "reason": payload["reason"],
        }
        payload["supplementary"] = []
        payload["si_discovery"] = {
            "status": "not_requested",
            "reason": "Main PDF was not published.",
        }
        from ..workflow.acceptance import evaluate_download_acceptance

        payload["acceptance"] = evaluate_download_acceptance(
            payload["main_pdf"], [], discovery_status="not_requested"
        ).model_dump(mode="json")
        payload["is_error"] = True
        await report_progress(ctx, 2, 2, "Original-file download failed")
        return _tool_result(payload, is_error=True)
    finally:
        if runtime is not None:
            runtime.close()
