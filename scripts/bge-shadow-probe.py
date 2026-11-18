"""Ad-hoc probe: run one real run_shadow against the live mddb-embed +
reranker. Usage:
    set -a; source ~/.config/secrets/ada-lab.env; set +a
    PYTHONPATH=. .venv/bin/python scripts/bge-shadow-probe.py"""
import asyncio
import logging
from types import SimpleNamespace

from backend import bge_memory_shadow

logging.basicConfig(level=logging.INFO, format="%(message)s")

BANKS = [
    SimpleNamespace(name="general", mddb_collection="ada-ha-bank-general",
                    writable=True, status_outcome=False, scope="person"),
    SimpleNamespace(name="family", mddb_collection="ada-ha-bank-family",
                    writable=True, status_outcome=False, scope="person"),
    SimpleNamespace(name="projects", mddb_collection="ada-ha-bank-projects",
                    writable=True, status_outcome=False, scope="person"),
    SimpleNamespace(name="technical",
                    mddb_collection="ada-ha-bank-technical",
                    writable=True, status_outcome=False, scope="person"),
]


async def main() -> None:
    metrics = await bge_memory_shadow.run_shadow(
        query="where is the gate remote",
        banks=BANKS,
        baseline_ids=[],
        instance="tony",
    )
    print({k: metrics[k] for k in sorted(metrics)})


if __name__ == "__main__":
    asyncio.run(main())
