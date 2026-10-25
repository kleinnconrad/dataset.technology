"""Tests of the command-line checks of the entry point."""
import pytest

from main import main


def test_unknown_collector_in_the_environment_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("COLLECTORS", "technologies,unknown")
    assert main(["--output-dir", str(tmp_path)]) == 1
    assert not (tmp_path / "state").exists()


def test_unknown_collector_on_the_command_line_is_rejected(tmp_path) -> None:
    with pytest.raises(SystemExit):
        main(["--collectors", "unknown", "--output-dir", str(tmp_path)])


def test_dry_run_of_the_registry_collector_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("COLLECTORS", raising=False)
    assert main(["--dry-run", "--collectors", "technologies", "--output-dir", str(tmp_path)]) == 0
    assert not tmp_path.joinpath("technologies").exists()
