from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from netsuite_llm_wiki_mcp.platform_paths import global_config_path
from netsuite_llm_wiki_mcp.runtime_config import RuntimeConfig, RuntimeConfigError, resolve_runtime_config, write_global_config


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
    except RuntimeConfigError as exc:
        _print_json(_error_payload(exc.code, str(exc), config_path=exc.config_path))
        return 2
    except ValueError as exc:
        _print_json(_error_payload("invalid_config", str(exc)))
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())