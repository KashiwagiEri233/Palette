"use client"

import * as React from "react"

import { cn } from "@/lib/utils"

/*
 * Simple sidebar primitive — a vertical navigation column with header/footer/item slots.
 * Elevation: sibling of main content at --card level, with inset separators.
 */

interface SidebarProps extends React.HTMLAttributes<HTMLElement> {
  collapsible?: boolean
  variant?: "default" | "inset" | "rail"
}

const Sidebar = React.forwardRef<HTMLElement, SidebarProps>(
  ({ className, variant = "default", ...props }, ref) => (
    <aside
      ref={ref}
      data-slot="sidebar"
      data-variant={variant}
      className={cn(
        "flex h-full flex-col text-card-foreground",
        variant === "default" && "w-64 bg-card border-r border-border [box-shadow:var(--shadow-s)]",
        variant === "inset" && "w-full bg-card",
        variant === "rail" && "w-full items-center bg-background py-2 px-1",
        className
      )}
      {...props}
    />
  )
)
Sidebar.displayName = "Sidebar"

interface SidebarHeaderProps extends React.HTMLAttributes<HTMLDivElement> {
  separator?: boolean
}

const SidebarHeader = React.forwardRef<HTMLDivElement, SidebarHeaderProps>(
  ({ className, separator = true, ...props }, ref) => (
    <div
      ref={ref}
      className={cn("flex items-center gap-2 px-4 py-3", separator && "border-b border-border", className)}
      {...props}
    />
  )
)
SidebarHeader.displayName = "SidebarHeader"

const SidebarContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("flex-1 overflow-y-auto p-2", className)} {...props} />
  )
)
SidebarContent.displayName = "SidebarContent"

const SidebarFooter = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("px-4 py-3 border-t border-border", className)} {...props} />
  )
)
SidebarFooter.displayName = "SidebarFooter"

const SidebarGroup = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("flex flex-col gap-0.5 py-2", className)} {...props} />
  )
)
SidebarGroup.displayName = "SidebarGroup"

const SidebarActionRow = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      data-slot="sidebar-action-row"
      className={cn(
        "group/sidebar-row relative flex min-w-0 rounded-lg transition-colors duration-150",
        "hover:bg-accent focus-within:bg-accent has-[[aria-haspopup=menu][aria-expanded=true]]:bg-accent",
        "[&>button]:min-w-0 [&>button]:w-full [&>button]:justify-start [&>button]:gap-1.5 [&>button]:px-2 [&>button]:py-1",
        "[&>button]:rounded-[inherit] [&>button]:bg-transparent [&>button:hover]:bg-transparent [&>button]:active:scale-100",
        "hover:[&>button]:pr-16 focus-within:[&>button]:pr-16 has-[[aria-haspopup=menu][aria-expanded=true]]:[&>button]:pr-16",
        "hover:[&_[data-slot=sidebar-row-status]]:invisible focus-within:[&_[data-slot=sidebar-row-status]]:invisible has-[[aria-haspopup=menu][aria-expanded=true]]:[&_[data-slot=sidebar-row-status]]:invisible",
        className
      )}
      {...props}
    />
  )
)
SidebarActionRow.displayName = "SidebarActionRow"

const SidebarRowActions = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      data-slot="sidebar-row-actions"
      className={cn(
        "invisible absolute inset-y-0 right-1 flex items-center bg-transparent",
        "group-hover/sidebar-row:visible group-focus-within/sidebar-row:visible group-has-[[aria-haspopup=menu][aria-expanded=true]]/sidebar-row:visible",
        className
      )}
      {...props}
    />
  )
)
SidebarRowActions.displayName = "SidebarRowActions"

const SidebarGroupLabel = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        "px-2 pb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground",
        className
      )}
      {...props}
    />
  )
)
SidebarGroupLabel.displayName = "SidebarGroupLabel"

interface SidebarItemProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  active?: boolean
  icon?: React.ReactNode
  density?: "default" | "compact"
}

const SidebarItem = React.forwardRef<HTMLButtonElement, SidebarItemProps>(
  ({ className, active, icon, density = "default", children, ...props }, ref) => (
    <button
      ref={ref}
      type="button"
      data-slot="sidebar-item"
      className={cn(
        "flex items-center gap-2.5 w-full rounded-lg px-2 text-left",
        density === "compact" ? "min-h-8 py-1 text-[13px]" : "py-1.5 text-sm",
        "transition-[background-color,color,box-shadow] duration-150",
        "text-muted-foreground hover:bg-accent hover:text-foreground",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40",
        active &&
          "bg-accent text-foreground [box-shadow:var(--shadow-s)]",
        className
      )}
      {...props}
    >
      {icon && <span className="flex h-4 w-4 items-center justify-center shrink-0">{icon}</span>}
      <span className="flex-1 truncate">{children}</span>
    </button>
  )
)
SidebarItem.displayName = "SidebarItem"

export {
  Sidebar,
  SidebarHeader,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarActionRow,
  SidebarRowActions,
  SidebarGroupLabel,
  SidebarItem,
}
