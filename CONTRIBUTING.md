# Contributing

You don't have to write code to help. If meydan is missing something your app or your client
needs, [open an issue](https://github.com/pehlivan-ai/meydan/issues/new/choose) ("Missing for my
app"): what the people using it need, what you looked for, and how you work around it today. That
is how meydan decides what becomes a part.

If you do write code:

- Python 3.12, managed with uv. `uv sync --all-extras` to set up.
- Ruff is the linter and formatter; code isn't done until `uv run ruff check .` and
  `uv run ruff format --check .` pass. Tests: `uv run pytest`.
- Keep the core framework-agnostic: tools and planning logic are plain Python; adapters wrap
  them for a given agent framework.
- Nothing instance-specific in this repo: no project ids, domains, phone numbers or secrets.
  Those belong in the instance.
- Times are timezone-aware everywhere; convert only through `meydan.clock`.
- Open an issue before large changes. By contributing you agree your work is licensed under
  Apache-2.0.
