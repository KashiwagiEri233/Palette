import type { ComponentType, SVGProps } from 'react'
import * as Icons from './index'

type IconProps = SVGProps<SVGSVGElement> & { size?: number }

function sized(Glyph: ComponentType<SVGProps<SVGSVGElement>>) {
  return function WorkbenchIcon({ size = 16, ...props }: IconProps) {
    return <Glyph width={size} height={size} aria-hidden="true" {...props} />
  }
}

export const Home = sized(Icons.HomeIcon)
export const Monitor = sized(Icons.ComputerIcon)
export const Settings2 = sized(Icons.SettingsIcon)
export const Plus = sized(Icons.PlusIcon)
export const Search = sized(Icons.SearchIcon)
export const ArrowLeft = sized(Icons.ArrowLeftIcon)
export const ArrowRight = sized(Icons.ArrowRightIcon)
export const MessageSquare = sized(Icons.ChatBubbleEmptyIcon)
export const FolderInput = sized(Icons.ImportIcon)
export const Disc = sized(Icons.CompactDiscIcon)
export const Pin = sized(Icons.PinIcon)
export const ChevronDown = sized(Icons.NavArrowDownIcon)
export const ChevronRight = sized(Icons.NavArrowRightIcon)
export const MoreHorizontal = sized(Icons.MoreHorizIcon)
export const PanelRightClose = sized(Icons.SidebarCollapseIcon)
export const PanelRightOpen = sized(Icons.SidebarExpandIcon)
export const PanelLeft = sized(Icons.SidebarCollapseIcon)
export const Sparkles = sized(Icons.SparkIcon)
export const Menu = sized(Icons.MenuIcon)
export const Play = sized(Icons.PlayIcon)
export const Power = sized(Icons.SystemShutIcon)
export const RotateCw = sized(Icons.RefreshIcon)
export const RotateCcw = sized(Icons.RefreshIcon)
export const Pause = sized(Icons.PauseIcon)
export const Trash2 = sized(Icons.TrashIcon)
export const Square = sized(Icons.SquareIcon)
export const ArrowUp = sized(Icons.ArrowUpIcon)
export const AlertTriangle = sized(Icons.WarningCircleIcon)
export const MousePointer2 = sized(Icons.PcMouseIcon)
