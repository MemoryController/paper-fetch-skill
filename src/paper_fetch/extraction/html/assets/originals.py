"""Validation and publication policy for original supplementary files only."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from ....artifacts import ArtifactStore
from ....http.headers import header_value
from ....utils import sanitize_filename
from ....xml_security import EXTERNAL_XML_LIMITS, parse_xml


def _validate_json(text: str) -> Any:
    # Bound nesting before the recursive stdlib decoder; strings do not nest.
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > EXTERNAL_XML_LIMITS.max_depth:
                raise ValueError("supplementary_json_depth_exceeded")
        elif char in "]}":
            depth -= 1

    def reject_constant(value: str) -> Any:
        raise ValueError("invalid_json_constant")

    value = json.loads(text, parse_constant=reject_constant)
    pending = [value]
    nodes = 0
    while pending:
        node = pending.pop()
        nodes += 1
        if nodes > EXTERNAL_XML_LIMITS.max_nodes:
            raise ValueError("supplementary_json_node_limit_exceeded")
        if isinstance(node, dict):
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return value


def validate_supplementary_payload(payload: bytes, filename: str) -> str:
    """Check recognizable original formats without conversion or decompression."""
    from ....providers._pdf_common import validate_original_pdf_bytes

    if not payload:
        raise ValueError("empty_supplementary_file")
    prefix = payload[:8192].lstrip().lower()
    if prefix.startswith((b"<!doctype html", b"<html")) or b"<html" in prefix:
        raise ValueError("supplementary_html_error")
    suffix = Path(filename).suffix.lower().lstrip(".")
    if payload.startswith(b"%PDF-"):
        validate_original_pdf_bytes(
            pdf_bytes=payload, source_url="", final_url="", supplementary=True
        )
        return "pdf"
    if suffix in {"zip", "docx", "xlsx", "pptx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = archive.namelist()
                office_root = {"docx": "word/", "xlsx": "xl/", "pptx": "ppt/"}.get(
                    suffix
                )
                if office_root and (
                    "[Content_Types].xml" not in names
                    or not any(name.startswith(office_root) for name in names)
                ):
                    raise ValueError("invalid_office_container")
        except zipfile.BadZipFile as exc:
            raise ValueError("invalid_zip_structure") from exc
        return suffix
    signatures = (
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", {"doc", "xls", "ppt"}),
        (b"\x89PNG\r\n\x1a\n", {"png"}),
        (b"\xff\xd8\xff", {"jpg", "jpeg"}),
        (b"GIF8", {"gif"}),
        (b"II*\x00", {"tif", "tiff"}),
        (b"MM\x00*", {"tif", "tiff"}),
        (b"\x1f\x8b", {"gz"}),
    )
    for magic, formats in signatures:
        if payload.startswith(magic) and suffix in formats:
            return suffix
    if suffix in {
        "csv",
        "tsv",
        "txt",
        "xml",
        "json",
        "dat",
        "xyz",
        "cif",
        "sdf",
        "mol",
        "rtf",
    }:
        if len(payload) > EXTERNAL_XML_LIMITS.max_bytes:
            raise ValueError("supplementary_text_size_exceeded")
        text = payload.decode("utf-8-sig")
        if "\x00" in text or not text.strip():
            raise ValueError("invalid_text_supplement")
        # Match whole error messages, not keywords in legitimate research data.
        if text.strip().casefold().rstrip(".!") in {
            "forbidden",
            "access denied",
            "verify you are human",
            "captcha",
            "just a moment",
            "resource_not_found",
            "upstream service failure",
        }:
            raise ValueError("supplementary_access_error")
        if suffix == "xml":
            root = parse_xml(payload, source="Original supplementary XML")
            if root.tag.rsplit("}", 1)[-1].casefold() in {
                "service-error",
                "error-response",
            }:
                raise ValueError("supplementary_service_error")
        elif suffix == "json":
            value = _validate_json(text)
            if isinstance(value, dict) and any(
                str(key).casefold() in {"service-error", "error-response"}
                for key in value
            ):
                raise ValueError("supplementary_service_error")
        elif suffix == "rtf" and not payload.lstrip().startswith(b"{\\rtf1"):
            raise ValueError("invalid_rtf_header")
        return suffix
    raise ValueError("unrecognized_supplementary_format")


def publish_original_supplement(
    asset: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    asset_dir: Path,
    source_url: str,
    final_url: str,
    reservation: Any,
    artifact_store: Any,
) -> dict[str, Any]:
    status = response.get("status_code")
    if not isinstance(status, int) or not 200 <= status < 300:
        raise ValueError("supplementary_http_status_not_success")
    staging = response.get("staging_path")
    payload = (
        Path(staging).read_bytes() if staging else bytes(response.get("body") or b"")
    )
    filename = str(asset.get("filename_hint") or Path(source_url.split("?", 1)[0]).name)
    actual_format = validate_supplementary_payload(payload, filename)
    stable_id = sanitize_filename(
        str(asset.get("id") or hashlib.sha256(source_url.encode()).hexdigest()[:16])
    )
    path = (
        asset_dir / "supplementary" / (stable_id + "-" + sanitize_filename(filename))
    ).resolve()
    store = (
        artifact_store
        if isinstance(artifact_store, ArtifactStore)
        else ArtifactStore.from_download_dir(asset_dir.parent)
    )
    if reservation is None:
        raise ValueError("missing_asset_reservation")
    with reservation.commit_critical_section():
        store.write_bytes_file(path, payload, overwrite=False)
        reservation.commit()
    return {
        "id": stable_id,
        "role": "supplementary",
        "kind": "supplementary",
        "status": "downloaded",
        "path": str(path),
        "size_bytes": len(payload),
        "downloaded_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "actual_format": actual_format,
        "format": actual_format,
        "declared_format": Path(filename).suffix.lstrip("."),
        "content_type": header_value(response.get("headers"), "content-type"),
        "source_url": final_url,
        "download_url": source_url,
        "discovery_url": asset.get("discovery_url"),
        "paper_doi": asset.get("paper_doi"),
        "validation": {
            "format": True,
            "size": True,
            "source": bool(
                asset.get("paper_doi")
                and asset.get("discovery_url")
                and asset.get("url") == source_url
            ),
            "structure": True if actual_format == "pdf" else None,
        },
    }
