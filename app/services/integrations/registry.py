"""
app/services/integrations/registry.py

Lets you add a new vendor by writing ONE new file and decorating its class
— no changes needed to integrations.py, webhook.py, or any router. The
router/sync-service looks connectors up by name string (stored in
integration_connections.vendor), never imports a vendor class directly.

Usage in a new vendor file, e.g. fireblocks.py:

    from app.services.integrations.registry import register_connector
    from app.services.integrations.base import IntegrationConnector

    @register_connector("fireblocks")
    class FireblocksConnector(IntegrationConnector):
        vendor_name = "fireblocks"
        ...

Then anywhere else:

    from app.services.integrations.registry import get_connector_class
    cls = get_connector_class("fireblocks")
    connector = cls(tenant_id, credentials)
"""
from __future__ import annotations

from app.services.integrations.base import IntegrationConnector

_REGISTRY: dict[str, type[IntegrationConnector]] = {}


def register_connector(vendor_name: str):
    def _wrap(cls: type[IntegrationConnector]) -> type[IntegrationConnector]:
        if vendor_name in _REGISTRY:
            raise ValueError(f"Connector for '{vendor_name}' already registered")
        cls.vendor_name = vendor_name
        _REGISTRY[vendor_name] = cls
        return cls
    return _wrap


def get_connector_class(vendor_name: str) -> type[IntegrationConnector]:
    try:
        return _REGISTRY[vendor_name]
    except KeyError:
        raise ValueError(
            f"No connector registered for vendor '{vendor_name}'. "
            f"Available: {sorted(_REGISTRY)}"
        )


def list_available_vendors() -> list[str]:
    return sorted(_REGISTRY)


def _import_all_connectors() -> None:
    """
    Side-effect import so every @register_connector decorator runs once at
    startup. Call this from main.py's startup event. Add new vendor modules
    to this list as you write them — that's the only router-adjacent file
    a new vendor needs to touch.
    """
    from app.services.integrations import stub_connector  # noqa: F401
    # from app.services.integrations import fireblocks       # noqa: F401  (once written)
    # from app.services.integrations import sardine           # noqa: F401  (once written)
