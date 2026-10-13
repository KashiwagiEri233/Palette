import * as React from "react"
import { Progress as ProgressPrimitive } from "@base-ui/react/progress"

import { cn } from "@/lib/utils"

const Progress = React.forwardRef<
  HTMLDivElement,
  Omit<ProgressPrimitive.Root.Props, "value"> & {
    value?: ProgressPrimitive.Root.Props["value"]
  }
>(({ className, value, ...props }, ref) => (
  <ProgressPrimitive.Root
    ref={ref}
    value={value ?? null}
    className={cn(
      "relative h-2 w-full overflow-hidden rounded-full border border-border",
      "bg-input [box-shadow:var(--shadow-inset)]",
      className
    )}
    {...props}
  >
    <ProgressPrimitive.Track className="h-full w-full">
      <ProgressPrimitive.Indicator className="h-full w-full flex-1 bg-primary transition-all duration-300 [box-shadow:var(--shadow-primary)]" />
    </ProgressPrimitive.Track>
  </ProgressPrimitive.Root>
))
Progress.displayName = "Progress"

export { Progress }
