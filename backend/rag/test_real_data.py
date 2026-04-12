from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.rag import build_rag_service, load_rag_config


def main() -> None:
    config = load_rag_config()
    sample_files = [
        PROJECT_ROOT / "raw_data" / "python" / "docs_html" / "python-3.13-docs-html" / "tutorial" / "classes.html",
        PROJECT_ROOT / "raw_data" / "python" / "docs_html" / "python-3.13-docs-html" / "tutorial" / "controlflow.html",
        PROJECT_ROOT / "raw_data" / "python" / "docs_html" / "python-3.13-docs-html" / "tutorial" / "modules.html",
        PROJECT_ROOT / "raw_data" / "python" / "docs_html" / "python-3.13-docs-html" / "reference" / "compound_stmts.html",
        PROJECT_ROOT / "raw_data" / "pytorch" / "official_tutorials" / "beginner_source" / "deep_learning_60min_blitz.rst",
        PROJECT_ROOT / "raw_data" / "pytorch" / "official_tutorials" / "beginner_source" / "nn_tutorial.py",
        PROJECT_ROOT / "raw_data" / "pytorch" / "official_tutorials" / "beginner_source" / "transfer_learning_tutorial.py",
    ]

    config = replace(
        config,
        qdrant=replace(
            config.qdrant,
            path=(PROJECT_ROOT / "backend" / "rag" / "storage" / "qdrant_real_data_test"),
            collection_name="raw_data_real_test",
        ),
    )

    rag = build_rag_service(config=config)
    try:
        summary = rag.ingest_paths(sample_files)
        print("INGEST SUMMARY")
        print(summary)
        print()

        queries = [
            "Python 中如何定义类？",
            "What does the Python for statement do?",
            "How do I build a neural network in PyTorch?",
        ]

        for query in queries:
            result = rag.search(query, top_k=3)
            print(f"QUERY: {query}")
            for index, hit in enumerate(result.hits, start=1):
                preview = hit.text[:220].replace("\n", " ")
                print(
                    f"  {index}. source={hit.source_name} chunk={hit.chunk_index + 1}/{hit.chunk_count} "
                    f"score={hit.score:.4f} preview={preview}"
                )
            context = rag.build_context(query, top_k=2)
            print("  CONTEXT PREVIEW:")
            print(f"  {context.context_text[:500].replace(chr(10), ' ')}")
            print()
    finally:
        rag.close()


if __name__ == "__main__":
    main()
