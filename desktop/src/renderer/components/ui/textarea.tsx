import * as React from "react"

import { cn } from "@/lib/utils"
import { formFieldBase, formFieldMultiLine } from "./_shared"

export interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  variant?: "default" | "composer"
}

const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, variant = "default", ...props }, ref) => (
    <textarea
      ref={ref}
      data-slot="textarea"
      data-variant={variant}
      className={cn(variant === "default" ? [formFieldBase, formFieldMultiLine] : "block w-full min-h-20 max-h-48 resize-none bg-transparent px-4 pt-4 pb-2 text-sm leading-relaxed text-foreground placeholder:text-muted-foreground disabled:opacity-50 focus-visible:outline-none", className)}
      {...props}
    />
  )
)
Textarea.displayName = "Textarea"

export { Textarea }
