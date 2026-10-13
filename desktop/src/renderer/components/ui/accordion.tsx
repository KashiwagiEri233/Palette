"use client"

import * as React from "react"
import { Accordion as AccordionPrimitive } from "@base-ui/react/accordion"
import { Icon } from "@iconify/react"

import { cn } from "@/lib/utils"

const Accordion = AccordionPrimitive.Root

// Base UI className can be a state function; the wrappers feed it to cn(), so
// narrow it to a plain string (same treatment as the other ported files).
type WithClassName<P> = Omit<P, "className"> & { className?: string }

function AccordionItem({
  className,
  ...props
}: WithClassName<AccordionPrimitive.Item.Props>) {
  return (
    <AccordionPrimitive.Item
      className={cn("border-b border-border last:border-b-0", className)}
      {...props}
    />
  )
}
AccordionItem.displayName = "AccordionItem"

function AccordionTrigger({
  className,
  children,
  ...props
}: WithClassName<AccordionPrimitive.Trigger.Props>) {
  return (
    <AccordionPrimitive.Header className="flex">
      <AccordionPrimitive.Trigger
        className={cn(
          "flex flex-1 items-center justify-between py-4 text-sm font-medium text-foreground",
          "transition-colors duration-150",
          "hover:text-primary",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 rounded-md",
          "[&[data-panel-open]>svg]:rotate-180",
          className
        )}
        {...props}
      >
        {children}
        <Icon
          icon="ph:caret-down"
          className="h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200"
        />
      </AccordionPrimitive.Trigger>
    </AccordionPrimitive.Header>
  )
}
AccordionTrigger.displayName = "AccordionTrigger"

function AccordionContent({
  className,
  children,
  ...props
}: WithClassName<AccordionPrimitive.Panel.Props>) {
  return (
    <AccordionPrimitive.Panel
      className="h-[var(--accordion-panel-height)] overflow-hidden text-sm text-muted-foreground transition-[height] duration-200 ease-out data-starting-style:h-0 data-ending-style:h-0"
      {...props}
    >
      <div className={cn("pb-4 pt-0 leading-relaxed", className)}>{children}</div>
    </AccordionPrimitive.Panel>
  )
}
AccordionContent.displayName = "AccordionContent"

export { Accordion, AccordionItem, AccordionTrigger, AccordionContent }
