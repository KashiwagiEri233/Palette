import * as React from "react"
import { cn } from "@/lib/utils"
import { cashmereSurfaceStyle } from "./_shared"

/*
 * Cashmere Card — a crisp white panel on the cream paper ground.
 * Flat fill (no gradient), a hairline ring + a very soft float (--shadow-s) and a
 * generous squircle radius, so it reads as clean lifted paper. Interactive cards
 * float a touch higher on hover.
 */

interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  interactive?: boolean
  variant?: "default" | "workspace" | "pane" | "input"
}

const Card = React.forwardRef<HTMLDivElement, CardProps>(
  ({ className, interactive, variant = "default", style, ...props }, ref) => (
    <div
      ref={ref}
      data-slot="card"
      data-variant={variant}
      style={{ ...cashmereSurfaceStyle, ...style }}
      className={cn(
        "bg-card text-card-foreground",
        variant === "default" && "rounded-2xl [box-shadow:var(--shadow-s)]",
        variant === "workspace" && "flex h-full min-h-0 flex-col overflow-hidden rounded-lg",
        variant === "pane" && "flex h-full min-h-0 min-w-0 flex-col rounded-none",
        variant === "input" && "rounded-2xl border border-border [box-shadow:var(--shadow-xs)] focus-within:ring-2 focus-within:ring-ring/25 focus-within:border-ring",
        "transition-[box-shadow,transform] duration-200",
        interactive &&
          "cursor-pointer hover:-translate-y-0.5 hover:[box-shadow:var(--shadow-l)]",
        className
      )}
      {...props}
    />
  )
)
Card.displayName = "Card"

const CardHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("flex flex-col gap-1.5 p-5", className)} {...props} />
  )
)
CardHeader.displayName = "CardHeader"

const CardTitle = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("text-base font-semibold text-foreground leading-snug", className)} {...props} />
  )
)
CardTitle.displayName = "CardTitle"

const CardDescription = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("text-sm text-muted-foreground", className)} {...props} />
  )
)
CardDescription.displayName = "CardDescription"

const CardContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("px-5 pb-5", className)} {...props} />
  )
)
CardContent.displayName = "CardContent"

const CardFooter = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("flex items-center gap-2 px-5 pb-5", className)} {...props} />
  )
)
CardFooter.displayName = "CardFooter"

export { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter }
