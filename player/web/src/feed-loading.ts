/** Bounded deduplicating refill. A shuffle-cycle rollover must not spin forever. */
export async function fillUniqueFeed<T extends { id: string }>(options: {
  items: T[]; seen: Set<string>; target: number; maxItems: number; maxRequests?: number;
  fetchBatch: () => Promise<T[]>; onAdd?: (item: T) => void;
}): Promise<{ reason: "satisfied" | "empty" | "no-new-items" | "request-limit"; requests: number }> {
  const { items, seen, maxItems, fetchBatch, onAdd } = options;
  const target = Math.min(options.target, maxItems);
  const budget = options.maxRequests ?? 5;
  let requests = 0;
  while (items.length < target && requests < budget) {
    requests++;
    const batch = await fetchBatch();
    if (!batch.length) return { reason: "empty", requests };
    const before = items.length;
    for (const item of batch) {
      if (items.length >= maxItems) break;
      if (seen.has(item.id)) continue;
      seen.add(item.id);
      items.push(item);
      onAdd?.(item);
    }
    if (items.length === before) return { reason: "no-new-items", requests };
  }
  return { reason: items.length >= target ? "satisfied" : "request-limit", requests };
}
