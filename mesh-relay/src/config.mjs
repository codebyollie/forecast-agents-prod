const positiveNumber = (value, fallback) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
};

const normalizeUrl = (value) => {
  const url = new URL(value);
  if (url.protocol !== "https:") throw new Error("Merchant endpoints must use HTTPS.");
  return url.toString();
};

function parseMerchants(raw) {
  if (!raw) return {};

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("MESH_MERCHANTS_JSON must be valid JSON.");
  }

  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("MESH_MERCHANTS_JSON must be an object keyed by merchant id.");
  }

  return Object.fromEntries(
    Object.entries(parsed).map(([id, merchant]) => {
      if (!/^[a-z0-9_-]{1,64}$/i.test(id)) {
        throw new Error(`Invalid merchant id: ${id}`);
      }
      if (!merchant || typeof merchant !== "object" || typeof merchant.endpoint !== "string") {
        throw new Error(`Merchant ${id} must include an endpoint.`);
      }

      const maxAmount = positiveNumber(merchant.maxAmount, undefined);
      return [id, {
        endpoint: normalizeUrl(merchant.endpoint),
        maxAmount,
        label: typeof merchant.label === "string" ? merchant.label.slice(0, 120) : id,
      }];
    }),
  );
}

export function loadConfig(env = process.env) {
  const enabled = env.MESH_ENABLED === "true";
  const config = {
    enabled,
    apiKey: env.MESH_RELAY_API_KEY || "",
    buyerPrivateKey: env.MESH_BUYER_PRIVATE_KEY || "",
    maxAmountPerRequest: positiveNumber(env.MESH_MAX_AMOUNT_PER_REQUEST, 0.05),
    dailyBudget: positiveNumber(env.MESH_DAILY_BUDGET_USDG, 1),
    merchants: parseMerchants(env.MESH_MERCHANTS_JSON || ""),
  };

  if (enabled && !config.apiKey) throw new Error("MESH_RELAY_API_KEY is required when Mesh is enabled.");
  if (enabled && !config.buyerPrivateKey) throw new Error("MESH_BUYER_PRIVATE_KEY is required when Mesh is enabled.");

  for (const merchant of Object.values(config.merchants)) {
    if (merchant.maxAmount && merchant.maxAmount > config.maxAmountPerRequest) {
      throw new Error("A merchant maxAmount cannot exceed MESH_MAX_AMOUNT_PER_REQUEST.");
    }
  }

  return config;
}

