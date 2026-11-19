import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from textual.widgets import OptionList

from minimal import config
from minimal.core.app import App
from minimal.core.host import Host
from minimal.core.session import Session
from minimal.extensions import loader
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply, Usage
from minimal.ui.commands import register_builtin_commands
from minimal.ui.tui.app import MinimalTui
from minimal.ui.tui.composer import Composer
from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest, RecordingProvider, text_reply

ITEM = MARKETPLACE / "widgets" / "agent_monitor.py"


def load_module():
    spec = importlib.util.spec_from_file_location("agent_monitor_under_test", ITEM)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def agent(id, parent=None, name=None, status="done", started=100.0, finished=None, calls=0, **kw):
    fields = dict(
        id=id,
        parent_id=parent,
        name=name or id,
        status=status,
        model="m1",
        provider="fake",
        prompt="do it",
        started=started,
        finished=finished,
        tool_calls=calls,
        usage=Usage(),
        error=None,
    )
    fields.update(kw)
    return SimpleNamespace(**fields)


def tool_reply(name, **input):
    return Reply(content=[{"type": "tool_use", "id": "c1", "name": name, "input": input}], stop_reason="tool_use")


class AgentMonitorTest(BundledExtensionTest):
    def setUp(self):
        super().setUp()
        self.module = load_module()

    def ids(self, agents):
        return [(a.id, depth) for a, depth in self.module.flatten(agents)]

    # --- tree ----------------------------------------------------------------

    def test_children_follow_their_parent_depth_first_in_start_order(self):
        agents = [
            agent("main"),
            agent("a", "main"),
            agent("c", "main"),  # started before b, but b's parent started earlier: a's subtree comes first
            agent("b", "a"),
            agent("d", "b"),
        ]
        self.assertEqual(self.ids(agents), [("main", 0), ("a", 1), ("b", 2), ("d", 3), ("c", 1)])

    def test_agent_with_unknown_parent_is_a_root_and_nothing_is_dropped_or_repeated(self):
        agents = [agent("main"), agent("orphan", "gone"), agent("self", "self"), agent("x", "y"), agent("y", "x")]
        flat = self.ids(agents)
        self.assertEqual(sorted(i for i, _ in flat), sorted(a.id for a in agents))
        self.assertEqual(dict(flat)["orphan"], 0)
        self.assertEqual(dict(flat)["self"], 0)

    def test_finished_agents_show_their_outcome_and_a_frozen_elapsed_time(self):
        rows = {
            status: self.module.make_row(agent("a", status=status, started=100.0, finished=165.0), 0, now=9999.0)
            for status in ("done", "error", "interrupted", "max_turns")
        }
        self.assertEqual(len({row.glyph for row in rows.values()}), 3)  # done, error, a warning shared by the other two
        self.assertEqual(rows["done"].glyph, self.module.theme.SUCCESS)
        self.assertEqual(rows["error"].glyph, self.module.theme.ERROR)
        self.assertIn("1m05s", rows["done"].details)  # finished - started, not now - started
        running = self.module.make_row(agent("a", status="running", calls=1), 0, now=107.0)
        self.assertEqual(running.glyph, self.module.theme.RUNNING)
        self.assertEqual(running.details, "m1 · 1 call · 7.0s")

    def test_row_names_the_agent_and_indents_by_depth(self):
        row = self.module.make_row(agent("1f2e3d4c", "main", name="researcher", calls=3), 2, now=110.0)
        self.assertEqual(row.text(), f"    {self.module.theme.SUCCESS} researcher#1f2e3d4c  m1 · 3 calls · 10s")
        self.assertEqual(self.module.make_row(agent("main"), 0, now=100.0).label, "main")

    def test_format_elapsed(self):
        f = self.module.format_elapsed
        shown = [f(-5), f(0.04), f(9.96), f(42.9), f(125), f(3725)]
        self.assertEqual(shown, ["0.0s", "0.0s", "10.0s", "42s", "2m05s", "1h02m"])

    # --- panel ---------------------------------------------------------------

    def test_panel_is_hidden_until_there_is_more_than_the_idle_main_agent(self):
        visible = self.module.panel_lines
        self.assertIsNone(visible([], 0))
        self.assertIsNone(visible([agent("main", status="done")], 0))
        self.assertIsNone(visible([agent("main", status="interrupted")], 0))
        self.assertIsNotNone(visible([agent("main", status="running")], 0))
        self.assertIsNotNone(visible([agent("main"), agent("a", "main")], 0))
        self.assertIsNotNone(visible([agent("a", "main", status="error")], 0))  # a lone non-main agent still shows

    def test_panel_counts_running_agents_and_keeps_only_the_newest_rows(self):
        agents = [agent("main", status="running")] + [
            agent(f"a{i}", "main", status="running" if i == 0 else "done") for i in range(20)
        ]
        lines = self.module.panel_lines(agents, 200.0)
        header = "".join(p if isinstance(p, str) else p[0] for p in lines[0])
        self.assertEqual(header, "agents · 2 running · 21 total")
        self.assertEqual(lines[1], [("… 9 earlier", "muted")])
        self.assertEqual(len(lines), 1 + 1 + self.module.MAX_PANEL_ROWS)
        self.assertEqual(lines[-1][3], ("a19", "bold"))

    def test_panel_line_pieces_use_only_known_styles(self):
        lines = self.module.panel_lines([agent("main", status="running"), agent("a", "main", status="error")], 120.0)
        styles = {p[1] for line in lines for p in line if isinstance(p, tuple)}
        self.assertLessEqual(styles, {"bold", "dim", "muted", "warning", "success", "error"})

    # --- text escaping -------------------------------------------------------

    def test_control_characters_in_prompt_error_name_and_model_are_escaped_not_emitted(self):
        evil = agent(
            "e1",
            "main",
            name="n\x1b[31mame",
            model="m\x07",
            prompt="a\x1b]0;title\x07b\nsecond\x00line\r\nthird",
            error="boom\x1b[2J\nsecond line",
        )
        detail = self.module.format_detail(evil, 101.0, ["x \x1b[1my"], "reply\x1b[0m\nlast")
        row = self.module.make_row(evil, 0, 101.0).text()
        for text in (detail, row):
            self.assertFalse([c for c in text if c != "\n" and not c.isprintable()], repr(text))
        self.assertIn("\\x1b]0;title\\x07b", detail)
        self.assertIn("  second\\x00line", detail)  # line breaks survive, indented under "prompt:"
        self.assertIn("\\x1b[2J", detail)
        self.assertIn("n\\x1b[31mame", row)

    def test_long_prompt_is_cut_and_only_the_last_reply_lines_are_shown(self):
        detail = self.module.format_detail(
            agent("a", prompt="p" * 5000), 101.0, [], "\n".join(f"line{i}" for i in range(20))
        )
        self.assertIn("p" * 100 + "…", detail)
        self.assertNotIn("p" * 2000, detail)
        self.assertIn("line19", detail)
        self.assertIn("line14", detail)
        self.assertNotIn("line13", detail)

    def test_detail_reports_status_usage_parent_and_error(self):
        usage = Usage(input_tokens=1200, output_tokens=34, cache_read_tokens=1000)
        detail = self.module.format_detail(
            agent("a", "main", status="error", calls=2, usage=usage, error="rate limited", finished=130.0), 999.0
        )
        for expected in ("a · error · 30s", "parent: main", "1,200 in · 34 out", "cache 1,000 read", "2 calls",
                         "error: rate limited", "prompt:\n  do it"):
            self.assertIn(expected, detail)

    def test_find_agent_prefers_an_id_then_the_newest_agent_of_that_name(self):
        agents = [agent("main"), agent("aa11", name="w"), agent("bb22", name="w"), agent("w", name="other")]
        self.assertEqual(self.module.find_agent(agents, "bb22").id, "bb22")
        self.assertEqual(self.module.find_agent(agents, " w ").id, "w")  # an id beats a name
        self.assertEqual(self.module.find_agent(agents[:3], "w").id, "bb22")
        self.assertIsNone(self.module.find_agent(agents, "nope"))

    # --- events --------------------------------------------------------------

    def test_monitor_keeps_the_recent_calls_and_last_reply_of_each_agent(self):
        monitor = self.module.Monitor()
        for i in range(10):
            monitor.tool_result({"name": "read", "input": {"path": f"f{i}.py"}, "output": "ok", "agent_id": "a"})
        monitor.tool_result(
            {"name": "bash", "input": {"command": "false"}, "output": "exit 1\nmore", "is_error": True, "agent_id": "b"}
        )
        monitor.agent_end({"agent_id": "a", "text": "final words"})
        calls, reply = monitor.trace("a")
        self.assertEqual(len(calls), self.module.TRACE_LINES)
        self.assertTrue(calls[-1].endswith("read f9.py"))
        self.assertEqual(reply, "final words")
        self.assertEqual(monitor.trace("b")[0], [f"{self.module.theme.ERROR} bash false - exit 1 more"])
        self.assertEqual(monitor.trace("zzz"), ([], ""))
        monitor.tool_result({"name": "x"})  # a payload without an agent id is ignored, never raises
        monitor.clear()
        self.assertEqual(monitor.trace("a"), ([], ""))

    def test_monitor_forgets_the_oldest_agents_beyond_the_cap(self):
        monitor = self.module.Monitor()
        for i in range(self.module.MAX_TRACKED + 5):
            monitor.tool_result({"name": "read", "input": {}, "output": "", "is_error": False, "agent_id": f"a{i}"})
        self.assertEqual(monitor.trace("a0"), ([], ""))
        self.assertEqual(len(monitor.trace(f"a{self.module.MAX_TRACKED + 4}")[0]), 1)

    # --- a real app ----------------------------------------------------------

    def app_with_a_subagent(self):
        """A finished turn in which the model's ``spawn`` tool ran the agent ``helper`` (it called ``echo``)."""
        app = self.make_app("widgets/agent_monitor.py", Session("t", self.tmp / "t.jsonl"), [])
        app.registry.register_tool("echo", lambda input, ctx: "echoed")
        app.registry.register_tool(
            "spawn", lambda input, ctx: ctx.run_agent_result(input["prompt"], name="helper").text
        )
        app.provider = RecordingProvider(
            [
                tool_reply("spawn", prompt="investigate\x1b[31m the bug"),
                tool_reply("echo", path="notes.md"),
                text_reply("found it in parser.py"),
                text_reply("all done"),
            ]
        )
        self.run_turn(app, "please investigate")
        return app

    def command(self, app, line):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            app.run_command(line)
        return out.getvalue()

    def test_panel_and_command_describe_the_agents_a_real_turn_started(self):
        app = self.app_with_a_subagent()
        panel = app.registry.panels["agent_monitor"].render(app.context)
        self.assertIsNotNone(panel)
        names = [next(p[0] for p in line if isinstance(p, tuple) and p[1] == "bold") for line in panel[1:]]
        self.assertEqual(names[0], "main")
        self.assertTrue(names[1].startswith("helper#"), names)
        self.assertEqual(panel[2][0], "  ")  # the helper is indented under main

        tree = self.command(app, "/agents")
        self.assertIn("main  ", tree)
        self.assertRegex(tree, r"\n  \S helper#[0-9a-f]{8}  ")
        self.assertIn("needs the terminal UI", tree)  # no widget with a console frontend

    def test_agents_command_with_an_id_prints_prompt_recent_calls_and_reply_escaped(self):
        app = self.app_with_a_subagent()
        child = next(a for a in app.context.agents() if a.name == "helper")
        for query in (child.id, "helper"):
            out = self.command(app, f"/agents {query}")
            self.assertIn("prompt:\n  investigate\\x1b[31m the bug", out)
            self.assertIn("echo notes.md", out)
            self.assertIn("last reply:\n  found it in parser.py", out)
            self.assertNotIn("\x1b", out)
        main = self.command(app, "/agents main")
        self.assertIn("all done", main)
        self.assertIn("1 call", main)  # spawn; the helper's echo is counted under the helper

    def test_agents_command_reports_what_is_wrong(self):
        app = self.make_app("widgets/agent_monitor.py", Session("t", self.tmp / "t.jsonl"), [])
        self.assertIn("No agents yet", self.command(app, "/agents"))
        app.provider = RecordingProvider([text_reply("hi")])
        self.run_turn(app, "hello")
        self.assertIn("no agent 'zzz'; run /agents to list them", self.command(app, "/agents zzz"))
        self.assertIsNone(app.registry.panels["agent_monitor"].render(app.context))  # only the idle main agent


@unittest.skipUnless(MARKETPLACE.is_dir(), "no marketplace checkout; set MARKETPLACE_CHECKOUT")
class AgentMonitorTuiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for patcher in (
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.tmp / "global"),
            mock.patch.object(config, "SETTINGS_FILE", self.tmp / "settings.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.core = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            trust_project=True,
            extra_extensions=[str(ITEM)],
            register_commands=register_builtin_commands,
        )
        _, errors = self.core.reload_extensions()
        self.assertEqual(errors, [])
        self.core.registry.register_tool(
            "spawn", lambda input, ctx: ctx.run_agent_result(input["prompt"], name="helper").text
        )
        self.core.provider_name, self.core.model = "fake", "m"
        self.core.provider = RecordingProvider(
            [tool_reply("spawn", prompt="look around"), text_reply("child reply"), text_reply("main reply")]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.core.run_turn("start")
        # A third agent still running, as one started from a background thread would be.
        self.worker = self.core.agents.begin(
            name="worker", parent_id="main", prompt="long task", model="m", provider="fake"
        )

    async def wait_until(self, pilot, condition):
        for _ in range(100):
            await pilot.pause(0.05)
            if condition():
                return
        self.fail("timed out")

    def idle(self, app):
        return lambda: not app.busy

    async def test_browser_shows_the_tree_the_selected_detail_and_interrupts_a_running_agent(self):
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            app.query_one("#prompt", Composer).set_text("/agents")
            await pilot.press("enter")
            await self.wait_until(pilot, lambda: type(app.screen).__name__ == "AgentsScreen")
            screen = app.screen
            options = screen.query_one(OptionList)
            await self.wait_until(pilot, lambda: options.option_count == 3 and screen.selected == "main")
            self.assertEqual([options.get_option_at_index(i).id for i in range(3)][0], "main")
            self.assertIn("main reply", screen.shown)

            await pilot.press("down", "down")
            await self.wait_until(pilot, lambda: screen.selected == self.worker.id)
            self.assertIn("long task", screen.shown)
            self.assertIn("running", screen.shown)

            await pilot.press("c")
            await self.wait_until(pilot, self.worker.cancel.is_set)

            await pilot.press("up")  # the finished helper: nothing to interrupt
            await self.wait_until(pilot, lambda: screen.selected != self.worker.id)
            self.assertIn("look around", screen.shown)
            self.assertIn("child reply", screen.shown)
            await pilot.press("escape")
            await self.wait_until(pilot, self.idle(app))
            self.assertEqual(type(app.screen).__name__, "Screen")

    async def test_closing_with_no_interaction_returns_the_prompt(self):
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            app.query_one("#prompt", Composer).set_text("/agents")
            await pilot.press("enter")
            await self.wait_until(pilot, lambda: type(app.screen).__name__ == "AgentsScreen")
            await pilot.press("c")  # on main: it is not running, so nothing is interrupted
            self.assertFalse(self.core.cancelled.is_set())
            await pilot.press("q")
            await self.wait_until(pilot, self.idle(app))
