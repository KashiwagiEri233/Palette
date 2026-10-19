import * as React from "react"
import { Icon } from "@iconify/react"

import { cn } from "@/lib/utils"

export interface EmptyStateProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  icon?: string | React.ReactNode
  variant?: "default" | "plain"
  heading?: "div" | "h1" | "h2"
  title: React.ReactNode
  description?: React.ReactNode
  action?: React.ReactNode
}

const EmptyState = React.forwardRef<HTMLDivElement, EmptyStateProps>(
  ({ className, icon = "ph:package", variant = "default", heading: Heading = "div", title, description, action, ...props }, ref) => (
    <div
      ref={ref}
      data-slot="empty-state"
      data-variant={variant}
      className={cn(
        "flex flex-col items-center justify-center gap-4 p-8 text-center text-card-foreground",
        variant === "default" && "rounded-xl bg-card border border-dashed border-border",
        className
      )}
      {...props}
    >
      <div
        className={cn(
          "flex h-12 w-12 items-center justify-center rounded-full",
          "bg-muted text-muted-foreground",
          "[box-shadow:var(--shadow-s)]"
        )}
      >
        {typeof icon === "string" ? <Icon icon={icon} className="h-5 w-5" /> : icon}
      </div>
      <div className="space-y-1 max-w-md">
        <Heading className="text-base font-semibold text-foreground">{title}</Heading>
        {description && (
          <div className="text-sm text-muted-foreground leading-relaxed">{description}</div>
        )}
      </div>
      {action && <div className="pt-1">{action}</div>}
    </div>
  )
)
EmptyState.displayName = "EmptyState"

export { EmptyState }
