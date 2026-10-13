import type { CSSProperties } from "react"

/* Card / sheet surface: flat. Depth comes from the ring + float shadow tokens,
 * never a fill gradient — that is what makes Cashmere read as paper. */
export const cashmereSurfaceStyle: CSSProperties = {}

/* Popover / floating-surface: flat. Same reasoning — clean token background,
 * hairline ring from --shadow-l, no top sheen. */
export const cashmereGrainStyle: CSSProperties = {}

export const formFieldBase =
  "w-full rounded-lg px-3.5 text-sm " +
  "bg-input text-foreground " +
  "border border-border " +
  "placeholder:text-muted-foreground " +
  "transition-[color,box-shadow,border-color] duration-150 " +
  "[box-shadow:var(--shadow-inset)] " +
  "hover:border-foreground/20 " +
  "focus-visible:outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35 " +
  "disabled:cursor-not-allowed disabled:opacity-50"

export const formFieldSingleLine = "flex h-10 py-2"
export const formFieldMultiLine = "flex min-h-[84px] py-2.5 resize-y"
