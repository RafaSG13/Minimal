"""Modal dialogs: the permission question and the pickers commands open through the frontend."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Label, OptionList, SelectionList
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from minimal.ui.tui import theme


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question picked with the arrow keys; dismisses with the answer."""

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; background: $modal-overlay; }
    ConfirmScreen > Vertical {
        width: 80%; max-width: 100; height: auto;
        padding: 1 2; background: $surface;
    }
    ConfirmScreen .title { width: 100%; text-style: bold; margin-bottom: 1; }
    ConfirmScreen #actions { height: auto; margin-top: 1; }
    ConfirmScreen .choice {
        width: auto; min-width: 12; margin-right: 2; padding: 0 2;
        content-align: center middle; background: $panel; color: $muted;
    }
    ConfirmScreen #yes.-selected { background: $success; color: $background; text-style: bold; }
    ConfirmScreen #no.-selected { background: $error; color: $background; text-style: bold; }
    ConfirmScreen #hint { color: $dim; margin-left: 1; }
    """

    BINDINGS = [
        ("left,up", "pick(True)", "Allow"),
        ("right,down", "pick(False)", "Deny"),
        ("tab", "switch", "Switch"),
        ("enter", "confirm", "Confirm"),
        ("escape", "answer(False)", "Deny"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question
        self.allow = (
            True  # enter on an untouched prompt allows, like most agent harnesses
        )

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                Content.assemble((theme.WARNING, "$warning"), " Permission"),
                classes="title",
            )
            yield Label(self.question, markup=False)
            with Horizontal(id="actions"):
                yield Label("Allow", id="yes", classes="choice -selected")
                yield Label("Deny", id="no", classes="choice")
                yield Label("← → choose · enter confirms · esc denies", id="hint")

    def action_pick(self, allow: bool) -> None:
        self.allow = allow
        self.query_one("#yes").set_class(allow, "-selected")
        self.query_one("#no").set_class(not allow, "-selected")

    def action_switch(self) -> None:
        self.action_pick(not self.allow)

    def action_confirm(self) -> None:
        self.dismiss(self.allow)

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)

    def on_click(self, event: events.Click) -> None:
        if event.widget is not None and event.widget.id in ("yes", "no"):
            self.dismiss(event.widget.id == "yes")


class PermissionScreen(ModalScreen[str]):
    """Whether a tool call may run: allow it once, for this session or always (writing the rule), or deny it.

    Dismisses with ``"once"``, ``"session"``, ``"always"`` or ``"deny"``. The wider answers appear only when there is a
    ``rule`` to add; ``"always"`` only when ``saved_in`` names the file it goes to.
    """

    DEFAULT_CSS = """
    PermissionScreen { align: center middle; background: $modal-overlay; }
    PermissionScreen > Vertical {
        width: 80%; max-width: 100; height: auto;
        padding: 1 2; background: $surface;
    }
    PermissionScreen .title { width: 100%; text-style: bold; margin-bottom: 1; }
    PermissionScreen .rule { color: $muted; margin-top: 1; }
    PermissionScreen #actions { height: auto; margin-top: 1; }
    PermissionScreen .choice {
        width: auto; margin-right: 2; padding: 0 2;
        content-align: center middle; background: $panel; color: $muted;
    }
    PermissionScreen .choice.-selected { background: $success; color: $background; text-style: bold; }
    PermissionScreen .choice.-deny.-selected { background: $error; }
    PermissionScreen #hint { color: $dim; margin-top: 1; }
    """

    BINDINGS = [
        ("left,up", "move(-1)", "Previous"),
        ("right,down,tab", "move(1)", "Next"),
        ("enter", "confirm", "Confirm"),
        ("y", "answer('once')", "Allow once"),
        ("s", "answer('session')", "This session"),
        ("a", "answer('always')", "Always"),
        ("n,d,escape", "answer('deny')", "Deny"),
    ]

    def __init__(self, question: str, rule: str | None = None, saved_in: str | None = None) -> None:
        super().__init__()
        self.question = question
        self.rule = rule
        self.saved_in = saved_in
        self.choices = [("once", "Allow once")]
        if rule is not None:
            self.choices.append(("session", "Allow this session"))
            if saved_in is not None:
                self.choices.append(("always", "Always allow"))
        self.choices.append(("deny", "Deny"))
        self.index = 0  # enter on an untouched prompt allows once, like most agent harnesses

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(Content.assemble((theme.WARNING, "$warning"), " Permission"), classes="title")
            yield Label(self.question, markup=False)
            if self.rule is not None:
                where = f"; always saves it in {self.saved_in}" if self.saved_in is not None else ""
                yield Label(f"Rule for this session or always: {self.rule}{where}", classes="rule", markup=False)
            with Horizontal(id="actions"):
                for i, (value, label) in enumerate(self.choices):
                    classes = f"choice -{value}" + (" -selected" if i == self.index else "")
                    yield Label(label, id=f"choice-{value}", classes=classes)
            yield Label("← → choose · enter confirms · esc denies", id="hint")

    def action_move(self, step: int) -> None:
        self.index = (self.index + step) % len(self.choices)
        for i, (value, _) in enumerate(self.choices):
            self.query_one(f"#choice-{value}").set_class(i == self.index, "-selected")

    def action_confirm(self) -> None:
        self.dismiss(self.choices[self.index][0])

    def action_answer(self, value: str) -> None:
        if any(value == offered for offered, _ in self.choices):
            self.dismiss(value)

    def on_click(self, event: events.Click) -> None:
        widget_id = event.widget.id if event.widget is not None else None
        if widget_id is not None and widget_id.startswith("choice-"):
            self.dismiss(widget_id.removeprefix("choice-"))


class DialogScreen(ModalScreen[Any]):
    """A titled box opened by a command through the frontend; esc cancels (dismisses with ``None``)."""

    DEFAULT_CSS = """
    DialogScreen { align: center middle; background: $modal-overlay; }
    DialogScreen > Vertical {
        width: 80%; max-width: 100; height: auto; max-height: 80%;
        padding: 1 2; background: $surface;
    }
    DialogScreen .title {
        width: 100%; text-style: bold; margin-bottom: 1;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    DialogScreen OptionList, DialogScreen SelectionList { height: auto; max-height: 20; background: $surface; }
    DialogScreen OptionList:focus, DialogScreen SelectionList:focus { background-tint: $foreground 0%; }
    DialogScreen OptionList > .option-list--option-highlighted,
    DialogScreen OptionList:focus > .option-list--option-highlighted,
    DialogScreen SelectionList > .option-list--option-highlighted,
    DialogScreen SelectionList:focus > .option-list--option-highlighted {
        color: $foreground; background: $panel; text-style: bold;
    }
    DialogScreen SelectionList > .selection-list--button { color: $border-muted; background: $surface; }
    DialogScreen SelectionList > .selection-list--button-highlighted { color: $border-muted; background: $panel; }
    DialogScreen SelectionList > .selection-list--button-selected { color: $success; background: $surface; }
    DialogScreen SelectionList > .selection-list--button-selected-highlighted { color: $success; background: $panel; }
    DialogScreen Input.-textual-compact { background: $panel; padding: 0 1; }
    DialogScreen .hint { color: $dim; margin-top: 1; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]
    HINT = ""

    def __init__(self, heading: str) -> None:
        super().__init__()
        self.heading = heading  # not ``title``: Screen.title is a reactive

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.heading, classes="title", markup=False)
            yield self.body()
            yield Label(self.HINT, classes="hint")

    def body(self) -> Widget:
        """The widget that takes the answer."""
        raise NotImplementedError

    def action_cancel(self) -> None:
        self.dismiss(None)



class ChoiceScreen(DialogScreen):
    """Pick one option; dismisses with its value."""

    HINT = "enter selects · esc cancels"

    def __init__(self, heading: str, options: Sequence[tuple[str, str | Content]]) -> None:
        super().__init__(heading)
        self.options = options

    def body(self) -> Widget:
        return OptionList(
            *(
                Option(label if isinstance(label, Content) else Content(label), id=value)
                for value, label in self.options
            ),
            compact=True,
        )

    def on_mount(self) -> None:
        if self.options:
            self.query_one(OptionList).highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)


class ChecklistScreen(DialogScreen):
    """Tick any options; dismisses with the ticked values in option order."""

    HINT = "space toggles · enter applies · esc cancels"
    BINDINGS = [Binding("enter", "apply", "Apply", priority=True)]

    def __init__(self, heading: str, options: list[tuple[str, str, bool]]) -> None:
        super().__init__(heading)
        self.options = options

    def body(self) -> Widget:
        return SelectionList[str](
            *(
                Selection(Content(label), value, checked)
                for value, label, checked in self.options
            ),
            compact=True,
        )

    def on_mount(self) -> None:
        if self.options:
            self.query_one(SelectionList).highlighted = 0

    def action_apply(self) -> None:
        ticked = set(self.query_one(SelectionList).selected)
        self.dismiss([value for value, _, _ in self.options if value in ticked])


class TextScreen(DialogScreen):
    """One line of text; dismisses with it stripped, or ``None`` if empty."""

    HINT = "enter confirms · esc cancels"

    def body(self) -> Widget:
        return Input(compact=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # otherwise MinimalTui.on_input_submitted sends it as a message
        self.dismiss(event.value.strip() or None)
