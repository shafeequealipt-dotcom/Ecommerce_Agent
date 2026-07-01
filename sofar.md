# EcomListing Pro — Progress So Far

## Overview

An AI-powered e-commerce product listing agent that generates complete, platform-compliant product listings (Amazon.in, Flipkart, Meesho) from a single product photo — including 4 AI-generated images.

---

## Files

### `listing_agent.py` — Core agent
- **Phase 1:** Analyzes product image via OpenRouter vision models → generates listing JSON (50+ fields)
- **Phase 2:** Generates 4 product images (lifestyle, before/after, how-to-use, hero) via fallback chain
- **Phase 3:** Saves listing + images + platform-specific text files to `products/<product-slug>/`
- Listing models: `google/gemma-4-31b-it:free` → `google/gemma-4-26b-a4b-it:free` → `nvidia/nemotron-nano-12b-v2-vl:free`
- Accepts `--hint` for product guidance and `--image-model` to choose image source
- JSON parser includes fallback that strips trailing text after the JSON object

### `telegram_bot.py` — Telegram interface
- Receives product photo + optional caption as a product hint
- Runs full listing pipeline (analyze → generate images → save)
- Sends back: listing JSON, platform-specific files, and all 4 images
- Commands: `/start`, `/help`, `/model` (switch image source)
- Image models: gemini (default), pollinations, nvidia, deapi
- Handles large outputs and errors gracefully

### `needkart_client.py` — NeedKart store integration (ready, not active)
- Admin JWT auth via `POST /auth/user/emailpass`
- Fetches prereqs: sales channels, shipping profiles, categories, stock locations
- Uploads images via `POST /admin/uploads`
- Creates product via `POST /admin/products` (title, description, price, MRP, SKU, images, category)
- Sets inventory at warehouse via inventory-items API
- `publish_listing()` runs the full pipeline: login → upload → create → set stock
- **Status:** Credentials pending (staging password not confirmed) — currently skipped

### `ecommerce.md` — Agent specification
- Complete spec for EcomListing Pro: product analysis, 50 listing fields, 4 image prompts
- Platform-specific compliance rules (Amazon, Flipkart, Meesho)
- JSON output structure, image QC checklist, copywriting templates

---

## Environment Variables

| Variable | Status |
|---|---|
| `OPENROUTER_API_KEY` / `OPENAI_API_KEY` | ✅ Set |
| `TELEGRAM_BOT_TOKEN` | ✅ Set (bot @EcomListingProBot) |
| `NVIDIA_API_KEY` | ✅ Set (slow/timeout) |
| `DE_API_KEY` | ✅ Set (rate-limited free tier) |
| `NEEDKART_API_BASE` | ✅ Set |
| `ADMIN_EMAIL` | ✅ Set (shafeequealipt@gmail.com) |
| `ADMIN_PASSWORD` | ✅ Set (via NeedKart_Api_Password) |
| `NeedKart_Api_Password` | ✅ Set (`Shaf@20203`) |
| `NeedKart_Api_Username` | ✅ Set |
| `NeedKart_Api_Baseurl` | ✅ Set |
| `Gemini_API_KEY1` | ✅ Set (quota exhausted) |

## Bot Flow

```
User sends photo + caption
  ↓
Phase 1: Analyze via OpenRouter (gemma-4) → listing JSON
  ↓
Phase 2: Generate 4 images via Gemini/Pollinations/NVIDIA
  ↓
Phase 3: Save to products/<slug>/ + send to Telegram
  ↓
Phase 4: Publish to NeedKart (login → upload images → create product → set stock)
```

## Pending

- [ ] Reduce retry delays on rate-limited models
- [ ] Switch default image model to Pollinations if Gemini credit runs out
