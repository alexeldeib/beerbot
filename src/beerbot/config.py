"""Configuration management using pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # GroupMe
    beerbot_bot_id: str
    groupme_webhook_secret: str | None = None
    require_registered_groups: bool = True

    # Database
    database_url: str

    # Environment
    environment: str = "production"

    # LLM. The current runtime adapter is Google; the configuration names are
    # provider-neutral so an OpenAI-compatible backend can be added without
    # changing the agent's deployment contract.
    llm_provider: str = "google"
    llm_model: str = "gemini-3.6-flash"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_supports_images: bool = True
    llm_supports_video: bool = True
    llm_supports_tools: bool = True

    # Backward-compatible Google credential.
    gemini_api_key: str | None = None
    enable_image_analysis: bool = True

    # Agent
    agent_max_tool_calls: int = 5

    # Weekly recap
    weekly_recap_enabled: bool = True
    weekly_recap_hour: int = 21  # 9 PM ET

    # Admin (optional - required for admin endpoints)
    admin_token: str | None = None

    # Build metadata
    app_version: str = "0.2.0"
    git_sha: str = "unknown"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def image_analysis_enabled(self) -> bool:
        return (
            self.enable_image_analysis
            and self.model_api_key is not None
            and self.llm_supports_images
        )

    @property
    def video_analysis_enabled(self) -> bool:
        return self.image_analysis_enabled and self.llm_supports_video

    @property
    def model_api_key(self) -> str | None:
        return self.llm_api_key or self.gemini_api_key


settings = Settings()
