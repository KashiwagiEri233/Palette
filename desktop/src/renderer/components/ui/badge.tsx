import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/*
 * Cashmere Badge — a soft rounded event chip, the way Cashmere tags calendar
 * categories. A soft pop-tint fill with deep readable text: playful accent chips.
 * The five vivid pop hues (blue, green, orange, yellow, purple) double as the
 * accent palette; semantic variants map onto the closest pop.
 */

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold " +
    "transition-colors duration-150",
  {
    variants: {
      variant: {
        default: "bg-secondary text-secondary-foreground",
        primary: "bg-primary text-primary-foreground",
        secondary: "bg-muted text-muted-foreground",
        outline: "bg-transparent text-foreground ring-1 ring-inset ring-border",
        orange: "bg-pop-orange text-pop-orange-foreground",
        blue: "bg-pop-blue text-pop-blue-foreground",
        green: "bg-pop-green text-pop-green-foreground",
        yellow: "bg-pop-yellow text-pop-yellow-foreground",
        purple: "bg-pop-purple text-pop-purple-foreground",
        success: "bg-pop-green text-pop-green-foreground",
        warning: "bg-pop-yellow text-pop-yellow-foreground",
        destructive: "bg-destructive text-destructive-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

const Badge = React.forwardRef<HTMLDivElement, BadgeProps>(
  ({ className, variant, ...props }, ref) => (
    <div ref={ref} className={cn(badgeVariants({ variant }), className)} {...props} />
  )
)
Badge.displayName = "Badge"

export { Badge, badgeVariants }
