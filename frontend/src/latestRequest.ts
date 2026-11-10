/**
 * Only the newest request may publish. A slower earlier reply is dropped, not queued:
 * the viewer already shows the newer target, so accepting the old payload would pair
 * one name with another's content.
 */
export function createLatest() {
  let current = 0;
  return {
    next(): number {
      current += 1;
      return current;
    },
    isCurrent(seq: number): boolean {
      return seq === current;
    },
  };
}
