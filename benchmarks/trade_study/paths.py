"""Where a dataset's manifests and results live.

Results never share a file across transports: a dataset read over HTTPS is
filed under ``<name>-https``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks.trade_study.datasets import Dataset

RESULTS = Path("results/trade_study")


def tag(dataset: Dataset) -> str:
    """The dataset's name, with the transport when it is not direct S3."""
    return dataset.name if dataset.transport == "s3" else f"{dataset.name}-{dataset.transport}"


def gate_path(dataset: Dataset, window: str) -> Path:
    return RESULTS / "gates" / f"{tag(dataset)}-{window}.json"


def probes_path(dataset: Dataset) -> Path:
    return RESULTS / f"probes-{tag(dataset)}.json"


def superchunks_path(dataset: Dataset, window: str) -> Path:
    return RESULTS / f"superchunks-{tag(dataset)}-{window}.json"


def load_manifest(dataset: Dataset, window: str) -> dict[str, Any]:
    """The pinned granules, grid and snapshot ``run.py discover`` wrote."""
    path = dataset.manifest_path(window)
    if not path.exists():
        raise SystemExit(f"{path} missing: run `run.py discover --dataset {dataset.name} --window {window}` first")
    return json.loads(path.read_text())


def write_json(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2, default=str) + "\n")
