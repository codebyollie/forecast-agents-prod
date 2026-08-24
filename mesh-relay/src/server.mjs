import http from "node:http";
import { createClient, getSettlement } from "@meshgateway/mpp-client";
import { privateKeyToAccount } from "viem/accounts";
import { loadConfig } from "./config.mjs";
import { SpendGuard } from "./spend-guard.mjs";

const config = loadConfig();
const guard = new SpendGuard({ dailyBudget: config.dailyBudget });
const port = Number(process.env.PORT || 8080);

const paymentClient = config.enabled
  ? createClient({
      signer: privateKeyToAccount(config.buyerPrivateKey),
      maxAmount: String(config.maxAmountPerRequest),
    })
  : null;

function respond(res, status, body) {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  res.end(JSON.stringify(body));
}

function isAuthorized(request) {
  const header = request.headers.authorization || "";
  return config.apiKey && header === `Bearer ${config.apiKey}`;
}

async function readJson(request) {
  let raw = "";
  for await (const chunk of request) {
    raw += chunk;
    if (raw.length > 1_000_000) throw new Error("Request body too large.");
  }
  return raw ? JSON.parse(raw) : {};
}

function publicMerchantList() {
  return Object.entries(config.merchants).map(([id, merchant]) => ({ id, label: merchant.label }));
}

async function invoke(request, response) {
  if (!isAuthorized(request)) return respond(response, 401, { error: "Unauthorized" });
  if (!config.enabled || !paymentClient) return respond(response, 503, { error: "Mesh relay is not enabled." });

  let payload;
  try {
    payload = await readJson(request);
  } catch {
    return respond(response, 400, { error: "Invalid JSON body." });
  }

  const merchantId = typeof payload.merchant === "string" ? payload.merchant : "";
  const merchant = config.merchants[merchantId];
  if (!merchant) return respond(response, 400, { error: "Unknown or unapproved merchant." });

  // Intentionally no caller supplied URL, headers, or path. Each merchant URL is explicitly allowlisted.
  const reserve = merchant.maxAmount || config.maxAmountPerRequest;
  try {
    guard.reserve(reserve);
    const upstream = await paymentClient.fetch(merchant.endpoint, { method: "GET" });
    const settlement = getSettlement(upstream);
    const contentType = upstream.headers.get("content-type") || "";
    const data = contentType.includes("application/json") ? await upstream.json() : await upstream.text();

    return respond(response, upstream.ok ? 200 : upstream.status, {
      merchant: merchantId,
      data,
      receipt: settlement ? { transaction: settlement.transaction, network: settlement.network } : null,
    });
  } catch (error) {
    guard.release(reserve);
    console.error("[mesh-relay] merchant call failed", { merchant: merchantId, message: error?.message });
    return respond(response, error?.code === "daily_budget_exceeded" ? 429 : 502, {
      error: error?.code === "daily_budget_exceeded" ? "Daily Mesh spending limit reached." : "Mesh merchant request failed.",
    });
  }
}

http.createServer(async (request, response) => {
  const url = new URL(request.url || "/", `http://${request.headers.host || "localhost"}`);
  if (request.method === "GET" && url.pathname === "/healthz") {
    return respond(response, 200, { status: "ok", enabled: config.enabled, merchantsConfigured: Object.keys(config.merchants).length });
  }
  if (request.method === "GET" && url.pathname === "/v1/merchants") {
    if (!isAuthorized(request)) return respond(response, 401, { error: "Unauthorized" });
    return respond(response, 200, { enabled: config.enabled, merchants: publicMerchantList() });
  }
  if (request.method === "POST" && url.pathname === "/v1/invoke") return invoke(request, response);
  return respond(response, 404, { error: "Not found" });
}).listen(port, "0.0.0.0", () => {
  console.log(`[mesh-relay] listening on ${port}; enabled=${config.enabled}; merchants=${Object.keys(config.merchants).length}`);
});

