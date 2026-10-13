"""Provider-owned original-file helpers; no article bootstrap or rendering."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from bs4 import BeautifulSoup

from ...publisher_identity import validate_extracted_identity
from ...quality.access_boundary import raise_for_paywall
from ..base import ProviderFailure


def bind_supplementary_assets(
    assets: Sequence[Mapping[str, Any]], doi: str, source_url: str
) -> list[dict[str, Any]]:
    bound = []
    seen = set()
    for item in assets:
        asset = dict(item)
        url = str(asset.get("url") or asset.get("source_url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        asset.update(
            url=url,
            id=hashlib.sha256((doi + "\n" + url).encode()).hexdigest()[:16],
            paper_doi=doi,
            discovery_url=source_url,
            kind="supplementary",
        )
        bound.append(asset)
    return bound


def validate_discovery_identity(
    html: str, doi: str, metadata: Mapping[str, Any], source_url: str
) -> dict[str, Any]:
    raise_for_paywall(html.encode(), source_url=source_url)
    soup = BeautifulSoup(html, "lxml")

    def citation(name):
        node = soup.find("meta", attrs={"name": name})
        return str(node.get("content") or "") if node else ""

    evidence = {
        "doi": citation("citation_doi") or citation("dc.Identifier"),
        "title": citation("citation_title"),
        "method": "publisher_html_metadata",
    }
    identity = validate_extracted_identity({**dict(metadata), "doi": doi}, {}, evidence)
    if identity.status != "match":
        raise ProviderFailure(
            "NO_RESULT", "Supplementary discovery page identity is not verified."
        )
    return identity.to_dict()


def supplementary_download_report(
    result: Mapping[str, Any],
    assets: Sequence[Mapping[str, Any]],
    provider: str,
    *,
    route: str = "assets",
) -> dict[str, Any]:
    """Attach owner and discovery facts to both completed and failed files."""
    by_url = {str(asset["url"]): asset for asset in assets}
    report: dict[str, list[dict[str, Any]]] = {"assets": [], "asset_failures": []}
    for key in report:
        for outcome in result.get(key) or []:
            item = dict(outcome)
            candidate = by_url.get(
                str(item.get("download_url") or item.get("source_url") or ""), {}
            )
            item.update(provider=provider, route=route)
            for field in ("id", "paper_doi", "discovery_url"):
                if field in candidate:
                    item.setdefault(field, candidate[field])
            if key == "asset_failures":
                item.setdefault("role", "supplementary")
                if isinstance(item.get("status"), int):
                    item["http_status"] = item.pop("status")
                item["status"] = "failed"
                item.setdefault(
                    "code", item.get("reason") or "supplementary_download_failed"
                )
            report[key].append(item)
    recorded = {item.get("id") for outcomes in report.values() for item in outcomes}
    for asset in assets:
        if asset["id"] not in recorded:
            report["asset_failures"].append(
                {
                    "id": asset["id"],
                    "role": "supplementary",
                    "status": "failed",
                    "code": "supplementary_not_completed",
                    "reason": "Download stopped before this file completed.",
                    "source_url": asset["url"],
                    "paper_doi": asset["paper_doi"],
                    "discovery_url": asset["discovery_url"],
                    "provider": provider,
                    "route": route,
                }
            )
    return report
