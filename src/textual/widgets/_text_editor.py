from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable, NamedTuple

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from tree_sitter import Language, Node, Parser, Tree

from textual import events, log
from textual._cells import cell_len
from textual.binding import Binding
from textual.geometry import Region, Size, clamp
from textual.reactive import Reactive, reactive
from textual.scroll_view import ScrollView
from textual.strip import Strip

LANGUAGES_PATH = (
    Path(__file__) / "../../../../tree-sitter-languages/textual-languages.so"
)


class Highlight(NamedTuple):
    """A range to highlight within a single line"""

    start_column: int | None
    end_column: int | None
    node: Node


class TextEditor(ScrollView, can_focus=True):
    BINDINGS = [
        # Cursor movement
        Binding("up", "cursor_up", "cursor up", show=False),
        Binding("down", "cursor_down", "cursor down", show=False),
        Binding("left", "cursor_left", "cursor left", show=False),
        Binding("right", "cursor_right", "cursor right", show=False),
        # Debugging bindings
        Binding("ctrl+s", "print_highlight_cache", "[debug] Print highlight cache"),
        Binding("ctrl+l", "print_line_cache", "[debug] Print line cache"),
    ]

    language: Reactive[str | None] = reactive(None)
    """The language to use for syntax highlighting (via tree-sitter)."""
    cursor_position = reactive((0, 0))
    """The cursor position (zero-based line_index, offset)."""

    def __init__(
        self,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)

        # --- Core editor data
        self.document_lines: list[str] = []
        """Each string in this list represents a line in the document."""

        self._highlights: dict[int, list[Highlight]] = defaultdict(list)
        """Mapping line numbers to the set of cached highlights for that line."""

        # TODO - currently unused
        self._line_cache: dict[int, list[Segment]] = defaultdict(list)
        """Caches segments for lines. Note that a line may span multiple y-offsets
         due to wrapping. These segments do NOT include the cursor highlighting.
         A portion of the line cache will be updated when an edit operation occurs
         or when a file is loaded for the first time.
         Tree sitter will tell us the modified ranges of the AST and we update
         the corresponding line ranges in this cache."""

        # --- Abstract syntax tree and related parsing machinery
        self._parser: Parser | None = None
        """The tree-sitter parser which extracts the syntax tree from the document."""
        self._ast: Tree | None = None
        """The tree-sitter Tree (AST) built from the document."""

    def watch_language(self, new_language: str | None) -> None:
        """Update the language used in AST parsing.

        When the language reactive string is updated, fetch the Language definition
        from our tree-sitter library file. If the language reactive is set to None,
        then the no parser is used."""
        if new_language:
            language = Language(LANGUAGES_PATH.resolve(), new_language)
            parser = Parser()
            self._parser = parser
            self._parser.set_language(language)
            self._ast = self._build_ast(parser, self.document_lines)
        else:
            self._ast = None

        log.debug(f"parser set to {self._parser}")

    def _build_ast(
        self,
        parser: Parser,
        document_lines: list[str],
    ) -> Tree | None:
        """Fully parse the document and build the abstract syntax tree for it.

        Returns None if there's no parser available (e.g. when no language is selected).
        """

        def read_callable(byte_offset, point):
            row, column = point
            row_out_of_bounds = row >= len(document_lines)
            column_out_of_bounds = not row_out_of_bounds and column >= len(
                document_lines[row]
            )
            if row_out_of_bounds or column_out_of_bounds:
                return None
            return document_lines[row][column:].encode("utf8")

        if parser:
            return parser.parse(read_callable)
        else:
            return None

    def load_text(self, text: str) -> None:
        """Load text from a string into the editor."""
        lines = text.splitlines(keepends=True)
        self.load_lines(lines)

    def load_lines(self, lines: list[str]) -> None:
        """Load text from a list of lines into the editor."""
        self.document_lines = lines

        # TODO Offer maximum line width and wrap if needed
        width = max(cell_len(line) for line in lines)
        height = len(lines)
        self.virtual_size = Size(width, height)

        # TODO - clear caches
        if self._parser is not None:
            self._ast = self._build_ast(self._parser, lines)
            self._cache_highlights(self._ast.walk(), lines)

        log.debug(f"loaded text. parser = {self._parser} ast = {self._ast}")

    def render_line(self, widget_y: int) -> Strip:
        document_lines = self.document_lines

        document_y = round(self.scroll_y + widget_y)
        out_of_bounds = document_y >= len(document_lines)
        if out_of_bounds:
            return Strip.blank(self.size.width)

        line_string = document_lines[document_y].replace("\n", "").replace("\r", "")
        line_text = Text(f"{line_string} ", end="")

        # Apply highlighting to the line if necessary.
        if self._highlights:
            highlights = self._highlights[document_y]
            for start, end, node_type in highlights:
                node_style = self._get_node_style(node_type)
                line_text.stylize(node_style, start, end)

        # Show the cursor if necessary
        cursor_row, cursor_column = self.cursor_position
        if cursor_row == document_y:
            line_text.stylize(
                Style(color="black", bgcolor="white"), cursor_column, cursor_column + 1
            )

        # We need to render according to the virtual size otherwise the rendering
        # will wrap the text content incorrectly.
        segments = self.app.console.render(
            line_text, self.app.console.options.update_width(self.virtual_size.width)
        )
        strip = (
            Strip(segments)
            .adjust_cell_length(self.virtual_size.width - 1)
            .crop(int(self.scroll_x), int(self.scroll_x) + self.virtual_size.width - 1)
            .simplify()
        )

        return strip

    def _get_node_style(self, node: Node) -> Style:
        # Apply simple highlighting to the node based on its type.
        if node.type == "identifier":
            style = Style(color="cyan")
        elif node.type == "string":
            style = Style(color="green")
        elif node.type == "import_from_statement":
            style = Style(bgcolor="magenta")
        else:
            style = Style.null()
        return style

    def _cache_highlights(
        self,
        cursor,
        document: list[str],
        line_range: tuple[int, int] | None = None,
    ) -> None:
        """Traverse the AST and highlight the document.

        Args:
            cursor: The tree-sitter Tree cursor.
            document: The document as a list of strings.
            line_range: The start and end line index that is visible. If None, highlight the whole document.
        """

        reached_root = False

        while not reached_root:
            # The range of the document (line indices) that we want to highlight.
            if line_range is not None:
                window_start, window_end = line_range
            else:
                window_start = 0
                window_end = len(document) - 1

            # Get the range of this node
            node_start_row, node_start_column = cursor.node.start_point
            node_end_row, node_end_column = cursor.node.end_point

            node_in_window = line_range is None or (
                window_start <= node_end_row and window_end >= node_start_row
            )

            # Cache the highlight data for this node if it's within the window range
            # At this point we're not actually looking at the document at all, we're
            # just storing data on the locations to highlight within the document.
            # This data will be referenced only when we render.
            if node_in_window:
                highlight_cache = self._highlights
                node = cursor.node
                if node_start_row == node_end_row:
                    highlight = Highlight(node_start_column, node_end_column, node)
                    highlight_cache[node_start_row].append(highlight)
                else:
                    # Add the first line
                    highlight_cache[node_start_row].append(
                        Highlight(node_start_column, None, node)
                    )
                    # Add the middle lines - entire row of this node is highlighted
                    for node_row in range(node_start_row + 1, node_end_row):
                        highlight_cache[node_row].append(Highlight(0, None, node))

                    # Add the last line
                    highlight_cache[node_end_row].append(
                        Highlight(0, node_end_column, node)
                    )

            if cursor.goto_first_child():
                continue

            if cursor.goto_next_sibling():
                continue

            retracing = True
            while retracing:
                if not cursor.goto_parent():
                    retracing = False
                    reached_root = True

                if cursor.goto_next_sibling():
                    retracing = False

    # --- Key handling
    async def _on_key(self, event: events.Key) -> None:
        if event.is_printable:
            event.stop()
            assert event.character is not None

    # --- Reactive watchers and validators
    # def validate_cursor_position(self, new_position: tuple[int, int]) -> tuple[int, int]:
    #     new_row, new_column = new_position
    #     clamped_row = clamp(new_row, 0, len(self.document_lines) - 1)
    #     clamped_column = clamp(new_column, 0, len(self.document_lines[clamped_row]) - 1)
    #     return clamped_row, clamped_column

    def watch_cursor_position(self, new_position: tuple[int, int]) -> None:
        log.debug(f"cursor_position = {new_position!r}")
        self.scroll_cursor_into_view()

    # --- Cursor utilities
    @property
    def cursor_at_first_row(self) -> bool:
        return self.cursor_position[0] == 0

    @property
    def cursor_at_last_row(self) -> bool:
        return self.cursor_position[0] == len(self.document_lines) - 1

    @property
    def cursor_at_start_of_row(self) -> bool:
        return self.cursor_position[1] == 0

    @property
    def cursor_at_end_of_row(self) -> bool:
        cursor_row, cursor_column = self.cursor_position
        row_length = len(self.document_lines[cursor_row])
        cursor_at_end = cursor_column == row_length - 1
        return cursor_at_end

    @property
    def cursor_at_start_of_document(self) -> bool:
        return self.cursor_at_first_row and self.cursor_at_start_of_row

    @property
    def cursor_at_end_of_document(self) -> bool:
        """True if the cursor is at the very end of the document."""
        return self.cursor_at_last_row and self.cursor_at_end_of_row

    def cursor_to_line_end(self) -> None:
        cursor_row, cursor_column = self.cursor_position
        self.cursor_position = (cursor_row, len(self.document_lines[cursor_row]))

    def cursor_to_line_start(self) -> None:
        cursor_row, cursor_column = self.cursor_position
        self.cursor_position = (0, cursor_row)

    def scroll_cursor_into_view(self) -> None:
        """Scroll the cursor into view."""
        cursor_row, cursor_column = self.cursor_position
        self.scroll_to_region(
            Region(x=cursor_column, y=cursor_row, width=1, height=1), animate=False
        )

    # ------ Cursor movement actions
    def action_cursor_left(self) -> None:
        """Move the cursor one position to the left.

        If the cursor is at the left edge of the document, try to move it to
        the end of the previous line.
        """
        if self.cursor_at_start_of_document:
            return

        cursor_row, cursor_column = self.cursor_position
        length_of_row_above = len(self.document_lines[cursor_row - 1])

        target_row = cursor_row if cursor_column != 0 else cursor_row - 1
        target_column = (
            cursor_column - 1 if cursor_column != 0 else length_of_row_above - 1
        )

        self.cursor_position = (target_row, target_column)

    def action_cursor_right(self) -> None:
        """Move the cursor one position to the right.

        If the cursor is at the end of a line, attempt to go to the start of the next line.
        """
        if self.cursor_at_end_of_document:
            return

        cursor_row, cursor_column = self.cursor_position

        target_row = cursor_row + 1 if self.cursor_at_end_of_row else cursor_row
        target_column = 0 if self.cursor_at_end_of_row else cursor_column + 1

        self.cursor_position = (target_row, target_column)

    def action_cursor_down(self) -> None:
        """Move the cursor down one cell."""
        if self.cursor_at_last_row:
            self.cursor_to_line_end()

        cursor_row, cursor_column = self.cursor_position

        target_row = min(len(self.document_lines) - 1, cursor_row + 1)
        # TODO: Fetch last active column on this row
        target_column = clamp(
            cursor_column, 0, len(self.document_lines[target_row]) - 1
        )

        self.cursor_position = (target_row, target_column)

    def action_cursor_up(self) -> None:
        """Move the cursor up one cell."""
        if self.cursor_at_first_row:
            self.cursor_to_line_start()

        cursor_row, cursor_column = self.cursor_position

        target_row = max(0, cursor_row - 1)
        # TODO: Fetch last active column on this row
        target_column = clamp(
            cursor_column, 0, len(self.document_lines[target_row]) - 1
        )

        self.cursor_position = (target_row, target_column)

    # --- Editor operations
    def insert_text_at_cursor(self, text: str) -> None:
        pass

    # --- Debug actions
    def action_print_line_cache(self) -> None:
        log.debug(self._line_cache)

        def traverse(cursor) -> Iterable[Node]:
            yield cursor.node

            if cursor.goto_first_child():
                yield from traverse(cursor)
                while cursor.goto_next_sibling():
                    yield from traverse(cursor)
                cursor.goto_parent()

        log.debug(list(traverse(self._ast.walk())))

    def action_print_highlight_cache(self) -> None:
        log.debug(self._highlights)

    def debug_state(self) -> str:
        return f"""\
cursor {self.cursor_position!r}
language {self.language!r}
document rows {len(self.document_lines)}"""

    def debug_highlights(self) -> str:
        return f"""\
highlight cache keys (rows) {len(self._highlights)}
highlight cache total size {sum(len(highlights) for key, highlights in self._highlights.items())}
current row highlight cache size {len(self._highlights[self.cursor_position[0]])}

[b]current row highlights[/]
{self._highlights[self.cursor_position[0]]}"""


if __name__ == "__main__":

    def traverse_tree(cursor):
        reached_root = False
        while reached_root == False:
            yield cursor.node

            if cursor.goto_first_child():
                continue

            if cursor.goto_next_sibling():
                continue

            retracing = True
            while retracing:
                if not cursor.goto_parent():
                    retracing = False
                    reached_root = True

                if cursor.goto_next_sibling():
                    retracing = False

    language = Language(LANGUAGES_PATH.resolve(), "python")
    parser = Parser()
    parser.set_language(language)

    CODE = """\
    from textual.app import App


    class ScreenApp(App):
        def on_mount(self) -> None:
            self.screen.styles.background = "darkblue"
            self.screen.styles.border = ("heavy", "white")


    if __name__ == "__main__":
        app = ScreenApp()
        app.run()
    """

    document_lines = CODE.splitlines(keepends=False)

    def read_callable(byte_offset, point):
        row, column = point
        if row >= len(document_lines) or column >= len(document_lines[row]):
            return None
        return document_lines[row][column:].encode("utf8")

    tree = parser.parse(bytes(CODE, "utf-8"))

    print(list(traverse_tree(tree.walk())))