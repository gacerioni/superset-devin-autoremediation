from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    # Devin
    devin_api_key: str = "cog_dev_local_unset"
    devin_org_id: str = "org_local_unset"
    devin_api_base: str = "https://api.devin.ai/v3"
    devin_mode: str = "mock"  # "mock" | "real"
    devin_max_acu_per_session: int = 3

    # GitHub
    github_token: str = ""
    github_repo: str = "gacerioni/superset"
    github_webhook_secret: str = ""
    github_remediate_label: str = "devin-remediate"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # App
    app_env: str = "dev"
    log_level: str = "INFO"
    poll_interval_seconds: int = 30


settings = Settings()
