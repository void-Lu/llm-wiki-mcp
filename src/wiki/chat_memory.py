from __future__ import annotations

"""Explicit, redacted and immutable chat source persistence."""

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import yaml

from common.redaction import (
    REDACTION_POLICY_VERSION,
    count_redaction_categories,
    count_redactions,
    redact_sensitive_text,
)
from common.privacy_policy import LocatorError, PrivacyPolicy, normalize_vault_relative
from wiki.atomic_file import FaultBarrier, atomic_write_text, sha256_file
from wiki.page_mutation import PageMutationCoordinator
from wiki.page_operation_store import PageOperation, PageOperationError
from wiki.wiki_io import read_markdown_page

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ROLE_HEADING = re.compile(r"^#{1,6}\s*(user|assistant)\s*:?[ \t]*$", re.IGNORECASE | re.MULTILINE)
_FORBIDDEN_ROLE = re.compile(r"^#{1,6}\s*(system|tool|developer|internal)\s*:?[ \t]*$", re.IGNORECASE | re.MULTILINE)


class ChatMemoryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _redact_value(value: Any) -> Any:
    return PrivacyPolicy().redact_metadata(value)


def _redaction_summary(values: list[str]) -> tuple[int, dict[str, int]]:
    original = "\n".join(values)
    redacted = redact_sensitive_text(original)
    return count_redactions(original, redacted), count_redaction_categories(original, redacted)


def decode_chat_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChatMemoryError("invalid_chat_metadata", "chat_metadata must be an object")
    allowed = {"session_id", "summary", "decisions", "open_questions", "tags", "project"}
    unknown = set(value) - allowed
    if unknown:
        raise ChatMemoryError("invalid_chat_metadata", "chat_metadata contains unsupported fields")
    session_id = value.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ChatMemoryError("invalid_chat_session_id", "chat_metadata.session_id must be a safe non-empty identifier")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ChatMemoryError("invalid_chat_metadata", "chat_metadata.summary must be a non-empty string")
    decoded: dict[str, Any] = {"session_id": session_id, "summary": summary.strip()}
    for key in ("decisions", "open_questions", "tags"):
        items = value.get(key)
        if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
            raise ChatMemoryError("invalid_chat_metadata", f"chat_metadata.{key} must be a list of non-empty strings")
        decoded[key] = [item.strip() for item in items]
    project = value.get("project")
    if project is not None:
        if not isinstance(project, str) or not project.strip():
            raise ChatMemoryError("invalid_chat_metadata", "chat_metadata.project must be a non-empty string when supplied")
        try:
            decoded["project"] = normalize_vault_relative(project.strip())
        except LocatorError as exc:
            raise ChatMemoryError(exc.code, "chat_metadata.project violates the privacy policy") from exc
    return decoded


def validate_visible_chat_markdown(content: object) -> str:
    if not isinstance(content, str) or not content.strip():
        raise ChatMemoryError("invalid_chat_content", "chat content must be a non-empty Markdown transcript")
    text = content.strip()
    # A transcript may contain arbitrary Markdown inside a message, but it
    # must begin with an explicit visible-role delimiter.  Otherwise text
    # before the first delimiter would be persisted without a user/assistant
    # provenance boundary.
    if _FORBIDDEN_ROLE.search(text) or not _ROLE_HEADING.match(text):
        raise ChatMemoryError("invalid_chat_content", "chat content must contain only visible user/assistant role-marked Markdown")
    headings = list(re.finditer(r"^#{1,6}\s*([^\n:]+)\s*:?[ \t]*$", text, re.MULTILINE))
    if any(match.group(1).strip().lower() not in {"user", "assistant"} for match in headings):
        raise ChatMemoryError("invalid_chat_content", "chat content contains a non-visible role heading")
    return text


def message_count(content: str) -> int:
    return len(_ROLE_HEADING.findall(content))


class ChatMemoryService:
    def __init__(
        self,
        vault_root: str | Path,
        *,
        coordinator: PageMutationCoordinator | None = None,
        fault: FaultBarrier | None = None,
    ):
        self.root = Path(vault_root).expanduser().resolve()
        self.fault = fault
        self.coordinator = coordinator or PageMutationCoordinator(self.root, fault=fault)

    def save(self, content: object, chat_metadata: object) -> dict[str, Any]:
        transcript = validate_visible_chat_markdown(content)
        metadata = decode_chat_metadata(chat_metadata)
        redacted_transcript = redact_sensitive_text(transcript)
        redacted_metadata = _redact_value(metadata)
        assert isinstance(redacted_metadata, dict)
        redacted_count, categories = _redaction_summary([transcript, _canonical(metadata)])
        payload = {"body": redacted_transcript, "metadata": redacted_metadata}
        redacted_hash = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        session_dir = self._session_dir(str(redacted_metadata["session_id"]))
        existing = self._revisions(session_dir)
        if existing:
            latest_path, latest = existing[-1]
            if latest.frontmatter.get("redacted_hash") == redacted_hash:
                return self._project_existing_revision(
                    latest_path,
                    latest.frontmatter,
                    redacted_metadata,
                    redacted_hash,
                )
            parent_body = latest.body
            append_only = redacted_transcript.startswith(parent_body) and len(redacted_transcript) > len(parent_body)
            parent_revision = int(latest.frontmatter["revision"])
        else:
            append_only = False
            parent_revision = None
            parent_body = ""
        revision = (parent_revision or 0) + 1
        processing_mode = "incremental" if append_only else "full"
        relative = session_dir.relative_to(self.root) / f"revision-{revision:06d}.md"
        delta_start_line = parent_body.count("\n") + 2 if append_only else 1
        frontmatter: dict[str, Any] = {
            "type": "chat_source",
            "session_id": redacted_metadata["session_id"],
            "revision": revision,
            "redacted_hash": redacted_hash,
            "parent_revision": parent_revision,
            "append_only": append_only,
            "processing_mode": processing_mode,
            "delta_start_line": delta_start_line,
            "redaction_policy_version": REDACTION_POLICY_VERSION,
            "redacted_count": redacted_count,
            "redacted_categories": categories,
            "summary": redacted_metadata["summary"],
            "decisions": redacted_metadata["decisions"],
            "open_questions": redacted_metadata["open_questions"],
            "tags": redacted_metadata["tags"],
        }
        if "project" in redacted_metadata:
            frontmatter["project"] = redacted_metadata["project"]
        serialized = self._serialize(frontmatter, redacted_transcript)
        intended_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        request_key = self._request_key(str(redacted_metadata["session_id"]), redacted_hash)
        existing_operation = self.coordinator.store.get_operation_by_request_key(request_key)
        try:
            operation = self.coordinator.prepare(
                request_key=request_key,
                operation_kind="chat_source",
                page_path=relative.as_posix(),
                base_hash=None,
                intended_hash=intended_hash,
            )
        except PageOperationError as exc:
            raise ChatMemoryError(exc.code, "chat source operation could not be prepared") from exc

        if existing_operation is not None:
            result = self.coordinator.project_existing(operation.operation_id, fault=self.fault)
        else:
            result = self.coordinator.commit_with_projections(operation.operation_id, serialized, fault=self.fault)
        if not result.get("ok"):
            raise ChatMemoryError(str(result.get("code") or "chat_source_commit_failed"), "chat source operation could not be completed")
        current = self.coordinator.store.get_operation(operation.operation_id) or operation
        response = self._response(self.root / relative, frontmatter, idempotent=existing_operation is not None)
        return self._with_operation_result(response, current, result)

    def _project_existing_revision(
        self,
        path: Path,
        frontmatter: Mapping[str, Any],
        metadata: Mapping[str, Any],
        redacted_hash: str,
    ) -> dict[str, Any]:
        """Recover/project a durable revision without creating another file."""

        current_hash = sha256_file(path)
        request_key = self._request_key(str(metadata["session_id"]), redacted_hash)
        existing_operation = self.coordinator.store.get_operation_by_request_key(request_key)
        try:
            operation = existing_operation or self.coordinator.prepare(
                request_key=request_key,
                operation_kind="chat_source",
                page_path=path.relative_to(self.root).as_posix(),
                base_hash=current_hash,
                intended_hash=current_hash,
            )
        except PageOperationError as exc:
            raise ChatMemoryError(exc.code, "chat source operation could not be prepared") from exc
        result = self.coordinator.project_existing(operation.operation_id, fault=self.fault)
        if not result.get("ok"):
            raise ChatMemoryError(str(result.get("code") or "chat_source_recovery_failed"), "chat source recovery could not be completed")
        current = self.coordinator.store.get_operation(operation.operation_id) or operation
        response = self._response(path, frontmatter, idempotent=True)
        return self._with_operation_result(response, current, result)

    def _with_operation_result(
        self,
        response: dict[str, Any],
        operation: PageOperation,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        response["operation_id"] = operation.operation_id
        response["state"] = result.get("state") or operation.state
        if result.get("repair_action"):
            response["repair_action"] = result["repair_action"]
        if result.get("failed_stage"):
            response["failed_stage"] = result["failed_stage"]
        response["index"] = self._index_response(operation)
        return response

    @staticmethod
    def _request_key(session_id: str, redacted_hash: str) -> str:
        return f"chat:{session_id}:{redacted_hash}"

    @staticmethod
    def _index_response(operation: PageOperation) -> dict[str, Any]:
        stage = operation.stages.get("retrieval", {})
        stage_result = stage.get("result", {})
        if not isinstance(stage_result, Mapping):
            stage_result = {}
        if stage.get("state") == "succeeded":
            retrieval_index = dict(stage_result.get("retrieval_index") or stage_result)
            indexed = retrieval_index.get("state") not in {"rebuild_required", "not_indexed"} and retrieval_index.get("ok") is True
            return {
                "ok": indexed,
                "generation": {"enabled": False, "reason": "raw_only"},
                "retrieval_index": retrieval_index,
            }
        return {
            "ok": False,
            "generation": {"enabled": False, "reason": "raw_only"},
            "retrieval_index": {
                "ok": False,
                "state": stage.get("state", "pending"),
                "code": stage.get("code") or "retrieval_pending",
            },
        }

    def provenance(self, sources: object) -> dict[str, Any]:
        if not isinstance(sources, list) or not sources:
            return {"ok": False, "code": "chat_sources_required"}
        normalized: list[dict[str, Any]] = []
        for source in sources:
            if not isinstance(source, Mapping) or set(source) != {"source_id", "revision", "redacted_hash"}:
                return {"ok": False, "code": "invalid_chat_source"}
            source_id, revision, redacted_hash = source.get("source_id"), source.get("revision"), source.get("redacted_hash")
            if not isinstance(source_id, str) or not _SESSION_ID.fullmatch(source_id) or not isinstance(redacted_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", redacted_hash):
                return {"ok": False, "code": "invalid_chat_source"}
            try:
                revision_number = int(str(revision).replace("revision-", ""))
            except ValueError:
                return {"ok": False, "code": "invalid_chat_source"}
            matches = list((self.root / "raw" / "sources" / "chat").glob(f"*/*/*/{source_id}/revision-{revision_number:06d}.md"))
            if len(matches) != 1:
                return {"ok": False, "code": "chat_source_not_found"}
            page = read_markdown_page(matches[0], self.root)
            if page.frontmatter.get("redacted_hash") != redacted_hash:
                return {"ok": False, "code": "chat_source_hash_mismatch"}
            normalized.append({"source_id": source_id, "revision": revision_number, "redacted_hash": redacted_hash, "path": matches[0].relative_to(self.root).as_posix()})
        return {"ok": True, "sources": normalized}

    def _session_dir(self, session_id: str) -> Path:
        root = self.root / "raw" / "sources" / "chat"
        existing = list(root.glob(f"*/*/*/{session_id}")) if root.exists() else []
        if len(existing) > 1:
            raise ChatMemoryError("ambiguous_chat_session", "session_id has multiple source directories")
        if existing:
            return existing[0]
        today = date.today()
        return root / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}" / session_id

    @staticmethod
    def _serialize(frontmatter: Mapping[str, Any], body: str) -> str:
        return f"---\n{yaml.safe_dump(dict(frontmatter), allow_unicode=True, sort_keys=False).strip()}\n---\n\n{body.strip()}\n"

    @staticmethod
    def _atomic_write(
        target: Path,
        text: str,
        *,
        fault: FaultBarrier | None = None,
    ) -> None:
        atomic_write_text(target, text, fault=fault)

    @staticmethod
    def _revisions(session_dir: Path) -> list[tuple[Path, Any]]:
        if not session_dir.is_dir():
            return []
        return [(path, read_markdown_page(path, session_dir.parents[6])) for path in sorted(session_dir.glob("revision-*.md"))]

    def _response(self, path: Path, frontmatter: Mapping[str, Any], *, idempotent: bool) -> dict[str, Any]:
        return {
            "ok": True,
            "path": path.relative_to(self.root).as_posix(),
            "source_id": frontmatter["session_id"],
            "revision": frontmatter["revision"],
            "redacted_hash": frontmatter["redacted_hash"],
            "redaction_policy_version": frontmatter["redaction_policy_version"],
            "redacted_count": frontmatter["redacted_count"],
            "redacted_categories": dict(frontmatter["redacted_categories"]),
            "processing_mode": frontmatter["processing_mode"],
            "idempotent": idempotent,
        }
