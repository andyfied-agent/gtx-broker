# GTX Broker packaging and release

The standalone package owns the broker routing and durable execution API. The
host repository supplies compute01 deployment, telemetry, and integration
configuration through an explicit package boundary.

## Versioning

The current release line is 0.1.x. The canonical version is
gtx_broker.version.__version__; pyproject.toml and the public package contract
must agree. Releases use annotated tags in the form vMAJOR.MINOR.PATCH.

## Continuous integration

The GitHub Actions CI workflow runs the full broker suite, installs the package
into the test environment, builds a source distribution and wheel, and checks
the resulting metadata. It does not require a workstation checkout.

## Publishing

The release workflow is tag-driven. A release is publishable only after the
tagged commit has passed CI and the repository has a configured PyPI trusted
publisher. No release tag or external publish is created by ordinary feature
branches.

## Public API

gtx_broker exports routing types and the durable execution outcome types from
gtx_broker.api. The second_shift namespace remains an intentional compatibility
surface until workstation integration has verified a pinned published release.
