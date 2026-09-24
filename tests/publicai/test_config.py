"""Configuration boundaries do not widen the network policy."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from publicai.config import Settings, load_settings


def test_defaults_use_direct_openai_and_disable_telemetry() -> None:
    settings = Settings()
    assert settings.model.provider == "openai"
    assert settings.telemetry.enabled is False


def test_unknown_config_cannot_add_allowed_hosts(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("allowed_hosts: [evil.example]\n")
    with pytest.raises(ValidationError):
        load_settings(path)


def test_missing_explicit_config_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "missing.yaml")


def test_apertus_profile_is_separate_and_configurable(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("mode: apertus\napertus:\n  review_model: custom-review\n  request_limit: 7\n")
    settings = load_settings(path)
    assert settings.active_model.provider == "swisscom"
    assert settings.active_model.discovery_model == "swiss-ai/Apertus-v1.5-70B"
    assert settings.active_model.review_model == "custom-review"
    assert settings.active_model.request_limit == 7
    assert settings.model.discovery_model == "gpt-6-sol"


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(mode="unknown")


@pytest.mark.parametrize("rate", [0, -1, 5, float("nan"), float("inf")])
def test_apertus_rate_must_leave_headroom(rate: float) -> None:
    with pytest.raises(ValidationError):
        Settings(apertus={"requests_per_second": rate})
