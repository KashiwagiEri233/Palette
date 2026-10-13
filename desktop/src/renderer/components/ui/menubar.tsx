"use client"

import * as React from "react"
import { Menubar as MenubarPrimitive } from "@base-ui/react/menubar"
import { Menu as MenuPrimitive } from "@base-ui/react/menu"
import { Icon } from "@iconify/react"

import { cn } from "@/lib/utils"
import { cashmereGrainStyle } from "./_shared"

type WithClassName<P> = Omit<P, "className"> & { className?: string }

const MenubarMenu = MenuPrimitive.Root
const MenubarGroup = MenuPrimitive.Group
const MenubarPortal = MenuPrimitive.Portal
const MenubarSub = MenuPrimitive.SubmenuRoot
const MenubarRadioGroup = MenuPrimitive.RadioGroup

const contentStyles =
  "z-50 min-w-[12rem] overflow-hidden p-1 " +
  "rounded-xl border border-border " +
  "bg-popover text-popover-foreground " +
  "[box-shadow:var(--shadow-l)] " +
  "transition-[transform,translate,scale,opacity] duration-150 " +
  "data-starting-style:opacity-0 data-starting-style:scale-95 data-ending-style:opacity-0 data-ending-style:scale-95 data-[side=bottom]:data-starting-style:-translate-y-2 data-[side=left]:data-starting-style:translate-x-2 data-[side=right]:data-starting-style:-translate-x-2 data-[side=top]:data-starting-style:translate-y-2"

const itemStyles =
  "relative flex cursor-default select-none items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none " +
  "transition-[background-color,color] duration-150 " +
  "focus:bg-accent focus:text-accent-foreground " +
  "data-[disabled]:pointer-events-none data-[disabled]:opacity-50"

const Menubar = React.forwardRef<HTMLDivElement, WithClassName<MenubarPrimitive.Props>>(
  ({ className, ...props }, ref) => (
    <MenubarPrimitive
      ref={ref}
      className={cn(
        "flex h-10 items-center gap-1 rounded-xl p-1",
        "bg-card text-card-foreground border border-border",
        "[box-shadow:var(--shadow-s)]",
        className
      )}
      {...props}
    />
  )
)
Menubar.displayName = "Menubar"

const MenubarTrigger = React.forwardRef<
  HTMLButtonElement,
  WithClassName<MenuPrimitive.Trigger.Props>
>(({ className, ...props }, ref) => (
  <MenuPrimitive.Trigger
    ref={ref}
    className={cn(
      "flex cursor-default select-none items-center rounded-md px-3 py-1.5 text-sm font-medium outline-none",
      "transition-[background-color,color] duration-150",
      "focus:bg-accent focus:text-accent-foreground",
      "data-popup-open:bg-accent data-popup-open:text-accent-foreground",
      className
    )}
    {...props}
  />
))
MenubarTrigger.displayName = "MenubarTrigger"

const MenubarSubTrigger = React.forwardRef<
  HTMLDivElement,
  WithClassName<MenuPrimitive.SubmenuTrigger.Props> & { inset?: boolean }
>(({ className, inset, children, ...props }, ref) => (
  <MenuPrimitive.SubmenuTrigger
    ref={ref}
    className={cn(
      itemStyles,
      "data-popup-open:bg-accent data-popup-open:text-accent-foreground",
      inset && "pl-8",
      className
    )}
    {...props}
  >
    {children}
    <Icon icon="ph:caret-right" className="ml-auto h-4 w-4" />
  </MenuPrimitive.SubmenuTrigger>
))
MenubarSubTrigger.displayName = "MenubarSubTrigger"

const MenubarSubContent = React.forwardRef<
  HTMLDivElement,
  WithClassName<Omit<MenuPrimitive.Popup.Props, "style">> &
    Pick<
      MenuPrimitive.Positioner.Props,
      "align" | "alignOffset" | "side" | "sideOffset"
    > & { style?: React.CSSProperties }
>(
  (
    {
      className,
      align = "start",
      alignOffset = -3,
      side = "right",
      sideOffset = 0,
      style,
      ...props
    },
    ref
  ) => (
    <MenuPrimitive.Portal>
      <MenuPrimitive.Positioner
        className="isolate z-50 outline-none"
        align={align}
        alignOffset={alignOffset}
        side={side}
        sideOffset={sideOffset}
      >
        <MenuPrimitive.Popup
          ref={ref}
          className={cn(contentStyles, className)}
          style={{ ...cashmereGrainStyle, ...style }}
          {...props}
        />
      </MenuPrimitive.Positioner>
    </MenuPrimitive.Portal>
  )
)
MenubarSubContent.displayName = "MenubarSubContent"

const MenubarContent = React.forwardRef<
  HTMLDivElement,
  WithClassName<Omit<MenuPrimitive.Popup.Props, "style">> &
    Pick<
      MenuPrimitive.Positioner.Props,
      "align" | "alignOffset" | "side" | "sideOffset"
    > & { style?: React.CSSProperties }
>(
  (
    { className, align = "start", alignOffset = -4, side, sideOffset = 8, style, ...props },
    ref
  ) => (
    <MenuPrimitive.Portal>
      <MenuPrimitive.Positioner
        className="isolate z-50 outline-none"
        align={align}
        alignOffset={alignOffset}
        side={side}
        sideOffset={sideOffset}
      >
        <MenuPrimitive.Popup
          ref={ref}
          className={cn(contentStyles, className)}
          style={{ ...cashmereGrainStyle, ...style }}
          {...props}
        />
      </MenuPrimitive.Positioner>
    </MenuPrimitive.Portal>
  )
)
MenubarContent.displayName = "MenubarContent"

const MenubarItem = React.forwardRef<
  HTMLDivElement,
  WithClassName<MenuPrimitive.Item.Props> & { inset?: boolean }
>(({ className, inset, ...props }, ref) => (
  <MenuPrimitive.Item
    ref={ref}
    className={cn(itemStyles, inset && "pl-8", className)}
    {...props}
  />
))
MenubarItem.displayName = "MenubarItem"

const MenubarCheckboxItem = React.forwardRef<
  HTMLDivElement,
  WithClassName<MenuPrimitive.CheckboxItem.Props>
>(({ className, children, checked, ...props }, ref) => (
  <MenuPrimitive.CheckboxItem
    ref={ref}
    className={cn(itemStyles, "pl-8", className)}
    checked={checked}
    {...props}
  >
    <span className="absolute left-2 flex h-3.5 w-3.5 items-center justify-center">
      <MenuPrimitive.CheckboxItemIndicator>
        <Icon icon="ph:check-bold" className="h-4 w-4 text-primary" />
      </MenuPrimitive.CheckboxItemIndicator>
    </span>
    {children}
  </MenuPrimitive.CheckboxItem>
))
MenubarCheckboxItem.displayName = "MenubarCheckboxItem"

const MenubarRadioItem = React.forwardRef<
  HTMLDivElement,
  WithClassName<MenuPrimitive.RadioItem.Props>
>(({ className, children, ...props }, ref) => (
  <MenuPrimitive.RadioItem
    ref={ref}
    className={cn(itemStyles, "pl-8", className)}
    {...props}
  >
    <span className="absolute left-2 flex h-3.5 w-3.5 items-center justify-center">
      <MenuPrimitive.RadioItemIndicator>
        <span className="h-2 w-2 rounded-full bg-primary [box-shadow:var(--shadow-primary)]" />
      </MenuPrimitive.RadioItemIndicator>
    </span>
    {children}
  </MenuPrimitive.RadioItem>
))
MenubarRadioItem.displayName = "MenubarRadioItem"

const MenubarLabel = React.forwardRef<
  HTMLDivElement,
  WithClassName<MenuPrimitive.GroupLabel.Props> & { inset?: boolean }
>(({ className, inset, ...props }, ref) => (
  <MenuPrimitive.GroupLabel
    ref={ref}
    className={cn(
      "px-2 py-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground",
      inset && "pl-8",
      className
    )}
    {...props}
  />
))
MenubarLabel.displayName = "MenubarLabel"

const MenubarSeparator = React.forwardRef<
  HTMLDivElement,
  WithClassName<MenuPrimitive.Separator.Props>
>(({ className, ...props }, ref) => (
  <MenuPrimitive.Separator
    ref={ref}
    className={cn("-mx-1 my-1 h-px bg-border", className)}
    {...props}
  />
))
MenubarSeparator.displayName = "MenubarSeparator"

const MenubarShortcut = ({ className, ...props }: React.HTMLAttributes<HTMLSpanElement>) => (
  <span className={cn("ml-auto text-xs tracking-widest text-muted-foreground", className)} {...props} />
)
MenubarShortcut.displayName = "MenubarShortcut"

export {
  Menubar,
  MenubarMenu,
  MenubarTrigger,
  MenubarContent,
  MenubarItem,
  MenubarSeparator,
  MenubarLabel,
  MenubarCheckboxItem,
  MenubarRadioGroup,
  MenubarRadioItem,
  MenubarPortal,
  MenubarSubContent,
  MenubarSubTrigger,
  MenubarGroup,
  MenubarSub,
  MenubarShortcut,
}
