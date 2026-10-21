"""受限 unified diff 的纯内存解析与应用。"""

from __future__ import annotations

from dataclasses import dataclass
import re

from tricoder.execution_state import PATCH_ERROR_DETAILS


_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?\n?$"
)
_NO_NEWLINE_MARKER = "\\ No newline at end of file"
PATCH_DIAGNOSTIC_KEYS = frozenset({
    "hunk_index", "declared_old", "declared_new", "observed_old", "observed_new",
})


@dataclass(frozen=True, slots=True)
class PatchHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FilePatch:
    path: str
    create: bool
    hunks: tuple[PatchHunk, ...]


class PatchError(ValueError):
    """表示受限补丁不符合语法或无法应用。"""

    def __init__(self, message: str, *, detail_code: str = "patch_invalid_format",
                 diagnostics: tuple[tuple[str, int], ...] = ()) -> None:
        if not isinstance(detail_code, str) or detail_code not in PATCH_ERROR_DETAILS:
            raise ValueError("补丁错误子类型无效")
        if (
            not isinstance(diagnostics, tuple) or len(diagnostics) > 5
            or any(not isinstance(item, tuple) or len(item) != 2
                   or not isinstance(item[0], str) or item[0] not in PATCH_DIAGNOSTIC_KEYS
                   or type(item[1]) is not int or not 0 <= item[1] <= 1_000_000
                   for item in diagnostics)
            or len({item[0] for item in diagnostics}) != len(diagnostics)
        ):
            raise ValueError("补丁数字诊断无效")
        super().__init__(message)
        self.detail_code = detail_code
        self.diagnostics = diagnostics


def parse_unified_diff(source: str) -> tuple[FilePatch, ...]:
    """解析只支持创建或原路径更新的 unified diff 文本。"""

    if not source:
        raise PatchError("补丁输入不能为空")

    lines = source.splitlines(keepends=True)
    patches: list[FilePatch] = []
    seen_paths: set[str] = set()
    index = 0
    hunk_index = 0

    while index < len(lines):
        old_path, new_path, create, index = _parse_file_header(lines, index)
        if new_path in seen_paths:
            raise PatchError("补丁不能重复修改同一路径", detail_code="patch_unsupported_operation")
        seen_paths.add(new_path)

        hunks: list[PatchHunk] = []
        previous_start: int | None = None
        previous_end: int | None = None
        line_delta = 0
        while index < len(lines) and lines[index].startswith("@@"):
            hunk_index += 1
            hunk, index = _parse_hunk(lines, index, hunk_index=hunk_index)
            if create and hunk.old_count != 0:
                raise PatchError("创建文件的补丁不能读取旧内容", detail_code="patch_invalid_hunk")
            if previous_start is not None and (
                hunk.old_start <= previous_start or hunk.old_start < previous_end
            ):
                raise PatchError("补丁 hunk 的旧范围重叠或未递增", detail_code="patch_invalid_hunk")
            if _target_index(hunk) != _source_index(hunk) + line_delta:
                raise PatchError("补丁 hunk 的新起始行与累计行差不一致", detail_code="patch_invalid_hunk")
            previous_start = hunk.old_start
            previous_end = hunk.old_start + hunk.old_count
            line_delta += hunk.new_count - hunk.old_count
            hunks.append(hunk)

        if not hunks:
            raise PatchError("补丁文件缺少 hunk", detail_code="patch_invalid_hunk")
        patches.append(FilePatch(new_path, create, tuple(hunks)))

    return tuple(patches)


def apply_file_patch(original: str, patch: FilePatch) -> str:
    """将已解析的单文件补丁应用到内存中的原始文本。"""

    if not patch.hunks:
        raise PatchError("补丁文件缺少 hunk", detail_code="patch_invalid_hunk")
    if patch.create and original:
        raise PatchError("创建补丁只能应用于空文本", detail_code="patch_context_mismatch")

    original_lines = original.splitlines(keepends=True)
    result: list[str] = []
    source_index = 0
    previous_start: int | None = None
    previous_end: int | None = None
    line_delta = 0

    for hunk in patch.hunks:
        _validate_hunk(hunk)
        if patch.create and hunk.old_count != 0:
            raise PatchError("创建文件的补丁不能读取旧内容", detail_code="patch_invalid_hunk")
        if previous_start is not None and (
            hunk.old_start <= previous_start or hunk.old_start < previous_end
        ):
            raise PatchError("补丁 hunk 的旧范围重叠或未递增", detail_code="patch_invalid_hunk")
        if _target_index(hunk) != _source_index(hunk) + line_delta:
            raise PatchError("补丁 hunk 的新起始行与累计行差不一致", detail_code="patch_invalid_hunk")
        previous_start = hunk.old_start
        previous_end = hunk.old_start + hunk.old_count
        line_delta += hunk.new_count - hunk.old_count

        start_index = _source_index(hunk)
        if start_index < source_index or start_index > len(original_lines):
            raise PatchError("补丁 hunk 的起始位置无效", detail_code="patch_invalid_hunk")
        result.extend(original_lines[source_index:start_index])

        cursor = start_index
        for line in hunk.lines:
            prefix, text = line[0], line[1:]
            if prefix == "+":
                result.append(text)
                continue
            if cursor >= len(original_lines) or original_lines[cursor] != text:
                raise PatchError("补丁上下文与原文不匹配", detail_code="patch_context_mismatch")
            if prefix == " ":
                result.append(text)
            cursor += 1

        if cursor != start_index + hunk.old_count:
            raise PatchError("补丁 hunk 的旧行数与声明不符", detail_code="patch_count_mismatch")
        source_index = cursor

    result.extend(original_lines[source_index:])
    return "".join(result)


def _parse_file_header(
    lines: list[str], index: int
) -> tuple[str, str, bool, int]:
    if not lines[index].startswith("--- "):
        raise PatchError("补丁必须以连续的文件头开始", detail_code="patch_invalid_header")
    if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
        raise PatchError("补丁文件头必须连续", detail_code="patch_invalid_header")

    old_raw = _header_path(lines[index], "--- ")
    new_raw = _header_path(lines[index + 1], "+++ ")
    if new_raw == "/dev/null":
        raise PatchError("补丁不支持删除文件", detail_code="patch_unsupported_operation")

    create = old_raw == "/dev/null"
    old_path = "" if create else _normalize_patch_path(old_raw, "a/")
    new_path = _normalize_patch_path(new_raw, "b/")
    if not create and old_path != new_path:
        raise PatchError("补丁仅支持同路径修改或创建", detail_code="patch_unsupported_operation")
    return old_path, new_path, create, index + 2


def _header_path(line: str, prefix: str) -> str:
    path = line[len(prefix) :]
    if path.endswith("\n"):
        path = path[:-1]
    if not path:
        raise PatchError("补丁文件路径不能为空", detail_code="patch_invalid_header")
    return path


def _normalize_patch_path(raw_path: str, required_prefix: str) -> str:
    """移除一个标准前缀并验证得到的是规范的相对路径。"""

    if not raw_path.startswith(required_prefix):
        raise PatchError("补丁文件路径前缀无效", detail_code="patch_invalid_header")
    path = raw_path[len(required_prefix) :]
    segments = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or ":" in path
        or "\x00" in path
        or any(segment in ("", ".", "..") for segment in segments)
    ):
        raise PatchError("补丁文件路径不是规范相对路径", detail_code="patch_invalid_header")
    return path


def _parse_hunk(lines: list[str], index: int, *, hunk_index: int = 1) -> tuple[PatchHunk, int]:
    match = _HUNK_HEADER.match(lines[index])
    if match is None:
        raise PatchError("补丁 hunk 头无效", detail_code="patch_invalid_hunk")

    old_start = int(match.group(1))
    old_count = int(match.group(2) or 1)
    new_start = int(match.group(3))
    new_count = int(match.group(4) or 1)
    _validate_positions(old_start, old_count, new_start, new_count)
    index += 1
    body: list[str] = []
    old_seen = 0
    new_seen = 0

    while old_seen < old_count or new_seen < new_count:
        if index >= len(lines):
            # EOF fixes the observed totals. Other structural failures do not
            # establish a complete hunk, so they keep diagnostics unspecified.
            counts = (("hunk_index", hunk_index), ("declared_old", old_count),
                      ("declared_new", new_count), ("observed_old", old_seen),
                      ("observed_new", new_seen))
            raise PatchError("补丁 hunk 的行数与声明不符", detail_code="patch_count_mismatch",
                             diagnostics=tuple((key, value) for key, value in counts
                                               if 0 <= value <= 1_000_000))
        line = lines[index]
        if _is_no_newline_marker(line):
            _remove_preceding_newline(body)
            index += 1
            continue
        if not line or line[0] not in " +-":
            raise PatchError("补丁 hunk 内容无效", detail_code="patch_invalid_hunk")
        body.append(line)
        if line[0] in " -":
            old_seen += 1
        if line[0] in " +":
            new_seen += 1
        if old_seen > old_count or new_seen > new_count:
            raise PatchError("补丁 hunk 的行数与声明不符", detail_code="patch_count_mismatch")
        index += 1

    if index < len(lines) and _is_no_newline_marker(lines[index]):
        _remove_preceding_newline(body)
        index += 1
    if index < len(lines) and lines[index] and lines[index][0] in " +-":
        if not lines[index].startswith("--- "):
            raise PatchError("补丁 hunk 的行数与声明不符", detail_code="patch_count_mismatch")

    return PatchHunk(old_start, old_count, new_start, new_count, tuple(body)), index


def _validate_hunk(hunk: PatchHunk) -> None:
    _validate_positions(hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count)
    old_seen = 0
    new_seen = 0
    for line in hunk.lines:
        if not line or line[0] not in " +-":
            raise PatchError("补丁 hunk 内容无效", detail_code="patch_invalid_hunk")
        if line[0] in " -":
            old_seen += 1
        if line[0] in " +":
            new_seen += 1
    if old_seen != hunk.old_count or new_seen != hunk.new_count:
        raise PatchError("补丁 hunk 的行数与声明不符", detail_code="patch_count_mismatch")


def _validate_positions(
    old_start: int, old_count: int, new_start: int, new_count: int
) -> None:
    if old_start < 0 or new_start < 0 or old_count < 0 or new_count < 0:
        raise PatchError("补丁 hunk 的范围无效", detail_code="patch_invalid_hunk")
    if old_count and old_start == 0:
        raise PatchError("补丁 hunk 的旧范围无效", detail_code="patch_invalid_hunk")
    if new_count and new_start == 0:
        raise PatchError("补丁 hunk 的新范围无效", detail_code="patch_invalid_hunk")


def _source_index(hunk: PatchHunk) -> int:
    return hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1


def _target_index(hunk: PatchHunk) -> int:
    return hunk.new_start if hunk.new_count == 0 else hunk.new_start - 1


def _is_no_newline_marker(line: str) -> bool:
    return line.rstrip("\n") == _NO_NEWLINE_MARKER


def _remove_preceding_newline(body: list[str]) -> None:
    if not body or not body[-1].endswith("\n"):
        raise PatchError("补丁换行标记位置无效", detail_code="patch_invalid_hunk")
    body[-1] = body[-1][:-1]
