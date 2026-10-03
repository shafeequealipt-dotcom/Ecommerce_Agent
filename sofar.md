# EcomListing Pro — Progress So Far

## Overview

An AI-powered e-commerce product listing agent that generates complete, platform-compliant product listings (Amazon.in, Flipkart, Meesho) from a single product photo — including 4 AI-generated images, and publishes to NeedKart store.

---

## Files

### `listing_agent.py` — Core agent
- **Phase 1:** Analyzes product image via OpenRouter vision models → generates listing JSON (50+ fields)
- **Phase 2:** Generates 4 product images (lifestyle, before/after, how-to-use, hero) via fallback chain
- **Phase 3:** Saves listing + images + platform-specific text files to `products/<product-slug>/`
- Listing models (all free): `nvidia/nemotron-nano-12b-v2-vl:free` (primary) → `google/gemma-4-26b-a4b-it:free` → `google/gemma-4-31b-it:free`
- Image fallback chain: deAPI.ai (paid) → NVIDIA (free) → Gemini via OpenRouter (paid) → Pollinations.ai (free)
- Accepts `--hint` for product guidance and `--image-model` to choose image source
- JSON parser includes fallback that strips trailing text after the JSON object

### `telegram_bot.py` — Telegram interface
- Receives product photo + optional caption as a product hint
- Runs full listing pipeline (analyze → generate images → save → publish to NeedKart)
- Sends back: listing JSON, platform-specific files, and all 4 images
- Commands: `/start`, `/help`, `/model` (switch image source)
- Image models: gemini (default), pollinations, nvidia, deapi
- Handles large outputs and errors gracefully

### `needkart_client.py` — NeedKart store integration
- Admin JWT auth via `POST /auth/user/emailpass`
- Fetches prereqs: sales channels, shipping profiles, categories, stock locations
- Uploads images via `POST /admin/uploads`
- **Auto-creates categories** if the AI-detected category doesn't exist on NeedKart
- Creates product via `POST /admin/products` (title, description, price, MRP, SKU, images, category)
- Sets inventory at warehouse via `/admin/inventory-items/{id}/location-levels`
- `publish_listing()` runs the full pipeline: login → upload → create → set stock
- Smart CATEGORY_MAP with word-boundary matching and longest-key-first priority

### `flipkart_client.py` — Flipkart Seller API v3 (NEW)
- Auth via OAuth client_credentials (App ID + App Secret → Bearer token)
- Check listing by SKU (`GET /sellers/skus/{sku}/listings`)
- Create listing (`POST /listings/v3` — batch max 10)
- Update price/stock (`POST /listings/v3/update/price`, `/update/inventory`)
- Full listing payload builder from agent's listing.json (price, tax, HSN, GST, packages, locations, address labels, dating labels)
- Bulk CSV generator as fallback when FSN unavailable

### `marketplace_publisher.py` — Multi-platform orchestrator (NEW)
- Dedup: checks if SKU already listed on each platform before publishing
- Publish to single platform or all configured platforms
- Human-readable summary of results per platform
- CSV generators for platforms without APIs (Flipkart bulk CSV, Meesho CSV)

### `ecommerce.md` — Agent specification
- Complete spec for EcomListing Pro: product analysis, 50 listing fields, 4 image prompts
- Platform-specific compliance rules (Amazon, Flipkart, Meesho)
- JSON output structure, image QC checklist, copywriting templates

---

## Environment Variables

| Variable | Status |
|---|---|
| `OPENROUTER_API_KEY` | ✅ Set (free models) |
| `TELEGRAM_BOT_TOKEN` | ✅ Set (bot @EcomListingProBot) |
| `NVIDIA_API_KEY` | ✅ Set (free tier, can time out) |
| `DE_API_KEY` | ✅ Set (rate-limited free tier) |
| `NeedKart_Api_Baseurl` | ✅ Set (`https://api.staging.needkart.store`) |
| `NeedKart_Api_Username` | ✅ Set (`shafeequealipt@gmail.com`) |
| `NeedKart_Api_Password` | ✅ Set (`Shaf@20203`) |
| `Gemini_API_KEY1` | ✅ Set (quota exhausted) |

### Needed for Flipkart
| Variable | Status |
|---|---|
| `FLIPKART_APP_ID` | ❌ Not yet provided |
| `FLIPKART_APP_SECRET` | ❌ Not yet provided |
| `FLIPKART_LOCATION_ID` | ❌ Not yet provided |

---

## Deployments

### Oracle Cloud VM (68.233.109.57) ✅
- Repo cloned to `/home/ubuntu/ecommerce-agent/`
- Python venv with `python-telegram-bot`
- Systemd service `ecom-bot.service` (auto-restart, enabled on boot)
- `.env` with all secrets

### GitHub ✅
- Repo: `shafeequealipt-dotcom/Ecommerce_Agent`
- Both `main` and `development` branches kept in sync

---

## Key Fixes & Improvements

- `category_ids` → `categories: [{id: "..."}]` (Zod schema compliance)
- Word-boundary CATEGORY_MAP matching (prevents "car" matching "care")
- Longest-key-first CATEGORY_MAP sorting (prevents "shoe" matching before "shoe care")
- Auto-create missing categories on NeedKart
- Handle slugify: collapse double dashes (`---` → `-`)
- Inventory ID: use `inventory_item_id` not link `id` (`pvitem_` → `iitem_`)
- Price/weight parsing: handle int values via `str()` wrapper + regex number extraction

## Bot Flow

```
User sends photo + caption
  ↓
Phase 1: Analyze via OpenRouter (nemotron-nano/gemma-4) → listing JSON
  ↓
Phase 2: Generate 4 images via deAPI → NVIDIA → Gemini → Pollinations
  ↓
Phase 3: Save to products/<slug>/ + send to Telegram
  ↓
Phase 4: Publish to NeedKart (login → upload images → create product → set stock)
         Auto-create category if needed
         Set inventory at India Warehouse
```

## Pending

- [ ] Get Flipkart API credentials (App ID, App Secret, Location ID)
- [ ] Test Flipkart listing creation end-to-end
- [ ] Amazon SP-API integration (future)
- [ ] Meesho automation (future — no API, CSV only)
- [ ] Add Telegram `/publish` command for marketplace publishing
