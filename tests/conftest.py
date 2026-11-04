import json
from pathlib import Path

import pytest

from refimpact.models import Scorer
from refimpact.schema import Comparison, Estimate, Impact


@pytest.fixture
def comparison():
    return Comparison.model_validate_json(Path("examples/false-start.json").read_text())


@pytest.fixture
def impact(comparison):
    return Impact(
        comparison=comparison,
        model_id="test-model",
        before=Estimate(ep=2, wp=0.5),
        actual=Estimate(ep=1.5, wp=0.47),
        alternative=Estimate(ep=2, wp=0.5),
        actual_epa=-0.5,
        alternative_epa=0,
        epa_impact=-0.5,
        wp_swing_pp=-3,
    )


@pytest.fixture(scope="session")
def scorer():
    directory = Path(".cache/models")
    if not (directory / "manifest.json").exists():
        pytest.skip("Run uv run refimpact models-fetch for model integration tests")
    return Scorer(directory)


@pytest.fixture(scope="session")
def published():
    return json.loads(Path("tests/fixtures/published.json").read_text())
