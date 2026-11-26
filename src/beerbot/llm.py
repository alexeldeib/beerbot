"""Provider-neutral model configuration and capability boundary.

Google and OpenAI-compatible sessions implement the explicit tool loop in
model_runtime.py. This module owns non-secret endpoint configuration and
capability metadata; provider-specific conversation state stays in the adapters.
"""

from dataclasses import dataclass

from .config import Settings


@dataclass(frozen=True)
class ModelCapabilities:
    """Features the configured endpoint is expected to support."""

    images: bool
    video: bool
    tools: bool


@dataclass(frozen=True)
class ModelProfile:
    """Public, non-secret description of a configured model endpoint."""

    provider: str
    model: str
    base_url: str | None
    capabilities: ModelCapabilities


def model_profile(settings: Settings) -> ModelProfile:
    """Build the configured model profile without exposing credentials."""
    return ModelProfile(
        provider=settings.llm_provider,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        capabilities=ModelCapabilities(
            images=settings.llm_supports_images,
            video=settings.llm_supports_video,
            tools=settings.llm_supports_tools,
        ),
    )
