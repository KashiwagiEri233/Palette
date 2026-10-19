import * as React from "react"
import { Checkbox as CheckboxPrimitive } from "@base-ui/react/checkbox"
import { CheckIcon } from "@/components/icons"

import { cn } from "@/lib/utils"

/*
 * Cashmere Checkbox: a soft recessed pocket when empty (faint inset), and
 * when checked it becomes the same flat near-black fill as the primary Button
 * (bg-primary + --shadow-primary), so the filled control and the button read as
 * one material.
 */

// Base UI splits Radix's checked="indeterminate" into a separate boolean
// prop; the wrapper keeps the Radix-shaped `checked` API and maps it.
type CheckboxProps = Omit<
  CheckboxPrimitive.Root.Props,
  "className" | "checked"
> & {
  className?: string
  checked?: boolean | "indeterminate"
}

const Checkbox = React.forwardRef<HTMLButtonElement, CheckboxProps>(
  ({ className, checked, ...props }, ref) => {
    const isIndeterminate = checked === "indeterminate"
    return (
      <CheckboxPrimitive.Root
        data-slot="checkbox"
        ref={ref}
        className={cn(
          "peer h-4 w-4 shrink-0 rounded-sm border border-border",
          "bg-input [box-shadow:var(--shadow-inset)]",
          "transition-[background-color,box-shadow,border-color] duration-150",
          "hover:border-ring/40",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40",
          "disabled:cursor-not-allowed disabled:opacity-50",
          "data-checked:bg-primary data-checked:border-primary data-checked:text-primary-foreground data-checked:[box-shadow:var(--shadow-primary)]",
          "data-indeterminate:bg-primary data-indeterminate:border-primary data-indeterminate:text-primary-foreground data-indeterminate:[box-shadow:var(--shadow-primary)]",
          className
        )}
        checked={isIndeterminate ? false : checked}
        indeterminate={isIndeterminate}
        nativeButton
        render={<button type="button" />}
        {...props}
      >
        <CheckboxPrimitive.Indicator data-slot="checkbox-indicator" className="size-full flex items-center justify-center text-current">
          {isIndeterminate ? (
            <span aria-hidden="true" className="h-0.5 w-2.5 rounded-full bg-current" />
          ) : (
            <CheckIcon aria-hidden="true" className="h-3 w-3" />
          )}
        </CheckboxPrimitive.Indicator>
      </CheckboxPrimitive.Root>
    )
  }
)
Checkbox.displayName = "Checkbox"

export { Checkbox }
