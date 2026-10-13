# Repository Guidelines

## Project Structure & Module Organization

`src/palette/` contains the Python MCP server, shared runtime owner, persistent VM management, display gateway, and input control. QEMU lifecycle code lives in `vm/`; management commands live in `qmp/`. Backend tests in `tests/` cover unit, integration, IPC, and hardware behavior.

`desktop/` is the Electron/React/TypeScript application: `src/main/` handles privileged operations, `src/preload/` exposes typed business APIs, and `src/renderer/` contains UI components and local assets. Frontend tests live in `desktop/tests/` and `desktop/e2e/`. Host profiles are in `profiles/`; usage and acceptance instructions are in `docs/`.

## Build, Test, and Development Commands

- `uv sync --locked`: install Python dependencies from the lockfile.
- `uv run palette doctor`: check host dependencies and profile compatibility.
- `uv run palette daemon start`: start the shared backend; `uv run palette serve --connect-only` attaches an MCP session.
- `uv run pytest`: run backend tests.
- `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy`: validate Python style and types.
- `uv run python scripts/check_docs.py`: validate documentation.
- `npm --prefix desktop ci` and `npm --prefix desktop run dev`: install and launch desktop development.
- `npm --prefix desktop run build`, `npm --prefix desktop test`, and `npm --prefix desktop run test:e2e`: type-check/build, run Vitest, and run Electron Playwright tests.

## Coding Style & Naming Conventions

Use four-space Python indentation, type annotations, `snake_case` functions, and `PascalCase` classes. Ruff enforces a 100-character line limit; mypy runs in strict mode. Follow existing two-space TypeScript formatting, `camelCase` variables, and `PascalCase` React components. Reuse installed UI primitives, local fonts, and icons. Keep prompts and implementation-plan text out of the interface.

## Testing Guidelines

Use pytest/pytest-asyncio for backend behavior, Vitest for frontend logic, and Playwright for Electron flows. Name tests `test_*.py`, `*.test.ts`, or `*.spec.ts`. Cover lifecycle failures, persistent data, input ordering, and control ownership when changing these paths. Real QEMU tests require explicit configuration; skipped or simulated tests do not establish hardware acceptance.

## Commit & Pull Request Guidelines

Use `Lin_ge/<topic>` branches and typed, imperative commits, e.g. `fix: restore fullscreen controls`. History uses `feat`, `fix`, `test`, `docs`, and `ci`. Describe behavior changes, relevant issues, validation, and platform limitations in PRs; include screenshots for UI changes. Update paired English/Chinese user documentation together.

## Security & Configuration

Keep credentials in the backend/main process and preserve renderer sandboxing. Route mutations through shared runtime and control policies. Never commit guest disks, ISOs, credentials, or `.runtime/` artifacts.
