import * as React from "react"
import { Toggle as TogglePrimitive } from "@base-ui/react/toggle"
import { ToggleGroup as ToggleGroupPrimitive } from "@base-ui/react/toggle-group"
import { type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"
import { toggleVariants } from "./toggle"

const ToggleGroupContext = React.createContext<VariantProps<typeof toggleVariants>>({
  size: "default",
  variant: "default",
})

interface ToggleGroupRootProps
  extends Omit<ToggleGroupPrimitive.Props, "className">,
    VariantProps<typeof toggleVariants> {
  className?: string
}

const ToggleGroup = React.forwardRef<HTMLDivElement, ToggleGroupRootProps>(
  ({ className, variant, size, children, ...props }, ref) => (
    <ToggleGroupPrimitive
      ref={ref}
      className={cn(
        "inline-flex items-center gap-0.5 rounded-xl p-1",
        "bg-card border border-border [box-shadow:var(--shadow-inset)]",
        className
      )}
      {...props}
    >
      <ToggleGroupContext.Provider value={{ variant, size }}>{children}</ToggleGroupContext.Provider>
    </ToggleGroupPrimitive>
  )
)
ToggleGroup.displayName = "ToggleGroup"

interface ToggleGroupItemProps
  extends Omit<TogglePrimitive.Props, "className">,
    VariantProps<typeof toggleVariants> {
  className?: string
}

const ToggleGroupItem = React.forwardRef<HTMLButtonElement, ToggleGroupItemProps>(
  ({ className, children, variant, size, ...props }, ref) => {
    const context = React.useContext(ToggleGroupContext)
    return (
      <TogglePrimitive
        ref={ref}
        className={cn(
          toggleVariants({
            variant: context.variant || variant,
            size: context.size || size,
          }),
          "data-pressed:bg-popover data-pressed:text-foreground data-pressed:[box-shadow:var(--shadow-s)]",
          className
        )}
        {...props}
      >
        {children}
      </TogglePrimitive>
    )
  }
)
ToggleGroupItem.displayName = "ToggleGroupItem"

export { ToggleGroup, ToggleGroupItem }
