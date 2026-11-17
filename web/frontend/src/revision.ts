/** UI revision marker, not an authentication token or a write approval.
 * getRandomValues is available on LAN HTTP; randomUUID requires a secure context.
 */
export function createRevision(source: Pick<Crypto, 'getRandomValues'> = globalThis.crypto): string {
  if (!source || typeof source.getRandomValues !== 'function') {
    throw new Error('浏览器不支持安全随机数，请使用支持 Web Crypto 的浏览器')
  }
  const bytes = source.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
