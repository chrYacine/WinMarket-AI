"""Access to the bundled example AO files (data/ao_examples/*.txt).

Serves the "Appels d'offres en stock" source mode of the analysis screen —
sample inputs only, never an input to any scoring value.
"""
from src.core.config import DATA_DIR

EXAMPLES_DIR = DATA_DIR / "ao_examples"


def list_examples() -> list[dict]:
    if not EXAMPLES_DIR.exists():
        return []
    return [
        {"id": p.stem, "label": p.stem.replace("_", " ")}
        for p in sorted(EXAMPLES_DIR.glob("*.txt"))
    ]


def read_example(example_id: str) -> str | None:
    path = (EXAMPLES_DIR / f"{example_id}.txt").resolve()
    if not path.is_file() or EXAMPLES_DIR.resolve() not in path.parents:
        return None
    return path.read_text(encoding="utf-8")
