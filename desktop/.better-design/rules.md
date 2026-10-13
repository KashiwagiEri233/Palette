# Desktop design rules

The selected system is Cashmere. Its installed components live in
`src/renderer/components/ui`, and tokens live in `src/renderer/globals.css`.
Compose those primitives rather than rebuilding buttons, tables, forms, dialogs or navigation.
Use the installed Iconoir assets in `src/renderer/components/icons` for visible glyphs.
Inter fonts are bundled locally. Application CSS controls layout and uses existing tokens.

Keep the user interface limited to VM data, settings, states and actions. Never display
prompts, planning instructions, design-system provenance or implementation commentary.
Confirm destructive operations; normal shutdown must not silently become a forced stop.
Treat the console display as read-only, and route human input through the typed preload API.

Review real Electron renders, font loading, component use, spacing, keyboard focus,
contrast and all requested interactions. Fake-QEMU checks are transport evidence only.
