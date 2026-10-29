# Connecting an external agent

Read this when Palette tools are missing, screenshots are not reaching the model, or the user is installing this skill in another harness.

## Skill installation

Import or copy the entire `palette-vm` folder, including `SKILL.md`, `agents/` and `references/`, into the calling harness's supported skill directory. A ZIP of this folder can be used when the harness supports skill archives. OpenAI-specific UI metadata in `agents/openai.yaml` is optional for other harnesses; the instructions and references do not depend on it.

If the harness has no skill loader, explicitly provide `SKILL.md` as agent instructions and make the referenced files available. Use the harness's own invocation convention; in a Codex skills environment, the skill is named `$palette-vm`.

Installing the skill does not install Palette, configure an MCP server, start VMs or grant lifecycle permissions.

## Existing shared backend

Palette Desktop and external MCP clients can share the same backend. The MCP server uses stdio, not an HTTP URL.

Example configuration for a client accepting an `mcpServers` object:

```json
{
  "mcpServers": {
    "palette": {
      "command": "uv",
      "args": [
        "run",
        "--project", "ABSOLUTE_PALETTE_CHECKOUT",
        "python", "-m", "palette.cli",
        "--root", "ABSOLUTE_CONFIGURATION_ROOT",
        "serve", "--connect-only"
      ]
    }
  }
}
```

Replace both uppercase path placeholders with actual absolute paths. The project checkout contains Palette's installed Python project; the configuration root must match the intended Desktop/backend environment. The two paths may be identical. Use the client's accepted executable-path and configuration format, and keep diagnostics off the stdio protocol stream.

`--connect-only` attaches to an existing compatible owner and fails if none is running. If the user wants a backend started, the normal host command is `uv run palette daemon start` with the same configuration root, or they can open Palette Desktop. Make host setup changes only when authorized. Do not switch to `--standalone` to recover a failed shared connection: that mode owns a separate backend lifecycle and closes its VMs when the MCP session ends.

## Connection checks

- Discover the current tool schemas. A client may prefix names with the configured server alias.
- Call `vm_list` and confirm that the returned VMs are the expected ones before acting. Empty results can mean the wrong configuration root, not permission to create a replacement VM.
- Confirm that the client forwards `ImageContent` PNG blocks from `vm_observe`, `vm_act` and `vm_wait` to a vision-capable model. A text-only tool wrapper can hide the image while still reporting a successful call.
- No public Palette `todo_write` tool is advertised. Use the external harness's own native planning tool for its progress panel. Neither an imported skill nor MCP connection establishes a bridge into a Desktop conversation's task list.
