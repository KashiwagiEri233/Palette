# Desktop design rules

The selected system is Cashmere. Its installed components live in
`src/renderer/components/ui`, and tokens live in `src/renderer/globals.css`.
Compose those primitives rather than rebuilding buttons, tables, forms, dialogs or navigation.
Use the installed Iconoir assets in `src/renderer/components/icons` for visible glyphs.
Inter fonts are bundled locally. Application CSS controls layout. The desktop uses a
Codex-inspired neutral workspace: matching title bar and icon rail, one continuous
content surface, aligned compact headers, flat lists and restrained borders.
Application token overrides live in `app.css`; `workspace.css` owns layout.
Use the Card workspace/pane/input and Sidebar inset/rail variants for embedded
surfaces rather than overriding component shapes from feature CSS.
Preserve the original Cashmere radius scale with a 1rem base: pill-shaped actions,
lg fields and navigation, 2xl cards and composers, xl menus and dialogs, and md
menu items. Workspace cards use lg; embedded panes stay square at their shared
edges. Use the existing radius tokens and component variants, without overriding
their colors, borders, shadows or focus treatment in page CSS. All selection
menus use the themed Select with local icons.

Keep the user interface limited to VM data, settings, states and actions. Never display
prompts, planning instructions, design-system provenance or implementation commentary.
Confirm destructive operations; normal shutdown must not silently become a forced stop.
Treat the console display as read-only, and route human input through the typed preload API.

Review real Electron renders, font loading, component use, spacing, keyboard focus,
contrast and all requested interactions. Fake-QEMU checks are transport evidence only.
