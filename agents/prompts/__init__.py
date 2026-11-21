"""Prompt templates. Kept as Markdown files so they can be read and reviewed
without reading code. Loaded with `agents.prompts.load()`."""

from functools import cache
from importlib.resources import files
from string import Template


@cache
def load(name: str) -> Template:
    """Return the template `<name>.md`. Placeholders look like `$today`.

    `string.Template` is used instead of `str.format`, so braces in a
    prompt (JSON examples) need no escaping.
    """
    text = files(__name__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    return Template(text)
