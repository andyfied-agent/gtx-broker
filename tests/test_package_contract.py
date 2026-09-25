from __future__ import annotations

import tomllib
from pathlib import Path

import gtx_broker


def test_project_version_matches_public_package_version():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == gtx_broker.__version__


def test_public_api_exports_routing_execution_and_outcomes():
    expected = {
        "GtxBrokerDirector",
        "RoutingRequest",
        "RoutingResponse",
        "BacklogExecutionAdapter",
        "ExecutionRequest",
        "ExecutionOutcome",
        "VerificationResult",
        "WorkerResult",
    }

    assert expected.issubset(set(gtx_broker.__all__))
    for name in expected:
        assert hasattr(gtx_broker, name)
