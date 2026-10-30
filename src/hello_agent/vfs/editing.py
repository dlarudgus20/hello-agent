"""Exact, non-overlapping edits resolved entirely against the original text."""

from itertools import pairwise

from .core import VFSError


def apply_edits(content: bytes, edits: list[dict]) -> bytes:
    if not isinstance(edits, list) or not edits:
        raise VFSError("invalid_edits", "edits must be a nonempty array")
    try:
        original = content.decode("utf-8")
    except UnicodeError as exc:
        raise VFSError("not_text", "File is not UTF-8 text") from exc
    spans = []
    for index, edit in enumerate(edits):
        if (
            not isinstance(edit, dict)
            or set(edit) != {"old", "new"}
            or not isinstance(edit["old"], str)
            or not edit["old"]
            or not isinstance(edit["new"], str)
        ):
            raise VFSError(
                "invalid_edits",
                f"Edit {index + 1} requires nonempty old and string new",
            )
        old, new = edit["old"], edit["new"]
        start = original.find(old)
        if start < 0:
            raise VFSError(
                "edit_not_found",
                f"Edit {index + 1}: old does not occur in the original file",
            )
        # Advance by one character to count overlapping occurrences too.
        if original.find(old, start + 1) >= 0:
            raise VFSError(
                "edit_ambiguous", f"Edit {index + 1}: old occurs more than once"
            )
        spans.append((start, start + len(old), new))
    spans.sort(key=lambda span: span[0])
    if any(right[0] < left[1] for left, right in pairwise(spans)):
        raise VFSError("edit_overlap", "Edit ranges overlap in the original file")
    result = original
    for start, end, new in reversed(spans):
        result = result[:start] + new + result[end:]
    try:
        return result.encode("utf-8")
    except UnicodeError as exc:
        raise VFSError("invalid_edits", "Replacement text must be valid UTF-8") from exc
