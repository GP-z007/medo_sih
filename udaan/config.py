from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def public_url(value: str | None) -> str | None:
    if not value:
        return None
    p = urlsplit(value)
    host = p.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return urlunsplit((p.scheme, host + (f":{p.port}" if p.port else ""), p.path, "", ""))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)
    database_url: SecretStr = SecretStr("postgresql+psycopg://udaan:udaan@127.0.0.1:5432/udaan")
    api_url: str | None = None
    api_host: str = "127.0.0.1"
    api_port: int = Field(8000, ge=1, le=65535)
    api_token: SecretStr | None = None
    ai_provider: Literal["openai_compatible"] = "openai_compatible"
    ai_provider_name: str = "OpenAI-compatible"
    ai_base_url: str | None = None
    ai_model: str | None = None
    ai_api_key: SecretStr | None = None
    ai_timeout_seconds: float = Field(90, gt=0, le=300)
    ai_recipe_timeout_seconds: float = Field(180, gt=0, le=300)
    health_interval_seconds: float = Field(60, ge=1)
    weaviate_url: str | None = "http://127.0.0.1:8080"
    weaviate_api_key: SecretStr | None = None
    display: str | None = None
    browser_view_url: str = "http://127.0.0.1:6080/vnc.html"
    operator_wait_seconds: int = Field(900, ge=1, le=86400)
    worker_lease_seconds: int = Field(30, ge=10, le=300)
    collection_concurrency: int = Field(5, ge=1, le=5)
    export_directory: Path = Path("var/exports")
    recipe_directory: Path = Path("recipes")
    sandbox_image: str = "udaan-repair:local"
    default_resilience_profile: str = "Standard"
    global_ip_dataset_path: Path | None = None

    @property
    def ai_configured(self):
        return bool(self.ai_base_url and self.ai_model and self.ai_api_key
                    and self.ai_api_key.get_secret_value().strip())

    def require_ai(self):
        from udaan.contracts import DomainError
        if not self.ai_configured:
            raise DomainError("MODEL_NOT_CONFIGURED", "AI NOT CONFIGURED. Add an AI provider in Settings to build recipes automatically.")

    @field_validator("api_token", "ai_api_key", "weaviate_api_key", mode="before")
    @classmethod
    def optional_secret(cls, value):
        value = value.strip() if isinstance(value, str) else value
        return None if value is None or value == "" else value

    @field_validator("ai_base_url", "weaviate_url", "ai_model", "display", mode="before")
    @classmethod
    def blank_unconfigured(cls, value):
        return value.strip() or None if isinstance(value, str) else value

    @field_validator("ai_base_url", "weaviate_url", "api_url", "browser_view_url")
    @classmethod
    def endpoint(cls, value):
        if value is None:
            return value
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError("Use an HTTP(S) endpoint without embedded credentials, query, or fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def effective_api_address(self):
        """API_URL is canonical when set; otherwise derive it from API_HOST/API_PORT."""
        if self.api_url:
            parsed = urlsplit(self.api_url)
            if parsed.path not in {"", "/"}:
                raise ValueError("API_URL must not contain a path")
            self.api_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if self.api_host in {"127.0.0.1", "localhost", "::1"}:
                self.api_host = parsed.hostname or self.api_host
        else:
            client_host = "127.0.0.1" if self.api_host in {"0.0.0.0", "::"} else self.api_host
            host = f"[{client_host}]" if ":" in client_host else client_host
            self.api_url = f"http://{host}:{self.api_port}"
        return self


class ResilienceProfile(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str

    navigation_ms: int = Field(ge=10000, le=60000)
    element_ms: int = Field(ge=5000, le=25000)
    transient_retries: int = Field(ge=0, le=4)
    stability_ms: int = Field(ge=500, le=5000)
    verification_passes: int = Field(ge=1, le=3)
    # Number of fresh-page retries after the initial website collection attempt.
    # Total website attempts are therefore recovery_attempts + 1.
    recovery_attempts: int = Field(ge=0, le=5)
    recovery_level: Literal['standard', 'extended', 'strong_recovery']
    challenge_behavior: Literal['back_off', 'human_assisted']
    backoff_seconds: int = Field(ge=1, le=10)

    def __getitem__(self, key):
        # Compatibility for the already validated deterministic recipes.
        return getattr(self, key)


RESILIENCE = {
    'Standard': ResilienceProfile(name='Normal', navigation_ms=25000, element_ms=8000,
        transient_retries=2, stability_ms=1500, verification_passes=1, recovery_attempts=2,
        recovery_level='standard', challenge_behavior='back_off', backoff_seconds=1),
    'Conservative': ResilienceProfile(name='Careful', navigation_ms=35000, element_ms=15000,
        transient_retries=3, stability_ms=2000, verification_passes=2, recovery_attempts=3,
        recovery_level='extended', challenge_behavior='human_assisted', backoff_seconds=2),
    'Robust': ResilienceProfile(name='Strong Recovery', navigation_ms=50000, element_ms=20000,
        transient_retries=4, stability_ms=3000, verification_passes=3, recovery_attempts=5,
        recovery_level='strong_recovery', challenge_behavior='human_assisted', backoff_seconds=2),
}


@lru_cache
def settings() -> Settings:
    return Settings()
