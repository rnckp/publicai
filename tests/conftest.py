"""Keep paid reviewer evaluations explicitly separate from ordinary test runs."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Require an explicit invocation even when provider credentials are present."""
    parser.addoption(
        "--run-review-evals",
        action="store_true",
        default=False,
        help="Run the fictional reviewer gold set against the configured model (paid API calls).",
    )
