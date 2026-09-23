# GTX Broker

GTX Broker is the deterministic scoping and routing controller for the
compute01 coding workflow. It classifies a task, routes bounded implementation
work to the configured P40 worker, and escalates architectural or exhausted
work to Codex.

This repository owns the broker classifier, durable controller state model,
escalation policy, durable queue, dispatcher, and execution adapter. The
`second_shift` import namespace is retained as a compatibility API while
callers migrate to the broker package. Host/GPU launchers, telemetry
deployment, Telegram handlers, and worker profiles remain in their owning
repositories.

## Interface boundary

`GtxBrokerDirector` accepts a `RoutingRequest` and returns a
`RoutingResponse`. Persistence is supplied through a StateFile-compatible
object exposing `get_registry()` and `save_registry()`; the broker does not
depend on a workstation checkout path. The queue and execution interfaces are
available through the compatibility exports in `second_shift`.

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

The package has no runtime dependencies outside the Python standard library.
Workstation and Telegram integration remain separate deployment concerns and
are verified on compute01 before their local compatibility imports are removed.
