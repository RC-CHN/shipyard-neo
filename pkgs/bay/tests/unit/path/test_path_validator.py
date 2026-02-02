"""Unit tests for path validation with normalization (Option B).

Includes edge cases: double slashes, empty segments, etc.
"""

from __future__ import annotations

import pytest

from app.errors import InvalidPathError
from app.validators.path import validate_optional_relative_path, validate_relative_path


class TestValidateRelativePath:
    """Test path validation with normalization (Option B)."""

    # --- Valid paths ---

    def test_valid_simple_path(self) -> None:
        assert validate_relative_path("file.txt") == "file.txt"

    def test_valid_nested_path(self) -> None:
        assert validate_relative_path("a/b/c.txt") == "a/b/c.txt"

    def test_normalizes_dot_prefix(self) -> None:
        # ./file.txt -> file.txt (removes .)
        assert validate_relative_path("./file.txt") == "file.txt"

    def test_normalizes_internal_traversal(self) -> None:
        # subdir/../file.txt -> file.txt
        assert validate_relative_path("subdir/../file.txt") == "file.txt"

    def test_normalizes_complex_path(self) -> None:
        # a/b/../c/d -> a/c/d
        assert validate_relative_path("a/b/../c/d") == "a/c/d"

    def test_normalizes_multiple_dots(self) -> None:
        # ./a/./b/./c -> a/b/c
        assert validate_relative_path("./a/./b/./c") == "a/b/c"

    def test_normalizes_to_dot_for_empty_result(self) -> None:
        # a/.. -> . (current dir)
        assert validate_relative_path("a/..") == "."

    def test_allows_hidden_files(self) -> None:
        assert validate_relative_path(".hidden") == ".hidden"

    def test_allows_triple_dots(self) -> None:
        # "..." is not "..", so it's allowed
        assert validate_relative_path("...file") == "...file"

    def test_allows_double_dots_in_filename(self) -> None:
        # "file..txt" contains .. but not as a path component
        assert validate_relative_path("file..txt") == "file..txt"

    def test_allows_deep_nested_path(self) -> None:
        assert validate_relative_path("a/b/c/d/e/f/g.txt") == "a/b/c/d/e/f/g.txt"

    def test_current_dir_only(self) -> None:
        assert validate_relative_path(".") == "."

    # --- Normalization edge cases ---

    def test_normalizes_double_slash(self) -> None:
        """Double slash a//b should be normalized to a/b."""
        result = validate_relative_path("a//b")
        assert result == "a/b"

    def test_normalizes_multiple_slashes(self) -> None:
        """Multiple slashes a///b///c should be normalized."""
        result = validate_relative_path("a///b///c")
        assert result == "a/b/c"

    def test_normalizes_trailing_slash(self) -> None:
        """Trailing slash should be handled (directory reference)."""
        # Depending on implementation, trailing slash may be removed or preserved
        result = validate_relative_path("a/b/")
        # Either "a/b" or "a/b/" is acceptable, just shouldn't crash
        assert result in ("a/b", "a/b/")

    def test_normalizes_dot_slash_combinations(self) -> None:
        """Complex dot/slash combinations should normalize correctly."""
        assert validate_relative_path("./a/./") == "a"
        assert validate_relative_path("./a/b/.") == "a/b"

    # --- Invalid paths ---

    def test_rejects_absolute_path(self) -> None:
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("/etc/passwd")
        assert exc.value.code == "invalid_path"
        assert exc.value.details["reason"] == "absolute_path"

    def test_rejects_absolute_path_container_mount(self) -> None:
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("/workspace/file.txt")
        assert exc.value.details["reason"] == "absolute_path"

    def test_rejects_traversal_at_start(self) -> None:
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("../file.txt")
        assert exc.value.details["reason"] == "path_traversal"

    def test_rejects_traversal_escaping_mount(self) -> None:
        # a/../../b.txt escapes: a -> . -> error
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("a/../../b.txt")
        assert exc.value.details["reason"] == "path_traversal"

    def test_rejects_deep_traversal(self) -> None:
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("../../etc/passwd")
        assert exc.value.details["reason"] == "path_traversal"

    def test_rejects_hidden_traversal(self) -> None:
        # Even if starting with dot prefix, traversal should be caught
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("./../file.txt")
        assert exc.value.details["reason"] == "path_traversal"

    def test_rejects_empty_path(self) -> None:
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("")
        assert exc.value.details["reason"] == "empty_path"

    def test_rejects_null_byte(self) -> None:
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("file\x00.txt")
        assert exc.value.details["reason"] == "null_byte"

    def test_rejects_null_byte_in_path(self) -> None:
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("subdir\x00/file.txt")
        assert exc.value.details["reason"] == "null_byte"

    # --- Custom field name ---

    def test_custom_field_name_in_error(self) -> None:
        with pytest.raises(InvalidPathError) as exc:
            validate_relative_path("", field_name="target_path")
        assert exc.value.details["field"] == "target_path"
        assert "target_path" in exc.value.message


class TestValidateOptionalRelativePath:
    """Test optional path validation."""

    def test_none_returns_none(self) -> None:
        assert validate_optional_relative_path(None) is None

    def test_valid_path_returns_normalized(self) -> None:
        assert validate_optional_relative_path("./a/../b.txt") == "b.txt"

    def test_invalid_path_raises(self) -> None:
        with pytest.raises(InvalidPathError) as exc:
            validate_optional_relative_path("../escape.txt")
        assert exc.value.details["reason"] == "path_traversal"

    def test_empty_string_raises(self) -> None:
        # Empty string is not None, so it should be validated
        with pytest.raises(InvalidPathError) as exc:
            validate_optional_relative_path("")
        assert exc.value.details["reason"] == "empty_path"

    def test_custom_field_name(self) -> None:
        with pytest.raises(InvalidPathError) as exc:
            validate_optional_relative_path("/absolute", field_name="cwd")
        assert exc.value.details["field"] == "cwd"
