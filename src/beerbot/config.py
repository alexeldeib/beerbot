"""Configuration management using pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # GroupMe
    beerbot_bot_id: str

    # Database
    database_url: str

    # Environment
    environment: str = "production"

    # Gemini
    gemini_api_key: str | None = None
    enable_image_analysis: bool = True

    # Agent
    agent_max_tool_calls: int = 5

    # Weekly recap
    weekly_recap_enabled: bool = True
    weekly_recap_hour: int = 21  # 9 PM ET

    # Admin (optional - required for admin endpoints)
    admin_token: str | None = None

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def image_analysis_enabled(self) -> bool:
        return self.enable_image_analysis and self.gemini_api_key is not None


settings = Settings()
