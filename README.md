# GTX Broker

GTX Broker is the deterministic scoping and routing controller for the
compute01 coding workflow. It classifies a task, routes bounded implementation
work to the configured P40 worker, and escalates architectural or exhausted
work to Codex.

This repository is the first extraction boundary. It currently contains the
broker classifier, durable controller state model, escalation policy, and
controller contract tests. Host/GPU launchers, telemetry deployment, Telegram
handlers, and worker execution remain in the workstation and bot repositories
until their migration contracts are implemented.

## Interface boundary

`GtxBrokerDirector` accepts a `RoutingRequest` and returns a
`RoutingResponse`. Persistence is supplied through a small StateFile-compatible
object exposing `get_registry()` and `save_registry()`; the broker does not
depend on a workstation import path.

```python
from gtx_broker import GtxBrokerDirector, RoutingRequest

director = GtxBrokerDirector(state_file)
response = director.route_request(RoutingRequest(
    task_id="TASK-001",
    task_description="Implement the requested feature",
    context_size=65536,
    quality_importance=0.8,
    workflow="direct_escalation",
))
```

## Development

```text
python3 -m pytest -q
```

The extraction intentionally starts with a standalone, dependency-free core.
The workstation adapter migration and removal of duplicate code are later gates
and must be verified on compute01 before deletion.
