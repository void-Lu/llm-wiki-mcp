from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Sequence

from runtime.platform_paths import global_config_path
from wiki.ingest_service import sync_retrieval_index
from retrieval.retrieval_index import RetrievalIndexStore
from retrieval.retrieval_eval import (
    RetrievalEvalError,
    evaluate_retrieval_gate,
    load_retrieval_dataset,
    run_retrieval_evaluation,
    write_retrieval_eval_report,
)
from retrieval.query_quality_calibration import (
    DEFAULT_MINIMUM_SAMPLE_COUNT,
    CalibrationGenerationError,
    generate_calibration_artifact,
    identity_from_manifest,
    load_calibration_observations,
    read_json_object,
    write_calibration_outputs,
)
from retrieval.retrieval_gold import RetrievalGoldError, finalize_retrieval_gold, sample_retrieval_gold
from runtime.runtime_config import ConfigRegistry, ResolvedVault, RuntimeConfig, RuntimeConfigError, VaultSettings, resolve_runtime_config, vault_storage_id, write_global_config
from retrieval.query_recall_policy import DEFAULT_TOP_K
from retrieval.vector_index import VectorIndexError, VectorIndexStore, default_vector_index_path, parse_vector_settings, vector_index_records
from retrieval.vector_provider import LocalBgeM3Provider, VectorProviderError, local_provider_readiness
from archive.archive_migration import apply_legacy_migration, plan_legacy_migration
from archive.archive_status_reader import ArchiveStatusReader
from archive.archive_service import ArchiveService
from wiki.page_repair import PageRepairService
from wiki.privacy_audit import PrivacyAuditError, PrivacyAuditService
from wiki.provenance_migration import ProvenanceMigrationError, ProvenanceMigrationService
from wiki.raw_replacement import RawReplacementError, RawReplacementService
from wiki.wiki_paths import PAGE_STATE_DB, STATE_DB


def _status_resolution(runtime: RuntimeConfig) -> tuple[ConfigRegistry, ResolvedVault]:
    registry = ConfigRegistry.from_file(runtime.global_config_path)
    configured = registry.config.vaults.get(runtime.vault_name)
    if configured is None or configured.root != runtime.vault_root:
        configured = next(
            (settings for settings in registry.config.vaults.values() if settings.root == runtime.vault_root),
            None,
        )
    if configured is not None:
        return registry, ResolvedVault(configured.name, configured.root, configured, "config")

    source = "env" if runtime.resolution_source == "env" else "legacy"
    return registry, ResolvedVault(runtime.vault_name, runtime.vault_root, VaultSettings(runtime.vault_name, runtime.vault_root), source)


def _storage_relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _runtime_payload(runtime: RuntimeConfig) -> dict[str, Any]:
    registry, resolution = _status_resolution(runtime)
    status = registry.public_status(resolution)
    root = resolution.root
    storage_paths = {
        "state": _storage_relative(root, root / STATE_DB),
        "page_state": _storage_relative(root, root / PAGE_STATE_DB),
        "retrieval_index": _storage_relative(root, RetrievalIndexStore(root).path),
        "vector_index": _storage_relative(root, default_vector_index_path(root)),
        "archives": f"{_storage_relative(root, root / 'archives')}/",
    }
    return {
        "ok": True,
        **status,
        "vault_root": str(runtime.vault_root),
        "vault": resolution.name,
        "storage_id": vault_storage_id(runtime.vault_root),
        "storage_paths": storage_paths,
        "config_path": str(runtime.global_config_path),
        "global_config_path": str(runtime.global_config_path),
        "sources_config_path": str(runtime.sources_config_path),
        "sources_config_exists": runtime.sources_config_path.exists(),
    }


def _error_payload(code: str, message: str, *, config_path: Path | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "code": code, "error": message}
    if config_path is not None:
        payload["config_path"] = str(config_path)
    return payload


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-wiki-mcp")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Create or update the user-level vault config.")
    init_parser.add_argument("--vault", required=True, help="Name to register for this vault.")
    init_parser.add_argument("--root", required=True, help="Path to the Obsidian vault root.")
    init_parser.add_argument("--default", action="store_true", help="Mark this vault as the default vault.")

    status_parser = subparsers.add_parser("status", help="Print resolved runtime diagnostics.")
    status_parser.add_argument("--root", help="Optional vault root override for diagnostics.")

    config_parser = subparsers.add_parser("config", help="Validate, display, and update user-level runtime configuration.")
    config_actions = config_parser.add_subparsers(dest="config_action", required=True)
    config_actions.add_parser("validate", help="Validate config.yaml without changing it.")
    config_show = config_actions.add_parser("show", help="Show a redacted configuration snapshot.")
    config_show.add_argument("--vault", help="Logical vault name; defaults to default_vault.")
    retrieval = config_actions.add_parser("set-retrieval", help="Set local retrieval settings for a logical vault.")
    retrieval.add_argument("--vault", required=True)
    retrieval.add_argument("--model-path", required=True)
    retrieval.add_argument("--device", default="cpu")
    retrieval.add_argument("--batch-size", type=int, default=16)
    retrieval.add_argument("--max-sequence-length", type=int, default=256)
    retrieval.add_argument("--candidate-limit", type=int, default=50)
    retrieval.add_argument("--rrf-k", type=int, default=60)
    retrieval.add_argument("--min-vector-score", type=float, default=0.5)
    privacy = config_actions.add_parser("set-privacy", help="Set privacy policy (redaction rules are never echoed by status).")
    privacy.add_argument("--vault", required=True)
    privacy.add_argument("--redaction-rule-version", default="v1")
    privacy.add_argument("--pii-policy", choices=("preserve", "redact"), default="preserve")
    privacy.add_argument("--disable-credential-redaction", action="store_true")
    telemetry = config_actions.add_parser("set-telemetry", help="Set bounded query telemetry retention.")
    telemetry.add_argument("--vault", required=True)
    telemetry.add_argument("--retention-days", type=int, required=True)
    telemetry.add_argument("--disable", action="store_true")
    archive = config_actions.add_parser("set-archive", help="Set archive index/snapshot/purge policy.")
    archive.add_argument("--vault", required=True)
    archive.add_argument("--snapshot-ttl-days", type=int, default=7)
    archive.add_argument("--archive-index-disabled", action="store_true")
    archive.add_argument("--automatic-purge", action="store_true")
    archive.add_argument("--purge-after-days", type=int)

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
    evaluation_parser.add_argument("--entrypoint", choices=("engine", "mcp"), default="engine", help="Evaluation boundary (default: engine; mcp is lexical-only).")
    evaluation_parser.add_argument("--query-version", choices=("v2",), default="v2", help="Query contract to evaluate (only v2 is supported).")
    evaluation_parser.add_argument("--scope", choices=("auto", "knowledge", "history", "all", "archive", "raw"), default=None, help="Optional fixed Query V2 corpus scope; omitted uses each case scope.")
    evaluation_parser.add_argument("--baseline-report", help="Optional frozen retrieval-eval.json used for the regression gate.")
    evaluation_parser.add_argument("--vector-model-path", help="Required local BGE-M3 directory for vector or hybrid evaluation.")
    evaluation_parser.add_argument("--vector-index-path", help="Optional vault-relative vector index directory for vector or hybrid evaluation.")

    quality_gate_parser = subparsers.add_parser("quality-gate", help="Offline quality-gate calibration administration.")
    quality_gate_actions = quality_gate_parser.add_subparsers(dest="quality_gate_action", required=True)
    calibrate_parser = quality_gate_actions.add_parser("calibrate", help="Generate a branch-relative calibration artifact and report.")
    calibrate_parser.add_argument("--dataset", required=True, help="Frozen calibration/shadow JSONL input (not sent to Query V2).")
    calibrate_parser.add_argument("--observations", help="Optional shadow decision JSONL; defaults to --dataset.")
    calibrate_parser.add_argument("--manifest", help="Frozen dataset manifest; defaults to <dataset-stem>.manifest.json when present.")
    calibrate_parser.add_argument("--output-dir", required=True, help="Directory for the artifact and safe calibration reports.")
    calibrate_parser.add_argument("--calibration-revision", default="calibration-unproven", help="Artifact calibration revision label.")
    calibrate_parser.add_argument("--minimum-sample-count", type=int, default=DEFAULT_MINIMUM_SAMPLE_COUNT, help=f"Minimum samples per bucket (default: {DEFAULT_MINIMUM_SAMPLE_COUNT}).")

    gold_sample_parser = subparsers.add_parser("retrieval-gold-sample", help="Create a redacted read-only telemetry annotation template.")
    gold_sample_parser.add_argument("--vault", required=True, help="Path to the vault whose telemetry is sampled read-only.")
    gold_sample_parser.add_argument("--output-dir", required=True, help="Directory for gold-template.jsonl and its manifest.")
    gold_sample_parser.add_argument("--count", type=int, default=50, help="Number of deduplicated cases to sample (default: 50).")
    gold_sample_parser.add_argument("--seed", default="wiki-query-real-vault-v1", help="Stable sampling seed.")
    gold_sample_parser.add_argument("--dataset-id", default="codingwork-wiki-query-gold", help="Dataset identifier written to the manifest.")

    gold_finalize_parser = subparsers.add_parser("retrieval-gold-finalize", help="Validate annotations and materialize retrieval gold JSONL.")
    gold_finalize_parser.add_argument("--vault", required=True, help="Path to the vault used to validate relevant paths.")
    gold_finalize_parser.add_argument("--template", required=True, help="Completed gold-template.jsonl path.")
    gold_finalize_parser.add_argument("--manifest", required=True, help="gold-template.manifest.json path.")
    gold_finalize_parser.add_argument("--output-dir", required=True, help="Directory for gold.jsonl and gold.manifest.json.")

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

    index_parser = subparsers.add_parser("index", help="Manage explicit passage FTS lifecycle.")
    index_actions = index_parser.add_subparsers(dest="index_action", required=True)
    for action in ("status", "build", "update"):
        action_parser = index_actions.add_parser(action, help=f"{action.title()} a passage retrieval store.")
        action_parser.add_argument("--vault", required=True)
        action_parser.add_argument("--scope", choices=("active", "archive", "raw"), default="active")

    archive_parser = subparsers.add_parser("archive", help="Admin-only archive lifecycle maintenance.")
    archive_actions = archive_parser.add_subparsers(dest="archive_action", required=True)
    archive_status = archive_actions.add_parser("status"); archive_status.add_argument("--vault", required=True)
    archive_recover = archive_actions.add_parser("recover"); archive_recover.add_argument("--vault", required=True)
    archive_rebuild = archive_actions.add_parser("rebuild-index"); archive_rebuild.add_argument("--vault", required=True)
    archive_purge = archive_actions.add_parser("purge"); archive_purge.add_argument("--vault", required=True); archive_purge.add_argument("--archive-id", required=True); archive_purge.add_argument("--forget", action="store_true"); archive_purge.add_argument("--authorize", action="store_true")
    archive_migrate = archive_actions.add_parser("migrate"); archive_migrate.add_argument("--vault", required=True); archive_migrate.add_argument("--apply", action="store_true")

    raw_replace_parser = subparsers.add_parser("raw-replace", help="Admin-only replacement of an external Raw Source tree.")
    raw_replace_actions = raw_replace_parser.add_subparsers(dest="raw_replace_action", required=True)
    raw_replace_plan = raw_replace_actions.add_parser("plan", help="Plan a Raw Source tree replacement without changing Wiki facts.")
    raw_replace_plan.add_argument("--vault", required=True)
    raw_replace_plan.add_argument("--source-root", required=True)
    raw_replace_plan.add_argument("--target-path", required=True, help="Vault-relative Raw Source tree to replace.")
    raw_replace_apply = raw_replace_actions.add_parser("apply", help="Apply a previously reviewed Raw Source replacement plan.")
    raw_replace_apply.add_argument("--vault", required=True)
    raw_replace_apply.add_argument("--plan-id", required=True)
    raw_replace_recover = raw_replace_actions.add_parser("recover", help="Recover an interrupted Raw Source replacement from its external backup.")
    raw_replace_recover.add_argument("--vault", required=True)
    raw_replace_recover.add_argument("--plan-id", required=True)

    repair_parser = subparsers.add_parser("repair", help="Admin-only repair of committed page projections.")
    repair_actions = repair_parser.add_subparsers(dest="repair_action", required=True)
    page_operation = repair_actions.add_parser("page-operation", help="Inspect or repair page operation journal entries.")
    page_operation_actions = page_operation.add_subparsers(dest="page_operation_action", required=True)
    page_plan = page_operation_actions.add_parser("plan", help="List pending page operations without changing projections.")
    page_plan.add_argument("--vault", required=True)
    page_plan.add_argument("--operation-id")
    page_apply = page_operation_actions.add_parser("apply", help="Repair one page operation's projections.")
    page_apply.add_argument("--vault", required=True)
    page_apply.add_argument("--operation-id", required=True)

    provenance = repair_actions.add_parser("provenance", help="Plan or apply strict raw-source provenance migration.")
    provenance_actions = provenance.add_subparsers(dest="provenance_action", required=True)
    provenance_plan = provenance_actions.add_parser("plan", help="Classify provenance without modifying page bytes.")
    provenance_plan.add_argument("--vault", required=True)
    provenance_plan.add_argument("--page-path")
    provenance_apply = provenance_actions.add_parser("apply", help="Apply a provenance plan after page/source CAS checks.")
    provenance_apply.add_argument("--vault", required=True)
    provenance_apply.add_argument("--plan-id", required=True)

    privacy_audit = repair_actions.add_parser("privacy-audit", help="Plan or apply an explicit historical privacy audit.")
    privacy_actions = privacy_audit.add_subparsers(dest="privacy_audit_action", required=True)
    privacy_plan = privacy_actions.add_parser("plan", help="Report redaction and locator changes without page writes.")
    privacy_plan.add_argument("--vault", required=True)
    privacy_apply = privacy_actions.add_parser("apply", help="Apply a privacy plan with CAS and rollback.")
    privacy_apply.add_argument("--vault", required=True)
    privacy_apply.add_argument("--plan-id", required=True)
    privacy_apply.add_argument("--allow-locator-changes", action="store_true", help="Explicitly allow filename/wikilink changes.")

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
                    "`llm-wiki-mcp init --vault <name> --root <vault-path> --default` "
                    "after creating it."
                ),
            }
        )
        _print_json(payload)
        return 2

    _print_json(payload)
    return 0


def _run_config(args: argparse.Namespace) -> int:
    registry = ConfigRegistry.from_file(global_config_path())
    if args.config_action == "validate":
        _print_json({"ok": True, "config_path": str(registry.config_path), "schema_version": registry.config.schema_version})
        return 0
    if args.config_action == "show":
        resolved = registry.resolve_vault(args.vault)
        _print_json({"ok": True, "config_path": str(registry.config_path), "config": registry.public_status(resolved)})
        return 0
    resolved = registry.resolve_vault(args.vault)
    if args.config_action == "set-retrieval":
        profile = {
            "embedding": {
                "enabled": True, "provider": "local_bge_m3", "model_path": args.model_path, "device": args.device,
                "batch_size": args.batch_size, "max_sequence_length": args.max_sequence_length,
                "candidate_limit": args.candidate_limit, "rrf_k": args.rrf_k, "min_vector_score": args.min_vector_score,
            }
        }
        write_global_config(registry.config_path, vault_name=resolved.name, vault_root=resolved.root, make_default=False, retrieval=profile)
    elif args.config_action == "set-privacy":
        write_global_config(registry.config_path, vault_name=resolved.name, vault_root=resolved.root, make_default=False, privacy={"credential_redaction_enabled": not args.disable_credential_redaction, "redaction_rule_version": args.redaction_rule_version, "pii_policy": args.pii_policy})
    elif args.config_action == "set-telemetry":
        write_global_config(registry.config_path, vault_name=resolved.name, vault_root=resolved.root, make_default=False, telemetry={"enabled": not args.disable, "retention_days": args.retention_days, "store_query_body": False})
    elif args.config_action == "set-archive":
        write_global_config(registry.config_path, vault_name=resolved.name, vault_root=resolved.root, make_default=False, archive={"archive_index_enabled": not args.archive_index_disabled, "index_snapshot_ttl_days": args.snapshot_ttl_days, "automatic_purge": args.automatic_purge, "purge_after_days": args.purge_after_days})
    else:
        raise ValueError(f"unknown config action: {args.config_action}")
    updated = ConfigRegistry.from_file(registry.config_path).resolve_vault(resolved.name)
    _print_json({"ok": True, "config_path": str(registry.config_path), "config": ConfigRegistry.from_file(registry.config_path).public_status(updated), "restart_required": True})
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
        entrypoint=args.entrypoint,
        query_version=args.query_version,
        scope=args.scope,
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
    if args.baseline_report:
        try:
            baseline_raw = json.loads(Path(args.baseline_report).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RetrievalEvalError("baseline_invalid", "baseline report could not be read as JSON") from exc
        if not isinstance(baseline_raw, dict):
            raise RetrievalEvalError("baseline_invalid", "baseline report must be a JSON object")
        report["gate"] = evaluate_retrieval_gate(report, baseline_raw)
    reports = write_retrieval_eval_report(report, args.output_dir)
    _print_json(
        {
            "ok": True,
            "dataset_id": report["metadata"]["dataset_id"],
            "dataset_revision": report["metadata"]["dataset_revision"],
            "metrics": report["metrics"],
            "gate": report.get("gate"),
            "reports": reports,
        }
    )
    return 0 if not args.baseline_report or report["gate"]["passed"] else 2


def _run_quality_gate_calibrate(args: argparse.Namespace) -> int:
    dataset_path = Path(args.dataset).expanduser()
    manifest_path = Path(args.manifest).expanduser() if args.manifest else dataset_path.with_suffix(".manifest.json")
    if not manifest_path.is_file():
        raise CalibrationGenerationError("identity_missing", "a frozen dataset manifest is required for calibration")
    identity = identity_from_manifest(read_json_object(manifest_path))
    observations_path = Path(args.observations).expanduser() if args.observations else dataset_path
    observations = load_calibration_observations(observations_path)
    result = generate_calibration_artifact(
        observations,
        identity=identity,
        calibration_revision=args.calibration_revision,
        minimum_sample_count=args.minimum_sample_count,
    )
    reports = write_calibration_outputs(result, args.output_dir)
    _print_json(
        {
            "ok": True,
            "status": result.report["status"],
            "unproven": result.report["unproven"],
            "observation_count": result.report["observation_count"],
            "bucket_count": result.report["bucket_count"],
            "reports": {name: Path(path).name for name, path in reports.items()},
        }
    )
    return 0


def _run_retrieval_gold_sample(args: argparse.Namespace) -> int:
    result = sample_retrieval_gold(
        args.vault,
        args.output_dir,
        count=args.count,
        seed=args.seed,
        dataset_id=args.dataset_id,
    )
    _print_json(_gold_cli_payload(result, artifact_names=("template", "manifest")))
    return 0


def _run_retrieval_gold_finalize(args: argparse.Namespace) -> int:
    result = finalize_retrieval_gold(args.template, args.manifest, args.vault, args.output_dir)
    _print_json(_gold_cli_payload(result, artifact_names=("dataset", "manifest")))
    return 0


def _gold_cli_payload(result: dict[str, Any], *, artifact_names: tuple[str, ...]) -> dict[str, Any]:
    """Return a safe CLI summary without echoing user filesystem paths."""

    payload: dict[str, Any] = {key: value for key, value in result.items() if key not in artifact_names}
    payload["artifacts"] = {
        key: Path(str(result[key])).name
        for key in artifact_names
        if key in result
    }
    return payload


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
    started_at = perf_counter()
    settings = parse_vector_settings(args.vault, _vector_config_from_args(args))
    store = VectorIndexStore(args.vault, settings.index_path)
    stage_started_at = perf_counter()
    records = vector_index_records(args.vault, include_raw_sources=bool(args.include_raw_sources))
    collect_records_ms = _elapsed_ms(stage_started_at)
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
        _print_json(_with_vector_timings(store.build(records, provider, include_raw_sources=bool(args.include_raw_sources)), collect_records_ms, started_at))
        return 0
    if args.vector_action == "update":
        _print_json(_with_vector_timings(store.update(records, provider, include_raw_sources=bool(args.include_raw_sources)), collect_records_ms, started_at))
        return 0
    raise ValueError(f"unknown vector action: {args.vector_action}")


def _with_vector_timings(payload: dict[str, object], collect_records_ms: float, started_at: float) -> dict[str, object]:
    timings = payload.get("timings_ms")
    stage_timings = timings if isinstance(timings, dict) else {}
    return {
        **payload,
        "timings_ms": {
            "collect_records": collect_records_ms,
            **stage_timings,
            "total": _elapsed_ms(started_at),
        },
    }


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 3)


def _run_index(args: argparse.Namespace) -> int:
    store = RetrievalIndexStore(args.vault, scope=args.scope)
    if args.index_action == "status":
        status = store.status()
        _print_json(status)
        return 0 if status.get("ok") else 2
    if args.scope == "active":
        _print_json(sync_retrieval_index(args.vault, full_build=args.index_action == "build"))
        return 0
    _print_json(store.reconcile() if args.index_action == "update" and store.path.exists() else store.build(store.iter_vault_pages()))
    return 0


def _run_archive(args: argparse.Namespace) -> int:
    if args.archive_action == "migrate":
        payload = apply_legacy_migration(args.vault) if args.apply else plan_legacy_migration(args.vault)
    elif args.archive_action == "status":
        payload = ArchiveStatusReader(args.vault).status()
    else:
        service = ArchiveService(args.vault, actor="cli-admin")
        if args.archive_action == "recover": payload = service.recover()
        elif args.archive_action == "rebuild-index": payload = service.rebuild_archive_index()
        elif args.archive_action == "purge": payload = service.purge(args.archive_id, authorized=bool(args.authorize), forget=bool(args.forget))
        else: raise ValueError(f"unknown archive action: {args.archive_action}")
    _print_json(payload)
    return 0 if payload.get("ok") else 2


def _run_raw_replace(args: argparse.Namespace) -> int:
    if args.raw_replace_action == "plan":
        service = RawReplacementService(args.vault, target_path=args.target_path)
        _print_json(service.plan(args.source_root))
        return 0
    if args.raw_replace_action == "apply":
        service = RawReplacementService(args.vault)
        payload = service.apply(args.plan_id)
        _print_json(payload)
        return 0 if payload.get("ok") and payload.get("state") in {"completed", "already_applied"} else 2
    if args.raw_replace_action == "recover":
        service = RawReplacementService(args.vault)
        payload = service.recover(args.plan_id)
        _print_json(payload)
        return 0 if payload.get("ok") and payload.get("state") == "recovered" else 2
    raise ValueError(f"unknown raw-replace action: {args.raw_replace_action}")


def _run_repair(args: argparse.Namespace) -> int:
    if args.repair_action == "page-operation":
        service = PageRepairService(args.vault)
        if args.page_operation_action == "plan":
            payload = service.plan(args.operation_id)
        elif args.page_operation_action == "apply":
            payload = service.apply(args.operation_id)
        else:
            raise ValueError(f"unknown page-operation action: {args.page_operation_action}")
    elif args.repair_action == "provenance":
        service = ProvenanceMigrationService(args.vault)
        if args.provenance_action == "plan":
            payload = service.plan(args.page_path)
        elif args.provenance_action == "apply":
            payload = service.apply(args.plan_id)
        else:
            raise ValueError(f"unknown provenance action: {args.provenance_action}")
    elif args.repair_action == "privacy-audit":
        service = PrivacyAuditService(args.vault)
        if args.privacy_audit_action == "plan":
            payload = service.plan()
        elif args.privacy_audit_action == "apply":
            payload = service.apply(args.plan_id, allow_locator_changes=bool(args.allow_locator_changes))
        else:
            raise ValueError(f"unknown privacy-audit action: {args.privacy_audit_action}")
    else:
        raise ValueError(f"unknown repair action: {args.repair_action}")
    _print_json(payload)
    return 0 if payload.get("ok") else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command in {None, "server"}:
        from app.server import main as server_main

        server_main()
        return 0

    try:
        if args.command == "init":
            return _run_init(args)
        if args.command == "status":
            return _run_status(args)
        if args.command == "config":
            return _run_config(args)
        if args.command == "retrieval-eval":
            return _run_retrieval_eval(args)
        if args.command == "quality-gate" and args.quality_gate_action == "calibrate":
            return _run_quality_gate_calibrate(args)
        if args.command == "retrieval-gold-sample":
            return _run_retrieval_gold_sample(args)
        if args.command == "retrieval-gold-finalize":
            return _run_retrieval_gold_finalize(args)
        if args.command == "vector":
            return _run_vector(args)
        if args.command == "index":
            return _run_index(args)
        if args.command == "archive":
            return _run_archive(args)
        if args.command == "raw-replace":
            return _run_raw_replace(args)
        if args.command == "repair":
            return _run_repair(args)
    except RuntimeConfigError as exc:
        _print_json(_error_payload(exc.code, str(exc), config_path=exc.config_path))
        return 2
    except RetrievalEvalError as exc:
        _print_json(_error_payload(exc.code, str(exc)))
        return 2
    except CalibrationGenerationError as exc:
        _print_json(_error_payload(exc.code, str(exc)))
        return 2
    except RetrievalGoldError as exc:
        _print_json(_error_payload(exc.code, str(exc)))
        return 2
    except (VectorIndexError, VectorProviderError) as exc:
        _print_json(_error_payload(exc.code, str(exc)))
        return 2
    except (ProvenanceMigrationError, PrivacyAuditError) as exc:
        _print_json(_error_payload(exc.code, "administrative plan could not be completed"))
        return 2
    except RawReplacementError as exc:
        _print_json(_error_payload(exc.code, "raw source replacement could not be completed"))
        return 2
    except ValueError as exc:
        _print_json(_error_payload("invalid_config", str(exc)))
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
