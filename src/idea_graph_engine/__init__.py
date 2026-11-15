"""Движок «Графа идеи»: всё про агентов и этапы, без интерфейса.

Запуск Claude Code и OpenCode, ход пакета-этапа, ворота шагов, совет из трёх агентов,
независимое ревью, исследователи, предложения правок зависимого, банки. Интерфейс — в ``idea-graph-shell``.
"""

from .agents import Adapters
from .config import Settings
from .packages import Registry
from .errors import EngineError
from .runtime import PackageContext, Project, Run

__version__ = "0.2.0"

__all__ = ["Adapters", "EngineError", "PackageContext", "Project", "Registry", "Run", "Settings"]
