from __future__ import annotations

from collections.abc import Sequence

from ..models import GameReference, JsonObject, ProviderCapabilities, RuntimeProfile
from ..provider import GameProvider


class DemoProvider(GameProvider):
    """Offline provider used to verify the full UI/backend contract safely."""

    provider_id = "demo"
    display_name = "provider.local_demo"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(owned_library=True, local_launch=True)

    async def connection_status(self) -> JsonObject:
        return {"state": "connected", "account": "offline-demo"}

    async def library(self) -> Sequence[GameReference]:
        return (
            GameReference("demo", "sample-1", "GameBridge Runtime Test"),
        )

    async def resolve_launch(self, game: GameReference) -> RuntimeProfile:
        raise RuntimeError("demo entries are not executable")
