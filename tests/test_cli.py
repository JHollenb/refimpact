from pathlib import Path

from typer.testing import CliRunner

from refimpact.cli import app
from refimpact.publisher import x_client


def test_publish_dry_run_needs_no_credentials(tmp_path):
    result = CliRunner().invoke(app, ["publish", "--db", str(tmp_path / "empty.sqlite")])
    assert result.exit_code == 0


def test_missing_credentials_names_only(monkeypatch):
    import pytest

    for key in [
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
        "X_EXPECTED_USER_ID",
    ]:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="Missing configuration: X_API_KEY"):
        x_client()


def test_wrong_account_is_rejected(monkeypatch):
    from types import SimpleNamespace

    import pytest

    for key in ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]:
        monkeypatch.setenv(key, "dummy-test-token")
    monkeypatch.setenv("X_EXPECTED_USER_ID", "123")
    monkeypatch.setattr(
        "tweepy.Client.get_me",
        lambda *args, **kwargs: SimpleNamespace(data=SimpleNamespace(id="456")),
    )
    with pytest.raises(ValueError, match="does not match"):
        x_client()


def test_compare_and_preview_cli(scorer, tmp_path):
    db = tmp_path / "cli.sqlite"
    result = CliRunner().invoke(
        app, ["compare", "examples/false-start.json", "--enqueue", "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    result = CliRunner().invoke(app, ["publish", "--db", str(db)])
    assert result.exit_code == 0
    assert "-1.1 pp" in result.output
    assert Path(db).is_file()
