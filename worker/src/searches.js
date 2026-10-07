// The scanner's search list (config.yaml, sent by each run) with the Telegram settings applied.

export const RULES = {   // name: [type, min, max] for /setrule
  min_profit: ["float", 0, 10_000],
  min_roi: ["float", 0, 1_000],
  min_rating: ["int", 1, 10],
  max_roi: ["float", 1, 10_000],
};

/** Short stable id for button data (Telegram allows 64 bytes). */
export async function searchId(query) {
  const hash = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(query.toLowerCase()));
  return [...new Uint8Array(hash)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 10);
}

/** Budget-mode limit: the /budget value, but never more than what's in the pool. */
export function effectiveBudget(setting, pool) {
  return pool === null || pool === undefined ? setting : Math.min(setting, pool);
}

/**
 * Budget searches look up to the budget, in both directions, unless their max price was set
 * by hand; they never look above it.
 */
export function setBudget(view, amount) {
  view.budget = Math.max(0, amount);
  for (const s of view.searches) {
    if (s.budget && (s.price_to_is_budget || s.price_to === null || s.price_to > view.budget)) {
      s.price_to_is_budget = s.price_to_is_budget || s.price_to === null;
      s.price_to = view.budget;
    }
  }
}

/** settings over the catalog: removed/added searches, on/off, prices, budget (capped by the pool), rules. */
export function applySettings(catalog, settings, pool = null) {
  const removed = new Set((settings.removed || []).map((q) => q.toLowerCase()));
  const searches = (catalog.searches || []).filter((s) => !removed.has(s.query.toLowerCase())).map((s) => ({ ...s }));
  const existing = new Set(searches.map((s) => s.query.toLowerCase()));
  for (const a of settings.added || []) {
    if (!existing.has(a.query.toLowerCase())) {
      searches.push({ query: a.query, price_from: a.price_from ?? null, price_to: a.price_to ?? null,
        budget: false, group: "Added from Telegram", added: true, price_to_is_budget: false });
    }
  }
  const disabled = new Set((settings.disabled || []).map((q) => q.toLowerCase()));
  const prices = Object.fromEntries(Object.entries(settings.prices || {}).map(([k, v]) => [k.toLowerCase(), v]));
  for (const s of searches) {
    s.enabled = !disabled.has(s.query.toLowerCase());
    const p = prices[s.query.toLowerCase()];
    if (p) {
      [s.price_from, s.price_to] = p;
      s.price_to_is_budget = false;   // set by hand: the budget only caps it
    }
  }
  const rules = { min_profit: 25, min_roi: 30, min_rating: 5, max_roi: 120, ...(catalog.rules || {}) };
  for (const [name, value] of Object.entries(settings.rules || {})) {
    if (name in RULES) rules[name] = value;
  }
  const view = {
    searches, rules,
    budget_rules: { min_profit: 12, min_roi: 35, max_roi: 150, ...(catalog.budget_rules || {}) },
    budget_setting: settings.budget ?? catalog.budget ?? 72,
    ebay: Boolean(catalog.ebay), subito: Boolean(catalog.subito),
  };
  setBudget(view, view.budget_setting);
  setBudget(view, effectiveBudget(view.budget_setting, pool));
  return view;
}
