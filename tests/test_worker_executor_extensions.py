from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentmesh.extensions.runtime import RuntimeExtensionRegistry
from agentmesh.extensions.sdk import ExtensionContext, InvalidRuntimeExtension
from agentmesh.extensions.trust import LockedExtension
from agentmesh.features import FeatureGateSet
from tests.test_runtime_extensions import _definition


def make(factory=None, *, trust="local"):
    definition = replace(_definition(), worker_executor_factory=factory)
    registry = RuntimeExtensionRegistry()
    registry.register(
        definition,
        trust=replace(LockedExtension.unmanaged(definition.manifest), trust=trust),
    )
    return registry


def choose(registry, *, identifier="example.extension", enabled="example.extension", gates=None):
    fallback = SimpleNamespace(execute=lambda **kwargs: {"summary": "standard"})
    context = ExtensionContext(tenant_id="tenant", services={"private-service": object()})
    result = registry.worker_executor(
        identifier,
        enabled=enabled,
        context=context,
        feature_gates=gates or FeatureGateSet.from_config("full", ""),
        fallback=fallback,
    )
    return result, fallback


def test_default_does_not_construct_or_import_an_execution_route():
    def forbidden(*args):
        pytest.fail("Disabled hook constructed an executor")

    result, fallback = choose(make(forbidden), identifier=None)
    assert result is fallback


def test_trusted_factory_receives_only_explicit_worker_capabilities_and_fallback():
    def factory(context, fallback):
        assert context.tenant_id == "tenant"
        assert context.has("private-service") and not context.has("admin-token")
        return SimpleNamespace(execute=lambda **kwargs: fallback.execute(**kwargs))

    result, fallback = choose(make(factory))
    assert result is not fallback and result.execute() == {"summary": "standard"}


@pytest.mark.parametrize("trust", ["unverified", "unmanaged"])
def test_untrusted_worker_code_is_not_constructed(trust):
    def forbidden(*args):
        pytest.fail("Untrusted factory executed")

    with pytest.raises(InvalidRuntimeExtension, match="locked trusted"):
        choose(make(forbidden, trust=trust))


def test_explicit_route_misconfiguration_never_silently_falls_back():
    with pytest.raises(InvalidRuntimeExtension, match="not enabled"):
        choose(make(), enabled="another.extension")
    with pytest.raises(InvalidRuntimeExtension, match="no worker executor"):
        choose(make())
    with pytest.raises(InvalidRuntimeExtension, match="implement execute"):
        choose(make(lambda *args: object()))
    with pytest.raises(InvalidRuntimeExtension, match="features"):
        choose(make(), gates=FeatureGateSet.from_config("minimal", ""))
    with pytest.raises(RuntimeError, match="factory unavailable"):
        choose(make(lambda *args: (_ for _ in ()).throw(RuntimeError("factory unavailable"))))
