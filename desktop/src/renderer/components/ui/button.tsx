import * as React from "react"
import { Button as ButtonPrimitive } from "@base-ui/react/button"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/*
 * Cashmere actions use a flat fill and a pill shape. Navigation uses the
 * existing rounded rectangle shared by sidebar rows.
 */

const buttonVariants = cva(
  [
    "inline-flex items-center justify-center gap-2 whitespace-nowrap",
    "text-sm font-medium leading-none",
    "rounded-full cursor-pointer select-none",
    "transition-[background-color,box-shadow,transform,color] duration-150 ease-out",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:ring-offset-2 focus-visible:ring-offset-background",
    "disabled:pointer-events-none disabled:opacity-50",
    "active:scale-[0.97]",
  ].join(" "),
  {
    variants: {
      variant: {
        // Primary action: flat, with a slightly lighter hover fill.
        default:
          "bg-primary text-primary-foreground [box-shadow:var(--shadow-xs)] " +
          "hover:bg-primary/90",
        primary:
          "bg-primary text-primary-foreground [box-shadow:var(--shadow-xs)] " +
          "hover:bg-primary/90",
        // Secondary action: neutral fill with a hairline ring.
        secondary:
          "bg-secondary text-secondary-foreground [box-shadow:var(--shadow-button)] " +
          "hover:bg-accent hover:[box-shadow:var(--shadow-button-hover)]",
        outline:
          "bg-transparent text-foreground border border-border " +
          "hover:bg-accent hover:border-foreground/20 hover:text-accent-foreground",
        ghost:
          "bg-transparent text-muted-foreground " +
          "hover:bg-accent hover:text-accent-foreground",
        navigation:
          "rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground " +
          "aria-[current=page]:bg-accent aria-[current=page]:text-foreground",
        destructive:
          "bg-destructive text-destructive-foreground [box-shadow:var(--shadow-xs)] " +
          "hover:brightness-105",
        link: "text-foreground underline-offset-4 hover:underline hover:text-ring",
      },
      size: {
        sm: "h-8 px-4 text-xs",
        default: "h-10 px-5",
        lg: "h-12 px-7 text-base",
        icon: "h-10 w-10",
        "icon-sm": "h-8 w-8",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

export interface ButtonProps
  extends Omit<ButtonPrimitive.Props, "className">,
    VariantProps<typeof buttonVariants> {
  className?: string
  asChild?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    // Callers written against the Radix API pass the element to become as a
    // child: <Button asChild><Link>…</Link></Button>. Base UI has no asChild,
    // it composes through `render`. Without this the prop reaches the DOM and
    // the anchor nests inside the button instead of replacing it, which stacks
    // the icon above the label and puts a link inside a button.
    if (asChild && React.isValidElement(props.children)) {
      const { children, ...rest } = props
      return (
        <ButtonPrimitive
          ref={ref}
          className={cn(buttonVariants({ variant, size, className }))}
          {...rest}
          // After the spread, so an explicit asChild child wins over a stray
          // render prop rather than being silently replaced by it.
          // SAFETY: this branch runs only when `asChild` is set, and the caller then
          // owns passing a single element for the slot to clone.
          render={children as React.ReactElement}
        />
      )
    }

    return (
      <ButtonPrimitive
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    )
  }
)
Button.displayName = "Button"

export { Button, buttonVariants }
