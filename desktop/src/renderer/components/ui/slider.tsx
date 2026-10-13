import * as React from "react"
import { Slider as SliderPrimitive } from "@base-ui/react/slider"

import { cn } from "@/lib/utils"

/*
 * Cashmere Slider: a soft recessed track; the filled Indicator is the flat
 * near-black fill (bg-primary + --shadow-primary), matching the primary
 * Button. The thumb is a small white knob with a soft float.
 */

const Slider = React.forwardRef<HTMLDivElement, SliderPrimitive.Root.Props>(
  ({ className, defaultValue, value, min = 0, max = 100, ...props }, ref) => {
    const _values = Array.isArray(value)
      ? value
      : Array.isArray(defaultValue)
        ? defaultValue
        : [min, max]

    return (
      <SliderPrimitive.Root
        data-slot="slider"
        ref={ref}
        thumbAlignment="edge"
        defaultValue={defaultValue}
        value={value}
        min={min}
        max={max}
        className={cn("relative w-full", className)}
        {...props}
      >
        <SliderPrimitive.Control className="flex w-full touch-none select-none items-center">
          <SliderPrimitive.Track
            data-slot="slider-track"
            className={cn(
              "relative h-1.5 w-full grow overflow-hidden rounded-full border border-border",
              "bg-input [box-shadow:var(--shadow-inset)]"
            )}
          >
            <SliderPrimitive.Indicator data-slot="slider-range" className="absolute h-full bg-primary [box-shadow:var(--shadow-primary)]" />
          </SliderPrimitive.Track>
          {Array.from({ length: _values.length }, (_, index) => (
            <SliderPrimitive.Thumb
              data-slot="slider-thumb" key={index}
              className={cn(
                "block h-4 w-4 rounded-full border border-border bg-popover",
                "[box-shadow:var(--shadow-m)]",
                "transition-[background-color,box-shadow] duration-150",
                "hover:[box-shadow:var(--shadow-l)] hover:bg-accent",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40",
                "data-disabled:pointer-events-none data-disabled:opacity-50"
              )}
            />
          ))}
        </SliderPrimitive.Control>
      </SliderPrimitive.Root>
    )
  }
)
Slider.displayName = "Slider"

export { Slider }
