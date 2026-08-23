from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from .models import GameReference, JsonObject, ProviderCapabilities, RuntimeProfile


class GameProvider(ABC):
    provider_id: str
    display_name: str

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    async def connection_status(self) -> JsonObject: ...

    @abstractmethod
    async def library(self) -> Sequence[GameReference]: ...

    @abstractmethod
    async def resolve_launch(self, game: GameReference) -> RuntimeProfile: ...

    def retained_installations(self) -> JsonObject:
        """Return verified, non-secret installation metadata safe to keep."""
        return {}

    def restore_retained_installations(self, payload: JsonObject) -> None:
        """Restore installation discovery metadata after provider state cleanup."""
        return None


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, GameProvider] = {}

    def register(self, provider: GameProvider) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"provider already registered: {provider.provider_id}")
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> GameProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise KeyError(f"unknown provider: {provider_id}") from exc

    def summaries(self) -> list[JsonObject]:
        return [
            {
                "id": item.provider_id,
                "name": item.display_name,
                "capabilities": item.capabilities().to_dict(),
            }
            for item in self._providers.values()
        ]
