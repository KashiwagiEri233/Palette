import * as React from "react"
import { PreviewCard as PreviewCardPrimitive } from "@base-ui/react/preview-card"

import { cn } from "@/lib/utils"
import { cashmereGrainStyle } from "./_shared"

const HoverCard = PreviewCardPrimitive.Root
const HoverCardTrigger = PreviewCardPrimitive.Trigger

const HoverCardContent = React.forwardRef<
  HTMLDivElement,
  Omit<PreviewCardPrimitive.Popup.Props, "className" | "style"> &
    Pick<
      PreviewCardPrimitive.Positioner.Props,
      "align" | "alignOffset" | "side" | "sideOffset"
    > & { className?: string; style?: React.CSSProperties }
>(
  (
    { className, align = "center", alignOffset, side, sideOffset = 6, style, ...props },
    ref
  ) => (
    <PreviewCardPrimitive.Portal>
      <PreviewCardPrimitive.Positioner
        align={align}
        alignOffset={alignOffset}
        side={side}
        sideOffset={sideOffset}
        className="isolate z-50"
      >
        <PreviewCardPrimitive.Popup
          ref={ref}
          style={{ ...cashmereGrainStyle, ...style }}
          className={cn(
            "z-50 w-64 p-4",
            "rounded-xl border border-border",
            "bg-popover text-popover-foreground",
            "[box-shadow:var(--shadow-l)]",
            "outline-none transition-[transform,translate,scale,opacity] duration-150 data-starting-style:opacity-0 data-starting-style:scale-95 data-ending-style:opacity-0 data-ending-style:scale-95 data-[side=bottom]:data-starting-style:-translate-y-2 data-[side=left]:data-starting-style:translate-x-2 data-[side=right]:data-starting-style:-translate-x-2 data-[side=top]:data-starting-style:translate-y-2",
            className
          )}
          {...props}
        />
      </PreviewCardPrimitive.Positioner>
    </PreviewCardPrimitive.Portal>
  )
)
HoverCardContent.displayName = "HoverCardContent"

export { HoverCard, HoverCardTrigger, HoverCardContent }
