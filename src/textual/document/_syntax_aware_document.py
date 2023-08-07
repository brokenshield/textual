from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from rich.style import Style
from rich.text import Text
from typing_extensions import TYPE_CHECKING

try:
    from tree_sitter_languages import get_language, get_parser

    if TYPE_CHECKING:
        from tree_sitter import Language, Parser, Tree
        from tree_sitter.binding import Query

    TREE_SITTER = True
except ImportError:
    TREE_SITTER = False

from textual._fix_direction import _fix_direction
from textual._languages import VALID_LANGUAGES
from textual.document._document import Document, Highlight

TREE_SITTER_PATH = Path(__file__) / "../../../../tree-sitter/"
HIGHLIGHTS_PATH = TREE_SITTER_PATH / "highlights/"

HIGHLIGHT_STYLES = {
    "string": Style(color="#E6DB74"),
    "string.documentation": Style(color="yellow"),
    "comment": Style(color="#75715E"),
    "keyword": Style(color="#F92672"),
    "include": Style(color="#F92672"),
    "keyword.function": Style(color="#F92672"),
    "keyword.return": Style(color="#F92672"),
    "conditional": Style(color="#F92672"),
    "number": Style(color="#AE81FF"),
    "class": Style(color="#A6E22E"),
    "function": Style(color="#A6E22E"),
    "function.call": Style(color="#A6E22E"),
    "method": Style(color="#A6E22E"),
    "method.call": Style(color="#A6E22E"),
    # "constant": Style(color="#AE81FF"),
    "variable": Style(color="white"),
    "parameter": Style(color="cyan"),
    "type": Style(color="cyan"),
    "escape": Style(bgcolor="magenta"),
    "heading": Style(color="#F92672", bold=True),
}


class SyntaxAwareDocument(Document):
    """A wrapper around a Document which also maintains a tree-sitter syntax
    tree when the document is edited."""

    def __init__(self, text: str, language: str):
        super().__init__(text)
        self._language: Language | None = None
        """The tree-sitter Language or None if tree-sitter unavailable."""

        self._parser: Parser | None = None
        """The tree-sitter Parser or None if tree-sitter unavailable"""

        self._syntax_tree: Tree | None = None
        """The tree-sitter Tree (syntax tree) built from the document."""

        self._highlights_query: str | None = None
        """The tree-sitter query string for used to fetch highlighted ranges"""

        self._highlights: dict[int, list[Highlight]] = defaultdict(list)
        """Mapping line numbers to the set of cached highlights for that line."""

        if TREE_SITTER:
            # TODO validate language string
            self._language = get_language(language)
            self._parser = get_parser(language)
            if language in VALID_LANGUAGES:
                highlight_query_path = (
                    Path(HIGHLIGHTS_PATH.resolve()) / f"{language}.scm"
                )
                self._highlights_query = highlight_query_path.read_text()
            else:
                raise RuntimeError(f"Invalid language {language!r}")

            self._syntax_tree = self._build_ast(self._parser)
            self._prepare_highlights()

    def insert_range(
        self, start: tuple[int, int], end: tuple[int, int], text: str
    ) -> tuple[int, int]:
        """Insert text at the given range.

        Args:
            start: A tuple (row, column) where the edit starts.
            end: A tuple (row, column) where the edit ends.
            text: The text to insert between start and end.

        Returns:
            The new end location after the edit is complete.
        """
        top, bottom = _fix_direction(start, end)

        # An optimisation would be finding the byte offsets as a single operation rather
        # than doing two passes over the document content.
        start_byte = self._tree_sitter_byte_offset(top)
        old_end_byte = self._tree_sitter_byte_offset(bottom)

        end_location = super().insert_range(start, end, text)

        if TREE_SITTER:
            text_byte_length = len(text.encode("utf-8"))
            self._syntax_tree.edit(
                start_byte=start_byte,
                old_end_byte=old_end_byte,
                new_end_byte=start_byte + text_byte_length,
                start_point=top,
                old_end_point=bottom,
                new_end_point=end_location,
            )
            self._syntax_tree = self._parser.parse(
                self._read_callable, self._syntax_tree
            )
            self._prepare_highlights()

        return end_location

    def delete_range(self, start: tuple[int, int], end: tuple[int, int]) -> str:
        """Delete text between `start` and `end`.

        This will update the internal syntax tree of the document, refreshing
        the syntax highlighting data. Calling `get_line` will now return a Text
        object with new highlights corresponding to this change.

        Args:
            start: The start of the range.
            end: The end of the range.

        Returns:
            A string containing the deleted text.
        """

        top, bottom = _fix_direction(start, end)
        start_byte = self._tree_sitter_byte_offset(top)
        old_end_byte = self._tree_sitter_byte_offset(bottom)

        deleted_text = super().delete_range(start, end)

        if TREE_SITTER:
            deleted_text_byte_length = len(deleted_text.encode("utf-8"))
            self._syntax_tree.edit(
                start_byte=start_byte,
                old_end_byte=old_end_byte,
                new_end_byte=old_end_byte - deleted_text_byte_length,
                start_point=top,
                old_end_point=bottom,
                new_end_point=top,
            )
            self._syntax_tree = self._parser.parse(
                self._read_callable, self._syntax_tree
            )
            self._prepare_highlights()

        return deleted_text

    # TODO - this should return a string and the highlights to apply, the actual highlighting should
    #  be done inside the TextArea by consulting the Theme object.
    def get_line_text(self, line_index: int) -> Text:
        """Apply syntax highlights and return the Text of the line.

        Args:
            line_index: The index of the line.

        Returns:
            The syntax highlighted Text of the line.
        """
        null_style = Style.null()
        line = Text(self[line_index], end="")

        if self._highlights:
            highlights = self._highlights[line_index]
            for start, end, highlight_name in highlights:
                node_style = HIGHLIGHT_STYLES.get(highlight_name, null_style)
                line.stylize(node_style, start, end)

        return line

    def _tree_sitter_byte_offset(self, location: tuple[int, int]) -> int:
        """Given a document coordinate, return the byte offset of that coordinate.
        This method only does work if tree-sitter was imported, otherwise it returns 0.
        """
        if not TREE_SITTER:
            return 0

        lines = self._lines
        row, column = location
        lines_above = lines[:row]
        end_of_line_width = len(self.newline)
        bytes_lines_above = sum(
            len(line.encode("utf-8")) + end_of_line_width for line in lines_above
        )
        if row < len(lines):
            bytes_on_left = len(lines[row][:column].encode("utf-8"))
        else:
            bytes_on_left = 0
        return bytes_lines_above + bytes_on_left

    def _prepare_highlights(
        self,
        start_point: tuple[int, int] | None = None,
        end_point: tuple[int, int] = None,
    ) -> None:
        if not TREE_SITTER:
            return None

        highlights = self._highlights
        query: Query = self._language.query(self._highlights_query)

        captures_kwargs = {}
        if start_point is not None:
            captures_kwargs["start_point"] = start_point
        if end_point is not None:
            captures_kwargs["end_point"] = end_point

        captures = query.captures(self._syntax_tree.root_node, **captures_kwargs)

        highlight_updates: dict[int, list[Highlight]] = defaultdict(list)
        for capture in captures:
            node, highlight_name = capture
            node_start_row, node_start_column = node.start_point
            node_end_row, node_end_column = node.end_point

            if node_start_row == node_end_row:
                highlight = Highlight(
                    node_start_column, node_end_column, highlight_name
                )
                highlight_updates[node_start_row].append(highlight)
            else:
                # Add the first line
                highlight_updates[node_start_row].append(
                    Highlight(node_start_column, None, highlight_name)
                )
                # Add the middle lines - entire row of this node is highlighted
                for node_row in range(node_start_row + 1, node_end_row):
                    highlight_updates[node_row].append(
                        Highlight(0, None, highlight_name)
                    )

                # Add the last line
                highlight_updates[node_end_row].append(
                    Highlight(0, node_end_column, highlight_name)
                )

        for line_index, updated_highlights in highlight_updates.items():
            highlights[line_index] = updated_highlights

    def _build_ast(
        self,
        parser: Parser,
    ) -> Tree | None:
        """Fully parse the document and build the abstract syntax tree for it.

        Returns None if there's no parser available (e.g. when no language is selected).
        """
        if parser:
            return parser.parse(self._read_callable)
        else:
            return None

    def _read_callable(self, byte_offset: int, point: tuple[int, int]) -> bytes | None:
        row, column = point
        lines = self._lines

        row_out_of_bounds = row >= len(lines)
        column_out_of_bounds = not row_out_of_bounds and column > len(lines[row])

        if row_out_of_bounds or column_out_of_bounds:
            return_value = None
        elif column == len(lines[row]) and row < len(lines):
            # TODO: Need to handle \r\n case here.
            return_value = "\n".encode("utf8")
        else:
            return_value = lines[row][column].encode("utf8")

        return return_value
