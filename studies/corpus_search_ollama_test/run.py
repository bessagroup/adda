"""Custom-graph runner verifying BACKLOG #32's CorpusSearch/CorpusAdd fix.

Graph: strategizer (entry) -> literature_reviewer.

literature_reviewer points at a local endpoint only when config.yaml carries
its `nodes.literature_reviewer` block (model, backend, base_url); strategizer
stays on Haiku regardless -- mirrors
studies/mathexpert_kinematic_matching_test/run.py's isolation pattern.

Usage:
  uv run python studies/corpus_search_ollama_test/run.py
"""
from __future__ import annotations

from pathlib import Path

from adda import AgenticRun, Edge, Graph, LiteratureReviewAgent, StrategizerAgent

STUDY_DIR = Path(__file__).parent


def build_graph() -> Graph:
    return Graph(
        nodes={
            "strategizer": StrategizerAgent(),
            "literature_reviewer": LiteratureReviewAgent(),
        },
        edges=(Edge("strategizer", "literature_reviewer"),),
        entry="strategizer",
    )


def main() -> None:
    report = AgenticRun(
        study_dir=STUDY_DIR,
        graph=build_graph(),
        interactive=False,
    ).execute()
    print(report)


if __name__ == "__main__":
    main()
