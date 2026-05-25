from __future__ import annotations

from netsuite_rag_mcp.models import Chunk, SourceDocument

FALLBACK_CODE_CHUNK_LINES = 50


def chunk_code_document(document: SourceDocument) -> list[Chunk]:
    """Chunk a code SourceDocument into function-level chunks with line ranges.

    Produces:
    1. A file header chunk (annotations, imports, define wrapper)
    2. Entry point function chunks (from frontmatter['functions'])
    3. Helper function chunks (non-entry-point functions)
    4. Falls back to line-based chunking if no functions metadata
    """
    if document.source_kind != "code":
        return []

    frontmatter = dict(document.frontmatter)
    functions = frontmatter.get("functions", [])

    if not functions:
        return _fallback_line_chunks(document, frontmatter)

    return _function_chunks(document, frontmatter, functions)


def _function_chunks(
    document: SourceDocument,
    frontmatter: dict,
    functions: list[dict],
) -> list[Chunk]:
    """Create header + function chunks from a code document with function metadata."""
    lines = document.body.splitlines()
    chunks: list[Chunk] = []
    chunk_index = 0

    # Determine first function start line (1-based)
    first_fn_start = min(fn["start_line"] for fn in functions)

    # ── File header chunk: lines 1 through (first_fn_start - 1) ──
    header_end = first_fn_start - 1
    if header_end >= 1:
        header_text = "\n".join(lines[0:header_end])
        header_metadata = dict(frontmatter)
        header_metadata.update({
            "doc_id": document.doc_id,
            "chunk_index": chunk_index,
            "source_path": document.source_path,
            "heading": frontmatter.get("script_type", document.source_path),
            "entry_point": False,
            "function_name": "(header)",
        })
        chunks.append(Chunk(
            id=f"{document.doc_id}_code_{chunk_index}",
            doc_id=document.doc_id,
            chunk_index=chunk_index,
            source_path=document.source_path,
            heading=frontmatter.get("script_type", document.source_path),
            text=header_text,
            metadata=header_metadata,
            function_name="(header)",
            line_start=1,
            line_end=header_end,
            source_kind=document.source_kind or "code",
            source_name=document.source_name,
            file_hash=document.file_hash,
        ))
        chunk_index += 1

    # ── Function chunks ──
    for fn in functions:
        fn_name = fn["name"]
        start_line = fn["start_line"]
        end_line = fn["end_line"]
        is_entry = fn.get("entry_point", False)

        # Extract function text (1-based to 0-based indexing)
        fn_text = "\n".join(lines[start_line - 1:end_line])

        fn_metadata = dict(frontmatter)
        fn_metadata.update({
            "doc_id": document.doc_id,
            "chunk_index": chunk_index,
            "source_path": document.source_path,
            "heading": fn_name,
            "entry_point": is_entry,
            "function_name": fn_name,
        })

        chunks.append(Chunk(
            id=f"{document.doc_id}_code_{chunk_index}",
            doc_id=document.doc_id,
            chunk_index=chunk_index,
            source_path=document.source_path,
            heading=fn_name,
            text=fn_text,
            metadata=fn_metadata,
            function_name=fn_name,
            line_start=start_line,
            line_end=end_line,
            source_kind="code",
            source_name=document.source_name,
            file_hash=document.file_hash,
        ))
        chunk_index += 1

    return chunks


def _fallback_line_chunks(
    document: SourceDocument,
    frontmatter: dict,
) -> list[Chunk]:
    """Split document into line-based chunks when no function metadata is available."""
    lines = document.body.splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return []

    chunks: list[Chunk] = []
    chunk_index = 0
    start = 0

    while start < total_lines:
        end = min(start + FALLBACK_CODE_CHUNK_LINES, total_lines)
        chunk_text = "\n".join(lines[start:end])
        # 1-based line numbers
        line_start = start + 1
        line_end = end

        fn_name = f"(chunk_{chunk_index})"
        metadata = dict(frontmatter)
        metadata.update({
            "doc_id": document.doc_id,
            "chunk_index": chunk_index,
            "source_path": document.source_path,
            "heading": fn_name,
            "function_name": fn_name,
            "line_start": line_start,
            "line_end": line_end,
        })

        chunks.append(Chunk(
            id=f"{document.doc_id}_code_{chunk_index}",
            doc_id=document.doc_id,
            chunk_index=chunk_index,
            source_path=document.source_path,
            heading=fn_name,
            text=chunk_text,
            metadata=metadata,
            function_name=fn_name,
            line_start=line_start,
            line_end=line_end,
            source_kind="code",
            source_name=document.source_name,
            file_hash=document.file_hash,
        ))
        chunk_index += 1
        start = end

    return chunks


def chunk_document(document: SourceDocument) -> list[Chunk]:
    sections = _split_h2_sections(document.body)
    if not sections:
        sections = [("Document", document.body)]

    chunks: list[Chunk] = []
    for index, (heading, text) in enumerate(sections):
        content = text.strip()
        if not content:
            continue
        metadata = dict(document.frontmatter)
        metadata.update(
            {
                "doc_id": document.doc_id,
                "chunk_index": index,
                "source_path": document.source_path,
                "heading": heading,
                "updated_at": document.updated_at,
            }
        )
        chunks.append(
            Chunk(
                id=f"{document.doc_id}:{index}",
                doc_id=document.doc_id,
                chunk_index=index,
                source_path=document.source_path,
                heading=heading,
                text=content,
                metadata=metadata,
            )
        )
    return chunks


def _split_h2_sections(markdown: str) -> list[tuple[str, str]]:
    lines = markdown.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    in_fence = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence

        if not in_fence and line.startswith("## "):
            if current_heading is not None:
                sections.append((current_heading, current_lines))
            current_heading = line[3:].strip()
            current_lines = [line]
            continue

        if current_heading is not None:
            current_lines.append(line)

    if current_heading is not None:
        sections.append((current_heading, current_lines))

    return [(heading, "\n".join(section_lines)) for heading, section_lines in sections]