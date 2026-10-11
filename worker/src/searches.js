// The scanner's search list (config.yaml, sent by each run) with the Telegram settings applied.

export const RULES = {   // name: [type, min, max] for /setrule
  min_profit: ["float", 0, 10_000],
  min_roi: ["float", 0, 1_000],
  min_rating: ["int", 1, 10],
  max_roi: ["float", 1, 10_000],
  max_price: ["float", 1, 10_000],   // main deals: the most we pay all in (budget deals use /budget)
};

// designer clothes' own minimums (config.yaml clothes.rules; /setrule clothes <name> <value>)
export const CLOTHES_DEFAULTS = { min_profit: 30, min_roi: 40, min_rating: 5, max_price: 200 };
export const CLOTHES_RULES = ["min_profit", "min_roi", "min_rating", "max_price"];

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
  const extras = (s, a) => {   // the app's platforms, must-have words, words to exclude, size
    if (!a) return;
    if (Array.isArray(a.platforms)) s.platforms = a.platforms;
    s.must = a.must || [];
    s.exclude = a.exclude || [];
    s.size = a.size || "";
  };
  for (const a of settings.added || []) {
    if (existing.has(a.query.toLowerCase())) continue;
    const clothes = a.clothes || a.group === "Clothes";
    const s = clothes
      ? { query: a.query, price_from: a.price_from ?? 20, price_to: a.price_to ?? null, budget: false, group: "Clothes",
        clothes: true, added: true, price_to_is_budget: false }
      : { query: a.query, price_from: a.price_from ?? null, price_to: a.price_to ?? null, budget: a.group === "Budget",
        group: a.group || "Added from Telegram", added: true, price_to_is_budget: false };
    extras(s, a);
    searches.push(s);
    existing.add(a.query.toLowerCase());
  }
  const overrides = Object.fromEntries(Object.entries(settings.overrides || {}).map(([k, v]) => [k.toLowerCase(), v]));
  for (const s of searches) if (!s.added) extras(s, overrides[s.query.toLowerCase()]);
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
  const rules = { min_profit: 25, min_roi: 30, min_rating: 5, max_roi: 120, max_price: 200, ...(catalog.rules || {}) };
  for (const [name, value] of Object.entries(settings.rules || {})) {
    if (name in RULES) rules[name] = value;
  }
  const view = {
    searches, rules,
    budget_rules: { min_profit: 12, min_roi: 35, max_roi: 150, ...(catalog.budget_rules || {}), ...(settings.budget_rules || {}) },
    music_rules: { min_profit: 20, min_roi: 35, ...(catalog.music_rules || {}), ...(settings.music_rules || {}) },
    clothes_rules: { ...CLOTHES_DEFAULTS, ...(catalog.clothes_rules || {}), ...(settings.clothes_rules || {}) },
    budget_setting: settings.budget ?? catalog.budget ?? 72,
    ebay: Boolean(catalog.ebay), subito: Boolean(catalog.subito),
  };
  setBudget(view, view.budget_setting);
  setBudget(view, effectiveBudget(view.budget_setting, pool));
  return view;
}
