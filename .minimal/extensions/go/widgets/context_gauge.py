"""Context gauge: a one-line bar of how full the context window is, with the prompt-cache hit rate."""

__version__ = "1.4"
__api__ = "2"

DEFAULTS = {"limit": 200_000, "warn": 0.5, "crit": 0.8}
"""Context window size in tokens, and the fill fractions from which the bar turns warning / error."""

WIDTH = 24
"""Width of the bar in cells."""

EIGHTHS = "▏▎▍▌▋▊▉"


def _bar(fraction: float, width: int, color: str) -> list:
    """A ``width``-cell gauge filled to ``fraction`` in ``color``, padded with dim ``░``, as pieces."""
    eighths = round(max(0.0, min(fraction, 1.0)) * width * 8)
    full, part = divmod(eighths, 8)
    filled = "█" * full + (EIGHTHS[part - 1] if part else "")
    return [(filled, color), ("░" * (width - len(filled)), "dim")]


def _last_usage(messages: list) -> dict | None:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("usage"):
            return message["usage"]
    return None


class ContextGauge:
    def __init__(self, settings) -> None:
        self.settings = settings

    def render(self, ctx):
        tokens = ctx.context_tokens
        if tokens is None:
            return None
        limit, warn, crit = self.settings["limit"], self.settings["warn"], self.settings["crit"]
        frac = min(tokens / limit, 1)
        color = "success" if frac < warn else "warning" if frac < crit else "error"
        parts = [
            ("ctx ", "muted"),
            "▕",
            *_bar(frac, WIDTH, color),
            "▏ ",
            f"{tokens / 1000:.1f}k/{limit // 1000}k {frac:.0%}",
        ]
        usage = _last_usage(ctx.messages)
        if usage:
            inp, read = usage.get("input_tokens") or 0, usage.get("cache_read_tokens") or 0
            if inp > 0 and read > 0:
                parts.append((f" · cache {read / inp:.0%}", "dim"))
        return [parts]


def register(minimal):
    settings = minimal.declare_settings(DEFAULTS)
    panel = ContextGauge(settings)
    minimal.register_panel("context_gauge", panel.render, area="bottom")
