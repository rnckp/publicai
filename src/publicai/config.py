"""Validated operator settings; website permissions are deliberately not configurable."""

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from publicai.crawler import CrawlSettings


class AgentMode(StrEnum):
    """Select an independent provider/model profile."""

    OPENAI = "openai"
    APERTUS = "apertus"


class ModelSettings(BaseModel):
    """Bound model requests independently from website request budgets."""

    model_config = ConfigDict(extra="forbid")
    discovery_model: str = "gpt-6-sol"
    review_model: str = "gpt-6-luna"
    temperature: float | None = Field(default=None, ge=0, le=2)
    reasoning_effort: Literal["low", "medium", "high"] = "low"
    max_tokens: int = Field(default=32000, ge=512, le=64000)
    timeout: float = Field(default=120, gt=0, le=300)
    retries: int = Field(default=2, ge=0, le=3)
    request_limit: int = Field(default=24, ge=1, le=100)
    tool_calls_limit: int = Field(default=80, ge=1, le=200)
    total_tokens_limit: int = Field(default=250000, ge=1000, le=1000000)


class OpenAISettings(ModelSettings):
    """Direct OpenAI model profile."""

    provider: Literal["openai"] = "openai"


class ApertusSettings(ModelSettings):
    """Swisscom profile with conservative per-run request pacing."""

    provider: Literal["swisscom"] = "swisscom"
    discovery_model: str = "swiss-ai/Apertus-v1.5-70B"
    review_model: str = "swiss-ai/Apertus-v1.5-70B"
    requests_per_second: float = Field(default=2, gt=0, le=4)


class TelemetrySettings(BaseModel):
    """Explicit opt-in for metadata-only telemetry."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    send_to_logfire: bool = False
    service_name: str = "municipality-factory"


class Settings(BaseModel):
    """Factory settings loaded from an operator-controlled YAML file."""

    model_config = ConfigDict(extra="forbid")
    mode: AgentMode = AgentMode.OPENAI
    apertus: ApertusSettings = Field(default_factory=ApertusSettings)
    model: OpenAISettings = Field(default_factory=OpenAISettings)
    web_search_enabled: bool = False
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    crawl: CrawlSettings = Field(default_factory=CrawlSettings)
    run_timeout: float = Field(default=600, gt=0, le=600)

    @property
    def active_model(self) -> OpenAISettings | ApertusSettings:
        """Return the selected profile without modifying the other mode's settings."""
        return self.apertus if self.mode == AgentMode.APERTUS else self.model


def load_settings(path: Path | None = None) -> Settings:
    """Load a small YAML document, rejecting misspelled or unsafe settings."""
    if path is None:
        path = Path("config.yaml")
        if not path.exists():
            return Settings()
    if path.stat().st_size > 65536:
        raise ValueError("Configuration exceeds 64 KiB")
    return Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
