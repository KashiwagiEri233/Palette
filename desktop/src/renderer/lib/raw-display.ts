export interface RawDisplayTicket { url: string; provider?: 'rfb' | 'raw'; generation?: string; instance_id?: string }
export interface RawFrame {
  kind: 'full' | 'rect' | 'cursor'
  instance_id: string; generation: string; seq: number; width: number; height: number
  rect?: [number, number, number, number]
  cursor?: { width?: number; height?: number; hot_x?: number; hot_y?: number; rgba?: string; visible?: boolean }
  pixels: Uint8ClampedArray<ArrayBuffer>
}

/** Decode complete or incremental RGBA frames. Source identity comes from the authenticated ticket. */
export function decodeRawFrame(buffer: ArrayBuffer, ticket: RawDisplayTicket): RawFrame {
  if (buffer.byteLength < 5 || buffer.byteLength > 8192 * 4320 * 4 + 131076) throw new Error('原始画面长度无效')
  const size = new DataView(buffer).getUint32(0)
  if (!size || size > 131072 || size > buffer.byteLength - 4) throw new Error('原始画面头部无效')
  const header = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(buffer, 4, size)))
  if (header.version !== 1 || header.format !== 'rgba' || header.instance_id !== ticket.instance_id || header.generation !== ticket.generation ||
    !Number.isSafeInteger(header.seq) || header.seq <= 0 || !Number.isInteger(header.width) || !Number.isInteger(header.height) ||
    header.width < 1 || header.height < 1 || header.width > 8192 || header.height > 4320) throw new Error('原始画面身份或尺寸无效')
  const kind = header.kind ?? 'full'
  const payloadBytes = buffer.byteLength - 4 - size
  if (kind === 'full') {
    if (payloadBytes !== header.width * header.height * 4) throw new Error('原始画面身份或尺寸无效')
  } else if (kind === 'cursor') {
    if (payloadBytes !== 0) throw new Error('原始画面身份或尺寸无效')
  } else if (kind === 'rect') {
    const rect = header.rect
    if (!Array.isArray(rect) || rect.length !== 4 || !rect.every((n: unknown) => Number.isInteger(n))) throw new Error('原始画面脏区无效')
    const [x, y, rw, rh] = rect as [number, number, number, number]
    if (x < 0 || y < 0 || rw < 1 || rh < 1 || x + rw > header.width || y + rh > header.height || payloadBytes !== rw * rh * 4) {
      throw new Error('原始画面脏区无效')
    }
  } else {
    throw new Error('原始画面类型无效')
  }
  const cursor = header.cursor
  if (cursor?.rgba && (!Number.isInteger(cursor.width) || !Number.isInteger(cursor.height) || cursor.width < 1 || cursor.height < 1 ||
    cursor.width > 128 || cursor.height > 128 || !Number.isInteger(cursor.hot_x) || !Number.isInteger(cursor.hot_y) ||
    cursor.hot_x < 0 || cursor.hot_y < 0 || cursor.hot_x >= cursor.width || cursor.hot_y >= cursor.height ||
    typeof cursor.rgba !== 'string' || cursor.rgba.length > 87384)) throw new Error('原始画面光标无效')
  return { ...header, kind, pixels: new Uint8ClampedArray(buffer, 4 + size) }
}
