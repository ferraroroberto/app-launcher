"""Base class for CLI subcommands."""

from __future__ import annotations

import argparse
import logging
from abc import ABC, abstractmethod

from src import AppConfig

logger = logging.getLogger(__name__)


class BaseCommand(ABC):
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    @classmethod
    @abstractmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:  # pragma: no cover
        ...

    @abstractmethod
    def execute(self, args: argparse.Namespace) -> int:  # pragma: no cover
        ...
