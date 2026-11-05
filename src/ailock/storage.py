"""Parquet writers. One file per (table, match) keeps ingest idempotent and parallel-safe."""
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from . import paths


def write_rows(path: Path, rows: list[dict]) -> None:
    if not rows:  # no schema to write; readers treat a missing file as "no data"
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    pq.write_table(pa.Table.from_pylist(rows), tmp, compression="zstd")
    tmp.replace(path)


def write_match_tables(match_id: int, tables: dict[str, list[dict]]) -> None:
    for name, rows in tables.items():
        if rows:
            write_rows(paths.TABLES / name / f"{match_id}.parquet", rows)


def has_match(match_id: int) -> bool:
    return (paths.TABLES / "matches" / f"{match_id}.parquet").exists()
