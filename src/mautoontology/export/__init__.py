"""Exporters: Apache Ossie, dbt MetricFlow, Snowflake semantic views, Databricks metric views, Markdown."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..model import Project

TARGETS = ("ossie", "dbt", "snowflake", "databricks", "markdown")


def export(project: Project, target: str, out_dir: str | Path) -> list[Path]:
    """Export the semantic layer to ``target``; returns the written files."""
    from . import databricks, dbt, markdown, ossie, snowflake

    writers: dict[str, Callable[[Project, Path], list[Path]]] = {
        "ossie": ossie.write,
        "dbt": dbt.write,
        "snowflake": snowflake.write,
        "databricks": databricks.write,
        "markdown": markdown.write,
    }
    if target not in writers:
        raise ValueError(f"unknown export target {target!r}; choose one of {', '.join(TARGETS)}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return writers[target](project, out)
