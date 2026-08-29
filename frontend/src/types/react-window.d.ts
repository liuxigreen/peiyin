declare module 'react-window' {
  import { Component, CSSProperties } from 'react'

  export interface ListOnScrollProps {
    scrollOffset: number
    scrollUpdateWasRequested: boolean
  }

  export interface FixedSizeListProps {
    height: number
    width: number | string
    itemCount: number
    itemSize: number
    overscanCount?: number
    className?: string
    style?: CSSProperties
    children: (props: { index: number; style: CSSProperties }) => JSX.Element | null
    onScroll?: (props: ListOnScrollProps) => void
  }

  export class FixedSizeList extends Component<FixedSizeListProps> {
    scrollTo(scrollOffset: number): void
    scrollToItem(index: number, align?: 'auto' | 'start' | 'end' | 'center'): void
  }
}
