import * as React from "react"
import { Select as SelectPrimitive } from "@base-ui/react/select"
import { CheckIcon, NavArrowDownIcon } from "@/components/icons"

import { cn } from "@/lib/utils"
import { cashmereGrainStyle, formFieldBase, formFieldSingleLine } from "./_shared"

const Select = SelectPrimitive.Root
const SelectGroup = SelectPrimitive.Group
const SelectValue = SelectPrimitive.Value

// Base UI className can be a state function; the wrappers feed it to cn(), so
// narrow it to a plain string (same treatment as the other ported files).
type WithClassName<P> = Omit<P, "className"> & { className?: string }

function SelectTrigger({
  className,
  children,
  ...props
}: WithClassName<SelectPrimitive.Trigger.Props>) {
  return (
    <SelectPrimitive.Trigger
      data-slot="select-trigger"
      className={cn(
        formFieldBase,
        formFieldSingleLine,
        "items-center justify-between gap-2 text-left",
        "[&>span]:line-clamp-1",
        className
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon
        render={
          <NavArrowDownIcon aria-hidden="true" className="h-4 w-4 shrink-0 text-muted-foreground opacity-70" />
        }
      />
    </SelectPrimitive.Trigger>
  )
}
SelectTrigger.displayName = "SelectTrigger"

function SelectScrollUpButton({
  className,
  ...props
}: WithClassName<SelectPrimitive.ScrollUpArrow.Props>) {
  return (
    <SelectPrimitive.ScrollUpArrow
      className={cn("top-0 flex w-full cursor-default items-center justify-center py-1", className)}
      {...props}
    >
      <NavArrowDownIcon aria-hidden="true" className="h-4 w-4 rotate-180" />
    </SelectPrimitive.ScrollUpArrow>
  )
}
SelectScrollUpButton.displayName = "SelectScrollUpButton"

function SelectScrollDownButton({
  className,
  ...props
}: WithClassName<SelectPrimitive.ScrollDownArrow.Props>) {
  return (
    <SelectPrimitive.ScrollDownArrow
      className={cn("bottom-0 flex w-full cursor-default items-center justify-center py-1", className)}
      {...props}
    >
      <NavArrowDownIcon aria-hidden="true" className="h-4 w-4" />
    </SelectPrimitive.ScrollDownArrow>
  )
}
SelectScrollDownButton.displayName = "SelectScrollDownButton"

function SelectContent({
  className,
  children,
  style,
  side,
  sideOffset,
  align,
  alignOffset,
  alignItemWithTrigger = false,
  container,
  ...props
}: WithClassName<SelectPrimitive.Popup.Props> &
  Pick<
    SelectPrimitive.Positioner.Props,
    "side" | "sideOffset" | "align" | "alignOffset" | "alignItemWithTrigger"
  > & { style?: React.CSSProperties; container?: SelectPrimitive.Portal.Props["container"] }) {
  return (
    <SelectPrimitive.Portal container={container}>
      <SelectPrimitive.Positioner
        className="isolate z-50 outline-none"
        positionMethod="fixed"
        side={side}
        sideOffset={sideOffset}
        align={align}
        alignOffset={alignOffset}
        alignItemWithTrigger={alignItemWithTrigger}
      >
        <SelectPrimitive.Popup
          data-slot="select-popup"
          style={{ ...cashmereGrainStyle, ...style }}
          className={cn(
            "relative z-50 max-h-96 min-w-[8rem] overflow-hidden",
            "rounded-xl border border-border",
            "bg-popover text-popover-foreground",
            "[box-shadow:var(--shadow-l)]",
            "[transform-origin:var(--transform-origin)] transition-[transform,opacity] duration-150",
            "data-starting-style:opacity-0 data-starting-style:scale-95",
            "data-ending-style:opacity-0 data-ending-style:scale-95",
            !alignItemWithTrigger &&
              "data-[side=bottom]:translate-y-1 data-[side=left]:-translate-x-1 data-[side=right]:translate-x-1 data-[side=top]:-translate-y-1",
            className
          )}
          {...props}
        >
          <SelectScrollUpButton />
          <SelectPrimitive.List
            className={cn(
              "p-1",
              !alignItemWithTrigger &&
                "max-h-[var(--available-height)] w-full min-w-[var(--anchor-width)] overflow-y-auto"
            )}
          >
            {children}
          </SelectPrimitive.List>
          <SelectScrollDownButton />
        </SelectPrimitive.Popup>
      </SelectPrimitive.Positioner>
    </SelectPrimitive.Portal>
  )
}
SelectContent.displayName = "SelectContent"

function SelectLabel({
  className,
  ...props
}: WithClassName<SelectPrimitive.GroupLabel.Props>) {
  return (
    <SelectPrimitive.GroupLabel
      className={cn(
        "px-2 py-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground",
        className
      )}
      {...props}
    />
  )
}
SelectLabel.displayName = "SelectLabel"

function SelectItem({
  className,
  children,
  ...props
}: WithClassName<SelectPrimitive.Item.Props>) {
  return (
    <SelectPrimitive.Item
      className={cn(
        "relative flex w-full cursor-default select-none items-center rounded-md py-1.5 pl-8 pr-2 text-sm outline-none",
        "transition-[background-color,color] duration-150",
        "data-highlighted:bg-accent data-highlighted:text-accent-foreground",
        "data-disabled:pointer-events-none data-disabled:opacity-50",
        className
      )}
      {...props}
    >
      <span className="absolute left-2 flex h-3.5 w-3.5 items-center justify-center">
        <SelectPrimitive.ItemIndicator>
          <CheckIcon aria-hidden="true" className="h-4 w-4 text-primary" />
        </SelectPrimitive.ItemIndicator>
      </span>
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    </SelectPrimitive.Item>
  )
}
SelectItem.displayName = "SelectItem"

function SelectSeparator({
  className,
  ...props
}: WithClassName<SelectPrimitive.Separator.Props>) {
  return (
    <SelectPrimitive.Separator
      className={cn("-mx-1 my-1 h-px bg-border", className)}
      {...props}
    />
  )
}
SelectSeparator.displayName = "SelectSeparator"

interface FormSelectOption {
  value: string
  label: React.ReactNode
  disabled?: boolean
}

interface FormSelectProps {
  id?: string
  value: string
  options: readonly FormSelectOption[]
  onValueChange?: (value: string) => void
  disabled?: boolean
  className?: string
  "aria-label"?: string
  "aria-describedby"?: string
  "aria-invalid"?: React.AriaAttributes["aria-invalid"]
}

function FormSelect({ value, options, onValueChange, disabled, ...triggerProps }: FormSelectProps) {
  return (
    <Select
      value={value}
      items={options}
      disabled={disabled}
      onValueChange={next => { if (next !== null) onValueChange?.(next) }}
    >
      <SelectTrigger {...triggerProps}><SelectValue /></SelectTrigger>
      <SelectContent>
        {options.map(option => (
          <SelectItem key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

export {
  FormSelect,
  Select,
  SelectGroup,
  SelectValue,
  SelectTrigger,
  SelectContent,
  SelectLabel,
  SelectItem,
  SelectSeparator,
  SelectScrollUpButton,
  SelectScrollDownButton,
}
