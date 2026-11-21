"""The Grafana dashboard only uses metrics the API really exports."""

import json
import re
from pathlib import Path

import httpx
from fastapi import FastAPI

DASHBOARD = Path(__file__).parents[2] / "infra/grafana/provisioning/dashboards/helix-overview.json"
_SUFFIXES = ("_bucket", "_count", "_sum")


def _used_metrics() -> set[str]:
    dashboard = json.loads(DASHBOARD.read_text())
    exprs = [t["expr"] for panel in dashboard["panels"] for t in panel["targets"]]
    names = set()
    for expr in exprs:
        for name in re.findall(r"\b([a-z][a-z0-9_]+)\s*[\[{(]", expr):
            if name in {"sum", "max", "rate", "increase", "histogram_quantile", "by"}:
                continue
            for suffix in _SUFFIXES:
                name = name.removesuffix(suffix)
            names.add(name)
    return names


async def test_every_dashboard_metric_is_exported(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        await http.get("/healthz")  # so the request metrics have a sample
        exported = (await http.get("/metrics")).text
    families = set(re.findall(r"^# TYPE (\S+) ", exported, re.MULTILINE))
    families |= {f + "_total" for f in families}

    missing = sorted(name for name in _used_metrics() if name not in families)

    assert missing == []


def test_the_dashboard_is_provisioned_read_only() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    assert dashboard["editable"] is False
    assert len({panel["id"] for panel in dashboard["panels"]}) == len(dashboard["panels"])
