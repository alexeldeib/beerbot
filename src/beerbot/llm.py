"""Provider-neutral model configuration and capability boundary.

The current agent still uses Google's automatic function calling. Keeping the
provider profile in one module makes that dependency explicit and gives the
next agent-loop iteration a stable place to add Google and OpenAI-compatible
adapters (including self-hosted multimodal models) without leaking deployment
configuration throughout the application.
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

    provider = settings.llm_provider if isinstance(settings.llm_provider, str) else "google"
    model = settings.llm_model if isinstance(settings.llm_model, str) else "gemini-3.6-flash"
    base_url = settings.llm_base_url if isinstance(settings.llm_base_url, str) else None

    return ModelProfile(
        provider=provider.lower(),
        model=model,
        base_url=base_url,
        capabilities=ModelCapabilities(
            images=settings.llm_supports_images,
            video=settings.llm_supports_video,
            tools=settings.llm_supports_tools,
        ),
    )
