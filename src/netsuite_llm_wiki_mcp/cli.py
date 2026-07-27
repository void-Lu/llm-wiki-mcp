from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from netsuite_llm_wiki_mcp.platform_paths import global_config_path
from netsuite_llm_wiki_mcp.retrieval_eval import (
    RetrievalEvalError,
    load_retrieval_dataset,
    run_retrieval_evaluation,
    write_retrieval_eval_report,
)
from netsuite_llm_wiki_mcp.runtime_config import RuntimeConfig, RuntimeConfigError, resolve_runtime_config, write_global_config
from netsuite_llm_wiki_mcp.vector_index import VectorIndexError, VectorIndexStore, parse_vector_settings
from netsuite_llm_wiki_mcp.vector_provider import LocalBgeM3Provider, VectorProviderError, local_provider_readiness
from netsuite_llm_wiki_mcp.wiki_query import DEFAULT_TOP_K, vector_index_records


def _runtime_payload(runtime: RuntimeConfig) -> dict[str, Any]:
    return {
        "ok": True,
        "vault_root": str(runtime.vault_root),
        "vault_name": runtime.vault_name,
        "resolution_source": runtime.resolution_source,
        "config_path": str(runtime.global_config_path),
        "global_config_path": str(runtime.global_config_path),
        "sources_config_path": str(runtime.sources_config_path),
        "sources_config_exists": runtime.sources_config_path.exists(),
        "data_root": str(runtime.data_root),
        "user_data_root": str(runtime.user_data_root),
        "vault_data_root": str(runtime.vault_data_root),
        "vault_storage_dir": str(runtime.vault_storage_dir),
        "vault_storage_id": runtime.vault_storage_id,
        "chroma_path": str(runtime.chroma_path),
        "manifest_path": str(runtime.manifest_path),
        "embedding_cache_path": str(runtime.embedding_cache_path),
        "model_cache_path": str(runtime.embedding_cache_path),
    }


def _error_payload(code: str, message: str, *, config_path: Path | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "code": code, "error": message}
    if config_path is not None:
        payload["config_path"] = str(config_path)
    return payload


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="netsuite-llm-wiki-mcp")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Create or update the user-level vault config.")
    init_parser.add_argument("--vault", required=True, help="Name to register for this vault.")
    init_parser.add_argument("--root", required=True, help="Path to the Obsidian vault root.")
    init_parser.add_argument("--default", action="store_true", help="Mark this vault as the default vault.")

    status_parser = subparsers.add_parser("status", help="Print resolved runtime diagnostics.")
    status_parser.add_argument("--root", help="Optional vault root override for diagnostics.")

    evaluation_parser = subparsers.add_parser("retrieval-eval", help="Run a read-only retrieval evaluation dataset.")
    evaluation_parser.add_argument("--vault", required=True, help="Path to the vault to query without modifying it.")
    evaluation_parser.add_argument("--dataset", required=True, help="Path to the retrieval evaluation JSONL cases.")
    evaluation_parser.add_argument("--manifest", help="Optional JSON manifest path; defaults beside the dataset.")
    evaluation_parser.add_argument("--output-dir", required=True, help="Directory for retrieval-eval.json and retrieval-eval.md.")
    evaluation_parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help=f"Candidate limit (default: {DEFAULT_TOP_K}).")
    evaluation_parser.add_argument("--repeats", type=int, default=1, help="Repeats per case for deterministic ranking checks.")
    evaluation_parser.add_argument("--no-context-budget", action="store_true", help="Skip the separate context budget measurement pass.")
    evaluation_parser.add_argument("--context-budget-case-limit", type=int, default=1, help="Number of leading cases measured in the separate context budget pass (default: 1).")
    evaluation_parser.add_argument("--retrieval-mode", choices=("lexical", "vector", "hybrid"), default="lexical", help="Evaluation path (default: lexical).")
    evaluation_parser.add_argument("--vector-model-path", help="Required local BGE-M3 directory for vector or hybrid evaluation.")
    evaluation_parser.add_argument("--vector-index-path", help="Optional vault-relative vector index directory for vector or hybrid evaluation.")

    vector_parser = subparsers.add_parser("vector", help="Manage the explicit local vector index lifecycle.")
    vector_actions = vector_parser.add_subparsers(dest="vector_action", required=True)
    for action in ("status", "build", "update"):
        action_parser = vector_actions.add_parser(action, help=f"{action.title()} a local vector index.")
        action_parser.add_argument("--vault", required=True, help="Path to the vault whose vector index is managed.")
        action_parser.add_argument("--index-path", help="Optional vault-relative vector index directory.")
        action_parser.add_argument("--include-raw-sources", action="store_true", help="Explicitly include raw sources in this index.")
        action_parser.add_argument("--model-path", required=action != "status", help="Local BGE-M3 model directory; never downloaded automatically.")
        action_parser.add_argument("--device", default="cpu", help="Sentence-transformers device (default: cpu).")
        action_parser.add_argument("--batch-size", type=int, default=16, help="Embedding batch size (default: 16).")
        action_parser.add_argument("--max-sequence-length", type=int, default=256, help="Maximum BGE-M3 input tokens (default: 256).")

    subparsers.add_parser("server", help="Run the MCP server.")
    return parser


def _run_init(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    if not root.exists() or not root.is_dir():
        _print_json(_error_payload("invalid_vault_root", f"Vault root does not exist or is not a directory: {root}"))
        return 2

    config_path = write_global_config(
        global_config_path(),
        vault_name=args.vault,
        vault_root=root,
        make_default=bool(args.default),
    )
    runtime = resolve_runtime_config(vault_root_arg=root, config_path=config_path, require_sources_config=False)
    _print_json(_runtime_payload(runtime))
    return 0


def _run_status(args: argparse.Namespace) -> int:
    runtime = resolve_runtime_config(vault_root_arg=args.root, require_sources_config=False)
    payload = _runtime_payload(runtime)
    if not runtime.sources_config_path.exists():
        payload.update(
            {
                "ok": False,
                "code": "missing_sources_config",
                "error": (
                    f"Vault root {runtime.vault_root} does not contain rag/sources.yaml. "
                    f"Create {runtime.sources_config_path} before indexing, or run "
                    "`netsuite-llm-wiki-mcp init --vault <name> --root <vault-path> --default` "
                    "after creating it."
                ),
            }
        )
        _print_json(payload)
        return 2

    _print_json(payload)
    return 0


def _run_retrieval_eval(args: argparse.Namespace) -> int:
    if args.retrieval_mode != "lexical" and not args.vector_model_path:
        raise RetrievalEvalError("vector_config_missing", "--vector-model-path is required for vector and hybrid evaluation")
    dataset = load_retrieval_dataset(args.dataset, args.manifest)
    report = run_retrieval_evaluation(
        args.vault,
        dataset,
        top_k=args.top_k,
        repeats=args.repeats,
        measure_context_budget=not args.no_context_budget,
        context_budget_case_limit=args.context_budget_case_limit,
        retrieval_mode=args.retrieval_mode,
        vector_config=(
            {
                "provider": "local_bge_m3",
                "model_path": args.vector_model_path,
                "index_path": args.vector_index_path,
            }
            if args.retrieval_mode != "lexical"
            else None
        ),
    )
    reports = write_retrieval_eval_report(report, args.output_dir)
    _print_json(
        {
            "ok": True,
            "dataset_id": report["metadata"]["dataset_id"],
            "dataset_revision": report["metadata"]["dataset_revision"],
            "metrics": report["metrics"],
            "reports": reports,
        }
    )
    return 0


def _vector_config_from_args(args: argparse.Namespace) -> dict[str, object]:
    return {
        "provider": "local_bge_m3",
        "model_path": args.model_path,
        "index_path": args.index_path,
        "device": args.device,
        "batch_size": args.batch_size,
        "max_sequence_length": args.max_sequence_length,
    }


def _run_vector(args: argparse.Namespace) -> int:
    settings = parse_vector_settings(args.vault, _vector_config_from_args(args))
    store = VectorIndexStore(args.vault, settings.index_path)
    records = vector_index_records(args.vault, include_raw_sources=bool(args.include_raw_sources))
    if args.vector_action == "status":
        status = store.status(records, include_raw_sources=bool(args.include_raw_sources))
        status["local_provider"] = local_provider_readiness(settings.model_path)
        _print_json(status)
        return 0 if status.get("ok") else 2

    if settings.model_path is None:
        raise VectorIndexError("model_missing", "--model-path is required for vector build and update")
    provider = LocalBgeM3Provider(
        settings.model_path,
        device=settings.device,
        batch_size=settings.batch_size,
        max_sequence_length=settings.max_sequence_length,
    )
    if args.vector_action == "build":
        _print_json(store.build(records, provider, include_raw_sources=bool(args.include_raw_sources)))
        return 0
    if args.vector_action == "update":
        _print_json(store.update(records, provider, include_raw_sources=bool(args.include_raw_sources)))
        return 0
    raise ValueError(f"unknown vector action: {args.vector_action}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command in {None, "server"}:
        from netsuite_llm_wiki_mcp.server import main as server_main

        server_main()
        return 0

    try:
        if args.command == "init":
            return _run_init(args)
        if args.command == "status":
            return _run_status(args)
        if args.command == "retrieval-eval":
            return _run_retrieval_eval(args)
        if args.command == "vector":
            return _run_vector(args)
    except RuntimeConfigError as exc:
        _print_json(_error_payload(exc.code, str(exc), config_path=exc.config_path))
        return 2
    except RetrievalEvalError as exc:
        _print_json(_error_payload(exc.code, str(exc)))
        return 2
    except (VectorIndexError, VectorProviderError) as exc:
        _print_json(_error_payload(exc.code, str(exc)))
        return 2
    except ValueError as exc:
        _print_json(_error_payload("invalid_config", str(exc)))
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
