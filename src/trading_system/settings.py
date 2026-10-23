"""Configuration and secret loading; .env files are deliberately not read."""

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal[1] = 1
    plan: Literal["free", "light", "standard", "premium"] = "standard"
    plan_evidence: str = "user_reported_2026-10-23"
    data_root: Path = Path("data/jquants")
    availability_rule: Literal["observed_at_plus_1_second_v1"] = "observed_at_plus_1_second_v1"
    single_writer: Literal[True] = True
    live_trading: Literal[False] = False

    @classmethod
    def load(cls, path: Path = Path("config/stage1.toml")) -> "Settings":
        with path.open("rb") as handle:
            return cls.model_validate(tomllib.load(handle))


def api_key() -> SecretStr | None:
    value = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not value and os.name == "nt":
        # Read the user's environment without printing values or requiring an app restart.
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, "JQUANTS_API_KEY")[0]).strip()
        except FileNotFoundError:
            pass
    return SecretStr(value) if value else None
