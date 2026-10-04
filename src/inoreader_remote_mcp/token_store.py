"""The Inoreader tokens, in one private JSON file on the mounted volume."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .private_file import read_json, write_json


@dataclass(frozen=True)
class InoreaderTokens:
    access_token: str
    refresh_token: str
    # Epoch seconds.
    expires_at: float
    scope: str = ""


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> InoreaderTokens | None:
        raw = read_json(self.path)
        return InoreaderTokens(**raw) if raw is not None else None

    def save(self, tokens: InoreaderTokens) -> None:
        write_json(self.path, asdict(tokens))
