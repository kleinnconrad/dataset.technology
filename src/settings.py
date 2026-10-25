"""
Runtime settings of the pipeline, read from environment variables.

Only values that may change between runs are settings. Fixed thresholds are
module constants in the modules that use them.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

USER_AGENT = "TechDataset/0.1 (+https://github.com/kleinnconrad/dataset.technology)"


def _env_int(name: str, default: int) -> int:
    """Reads a positive integer from an environment variable.

    Args:
        name: Name of the environment variable.
        default: Value used when the variable is unset or empty.

    Returns:
        int: The parsed value.

    Raises:
        ValueError: If the variable is set but is not an integer of at least 1.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from e
    if value < 1:
        raise ValueError(f"Environment variable {name} must be at least 1, got {value}")
    return value


def _env_secret(name: str) -> Optional[str]:
    """Reads a secret and removes surrounding whitespace and quotes.

    Args:
        name: Name of the environment variable.

    Returns:
        Optional[str]: The value, or None if it is unset or empty.
    """
    raw = os.getenv(name, "").strip().strip("\"'").strip()
    return raw or None


def default_output_dir() -> Path:
    """Returns the folder for records and state.

    In GitHub Actions this is ``data/`` in the repository, which the workflow
    commits. Locally it is a folder in Downloads, so that test runs do not
    change the committed dataset.

    Returns:
        Path: The output folder.
    """
    if os.getenv("GITHUB_ACTIONS") == "true":
        return Path("data")
    return Path.home() / "Downloads" / "tech_dataset"


@dataclass(frozen=True)
class Settings:
    """Settings that can be changed per run without code changes.

    Attributes:
        github_token (Optional[str]): Token for the GitHub REST API. In GitHub
            Actions the workflow passes its GITHUB_TOKEN; without a token the
            GitHub collector stops at the limit of 60 requests per hour.
        stackexchange_key (Optional[str]): Stack Exchange API key; raises the daily
            quota from 300 requests per IP address to 10,000.
        nvd_api_key (Optional[str]): NVD API key; raises the NVD rate limit from
            5 to 50 requests per 30 seconds.
        run_deadline_minutes (int): Minutes after the start of the run after which
            the collectors start no new work, so that the workflow can commit
            before its timeout.
        collectors (Optional[tuple[str, ...]]): Names of the collectors to run;
            None runs all that are due.
        output_dir (Path): Folder for the datasets and ``state/``.
    """
    github_token: Optional[str] = None
    stackexchange_key: Optional[str] = None
    nvd_api_key: Optional[str] = None
    run_deadline_minutes: int = 40
    collectors: Optional[tuple[str, ...]] = None
    output_dir: Path = Path("data")

    @property
    def state_dir(self) -> Path:
        """Folder of the state files."""
        return self.output_dir / "state"

    @classmethod
    def from_env(cls) -> "Settings":
        """Builds the settings from environment variables, falling back to the defaults.

        Returns:
            Settings: The settings for this run.
        """
        collectors_raw = os.getenv("COLLECTORS", "").strip()
        collectors = tuple(name.strip() for name in collectors_raw.split(",") if name.strip()) or None
        return cls(
            github_token=_env_secret("GITHUB_TOKEN"),
            stackexchange_key=_env_secret("STACKEXCHANGE_KEY"),
            nvd_api_key=_env_secret("NVD_API_KEY"),
            run_deadline_minutes=_env_int("RUN_DEADLINE_MINUTES", cls.run_deadline_minutes),
            collectors=collectors,
            output_dir=default_output_dir(),
        )
