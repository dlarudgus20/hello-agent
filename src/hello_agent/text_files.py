"""UTF-8 line reading and literal search over the virtual filesystem."""

from hashlib import sha256

from .vfs import VFSError, VirtualFileSystem
from .vfs.core import normalize_path


def integer(value, name: str, minimum: int = 1) -> None:
    if type(value) is not int or value < minimum:
        raise VFSError("invalid_arguments", f"{name} must be an integer >= {minimum}")


def text_lines(data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VFSError("not_text", "File is not UTF-8 text") from exc
    if not text:
        return []
    parts = text.split("\n")
    # Only CR immediately before LF belongs to the line separator.
    lines = [part.removesuffix("\r") for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def read_file(
    vfs: VirtualFileSystem, path: str, start_line: int = 1, max_lines: int = 200
) -> dict:
    integer(start_line, "start_line")
    integer(max_lines, "max_lines")
    path = normalize_path(path)
    data = vfs.read_file(path)
    lines = text_lines(data)
    selected = lines[start_line - 1 : start_line - 1 + max_lines]
    end_line = start_line + len(selected) - 1 if selected else None
    has_more = end_line is not None and end_line < len(lines)
    return {
        "status": "success",
        "path": path,
        "file_size_bytes": len(data),
        "sha256": sha256(data).hexdigest(),
        "total_lines": len(lines),
        "start_line": start_line,
        "end_line": end_line,
        "returned_lines": len(selected),
        "has_more": has_more,
        "next_start_line": end_line + 1 if has_more else None,
        "lines": [
            {"line_number": number, "text": text}
            for number, text in enumerate(selected, start_line)
        ],
    }


def search_files(
    vfs: VirtualFileSystem,
    paths: list[str],
    queries: list[str],
    max_results: int = 100,
    offset: int = 0,
) -> dict:
    integer(max_results, "max_results")
    integer(offset, "offset", 0)
    if not isinstance(paths, list) or not paths:
        raise VFSError("invalid_arguments", "paths must be a nonempty array of paths")
    if (
        not isinstance(queries, list)
        or not queries
        or any(
            not isinstance(query, str) or not query or "\r" in query or "\n" in query
            for query in queries
        )
    ):
        raise VFSError(
            "invalid_arguments",
            "queries must be a nonempty array of nonempty single-line strings",
        )
    queries = list(dict.fromkeys(queries))
    pending = {}
    # Validate every explicit scope before traversal; missing scopes are errors.
    for raw_path in paths:
        path = normalize_path(raw_path)
        entry = vfs.stat(path)
        if raw_path.endswith("/") and entry.type != "directory":
            raise VFSError("not_directory", f"Not a directory: {raw_path!r}")
        pending[path] = entry.type

    skipped = {}

    def skip(path, exc):
        if exc.code not in (
            "not_found",
            "not_directory",
            "is_directory",
            "not_text",
            "not_readable",
        ):
            raise exc
        skipped[path] = {"path": path, "reason": exc.code}

    files = set()
    visited = set()
    while pending:
        path, kind = pending.popitem()
        if path in visited:
            continue
        visited.add(path)
        if kind == "file":
            files.add(path)
            continue
        try:
            entries = vfs.listdir(path)
        except VFSError as exc:
            skip(path, exc)
            continue
        for entry in entries:
            child = path.rstrip("/") + "/" + entry.name
            if child not in visited:
                pending[child] = entry.type

    matches = []
    total_matches = files_scanned = files_matched = 0
    for path in sorted(files):
        try:
            lines = text_lines(vfs.read_file(path))
        except VFSError as exc:
            skip(path, exc)
            continue
        files_scanned += 1
        matched_file = False
        for number, text in enumerate(lines, 1):
            matched_strings = [query for query in queries if query in text]
            if not matched_strings:
                continue
            matched_file = True
            if offset <= total_matches < offset + max_results:
                matches.append(
                    {
                        "path": path,
                        "line_number": number,
                        "text": text,
                        "matched_strings": matched_strings,
                    }
                )
            total_matches += 1
        files_matched += matched_file
    has_more = offset + len(matches) < total_matches
    return {
        "status": "success",
        "paths": sorted({normalize_path(path) for path in paths}),
        "queries": queries,
        "offset": offset,
        "max_results": max_results,
        "total_matches": total_matches,
        "returned_matches": len(matches),
        "files_scanned": files_scanned,
        "files_matched": files_matched,
        "has_more": has_more,
        "next_offset": offset + len(matches) if has_more else None,
        "skipped_files": [skipped[path] for path in sorted(skipped)],
        "matches": matches,
    }
