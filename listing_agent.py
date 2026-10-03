#!/usr/bin/env python3
"""EcomListing Pro Agent — generates complete e-commerce listing + 4 product images from a single product photo."""

import json, os, base64, sys, re, time, uuid
import urllib.request, urllib.error, urllib.parse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_DIR = os.path.join(SCRIPT_DIR, "products")

def _load_env_key(env_var):
    key = os.environ.get(env_var)
    if key:
        return key
    zshrc = os.path.expanduser("~/.zshrc")
    if os.path.isfile(zshrc):
        pat = re.compile(r'^export\s+' + re.escape(env_var) + r'="?([^"\n]+)"?\s*$')
        with open(zshrc) as f:
            for line in f:
                m = pat.match(line)
                if m:
                    return m.group(1)
    return None

NVIDIA_API_KEY = _load_env_key("NVIDIA_API_KEY")
DE_API_KEY = _load_env_key("DE_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")

LISTING_MODELS = [
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "google/gemini-2.5-flash-lite-preview-09-2025",
    "qwen/qwen3-vl-8b-instruct",
    "meta-llama/llama-3.2-11b-vision-instruct",
]
GEMINI_IMAGE_MODEL = "google/gemini-3.1-flash-image"
POLLINATIONS_URL = "https://image.pollinations.ai/prompt"
DEAPI_MODEL = "Flux_2_Klein_4B_BF16"
DEAPI_BASE = "https://api.deapi.ai/api/v1/client"
DEAPI_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

if not OPENROUTER_API_KEY:
    print("ERROR: OPENROUTER_API_KEY or OPENAI_API_KEY not set in environment.", file=sys.stderr)
    sys.exit(1)


def slugify(text):
    s = text.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')[:60]


def encode_image(image_path):
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def mime_for(path):
    ext = os.path.splitext(path)[1].lower()
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")


def or_request(payload, timeout=180):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://github.com/ecomlisting-pro",
            "X-Title": "EcomListing Pro Agent",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            err_json = json.loads(body)
            msg = err_json.get("error", {}).get("message", body[:300])
        except json.JSONDecodeError:
            msg = body[:300]
        return {"_http_error": e.code, "_error_msg": msg}


def nvidia_request(payload, model, timeout=180):
    """Call NVIDIA build.nvidia.com API."""
    if not NVIDIA_API_KEY:
        return {"_http_error": 0, "_error_msg": "NVIDIA_API_KEY not set"}
    data = json.dumps(payload).encode()
    url = f"https://ai.api.nvidia.com/v1/genai/black-forest-labs/{model}"
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"_success": True, "data": json.loads(resp.read())}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {"_http_error": e.code, "_error_msg": body[:400]}


# ── deAPI.ai img2img (primary image source) ───────────────────────────────────

def _build_multipart(fields, file_field_name, file_data, filename):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    if file_data:
        ext = os.path.splitext(filename)[1].lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext, "image/jpeg")
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field_name}"; filename="{os.path.basename(filename)}"\r\n'
            f'Content-Type: {mime}\r\n\r\n'.encode()
        )
        parts.append(file_data)
        parts.append(b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return boundary, b"".join(parts)


def _deapi_poll(rid, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(2)
        req = urllib.request.Request(
            f"{DEAPI_BASE}/request-status/{rid}",
            headers={"Authorization": f"Bearer {DE_API_KEY}", "Accept": "application/json", "User-Agent": DEAPI_UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                d = json.loads(resp.read())
        except Exception:
            continue
        status = d.get("data", {}).get("status", "unknown")
        if status == "done":
            return d["data"].get("result_url")
        if status in ("failed", "error"):
            return None
    return None


def generate_via_deapi(image_key, user_prompt, ref_image_path, product_name, category):
    """Generate via deAPI.ai FLUX.2-klein-4b img2img — passes reference product photo for consistency."""
    prompt = premium_image_prompt(image_key, user_prompt, product_name)

    with open(ref_image_path, "rb") as f:
        img_data = f.read()

    seed = hash(image_key + str(time.time())) % 10000000
    fields = {
        "model": DEAPI_MODEL,
        "prompt": prompt,
        "steps": "4",
        "seed": str(seed),
        "width": "1024",
        "height": "1024",
    }
    boundary, body = _build_multipart(fields, "image", img_data, ref_image_path)

    # Retry with backoff for rate limits (429)
    for attempt in range(3):
        if attempt > 0:
            time.sleep(5 * attempt)
        req = urllib.request.Request(
            f"{DEAPI_BASE}/img2img",
            data=body,
            headers={
                "Authorization": f"Bearer {DE_API_KEY}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
                "User-Agent": DEAPI_UA,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
            break  # success
        except urllib.error.HTTPError as e:
            code = e.code
            body_err = e.read().decode()[:200]
            if code == 429:
                if attempt < 2:
                    print(f"    -> deAPI.ai: rate limited, retrying...", file=sys.stderr)
                    continue
                print(f"    -> deAPI.ai: rate limited after retries", file=sys.stderr)
                return None
            print(f"    -> deAPI.ai: error {code} - {body_err}", file=sys.stderr)
            return None

    rid = result.get("request_id") or result.get("data", {}).get("request_id", "")
    if not rid:
        print(f"    -> deAPI.ai: no request_id in response", file=sys.stderr)
        return None

    result_url = _deapi_poll(rid, timeout=180)
    if not result_url:
        print(f"    -> deAPI.ai: polling failed/timed out", file=sys.stderr)
        return None

    dl = urllib.request.Request(result_url, headers={"User-Agent": DEAPI_UA})
    try:
        with urllib.request.urlopen(dl, timeout=60) as resp:
            img = resp.read()
    except Exception:
        return None

    if len(img) < 500:
        return None

    ct = resp.headers.get("Content-Type", "image/png")
    ext = {"image/png": "png", "image/jpeg": "jpg"}.get(ct, "png")
    print(f"    -> deAPI.ai FLUX.2-klein img2img success ({len(img)//1024} KB, {ext})", file=sys.stderr)
    return (ext, img)


# ── Phase 1: Analyze image & generate listing JSON ──────────────────────────

SEO_PERSONA = """You are EcomListing Pro: a senior marketplace SEO engineer and brand copywriter for Indian marketplaces (Amazon.in, Flipkart, Meesho). You work the way a Helium 10 / Keepa power user does — keyword-first, indexing-aware, conversion-led — and you write with the restraint of a premium brand. Output ONLY valid JSON — no markdown, no extra text."""

LISTING_SCHEMA = """{
  "product_analysis": {
    "category": "",
    "product_name": "",
    "brand": "",
    "key_features": [],
    "target_audience": "",
    "use_case": ""
  },
  "brand_positioning": {
    "core_promise": "",
    "tagline": "",
    "price_justification": [],
    "tone": ""
  },
  "seo": {
    "primary_keyword": "",
    "secondary_keywords": [],
    "long_tail_keywords": [],
    "amazon_keywords_used": [],
    "flipkart_keywords_used": []
  },
  "listing_fields": {
    "product_title": "",
    "item_highlights": "",
    "bullet_points": ["", "", "", "", ""],
    "description": "",
    "backend_search_terms": "",
    "mrp": "",
    "selling_price": "",
    "hsn_code": "",
    "gst_rate": "",
    "brand": "",
    "category_path": "",
    "subcategory": "",
    "color": "",
    "size_dimensions": "",
    "material": "",
    "weight": "",
    "package_dimensions": "",
    "country_of_origin": "",
    "manufacturer_details": "",
    "packer_details": "",
    "importer_details": "",
    "manufacturing_date": "",
    "expiry_date": "",
    "shelf_life": "",
    "net_quantity": "",
    "sku": "",
    "ean_upc": "",
    "pack_of": "",
    "stock_quantity": "",
    "fssai_license": "",
    "bis_certification": "",
    "customer_care": "",
    "warranty": "",
    "care_instructions": "",
    "size_chart": "",
    "fit_type": "",
    "fabric": "",
    "pattern": "",
    "occasion": "",
    "suitable_for": "",
    "age_range": "",
    "battery": "",
    "power": "",
    "connectivity": "",
    "compatible_devices": "",
    "key_features_infographic": [],
    "search_keywords": "",
    "shipping_weight": "",
    "fulfillment_type": "",
    "tax_code": ""
  },
  "platform_specific": {
    "amazon": {
      "title": "",
      "item_highlights": "",
      "bullets": [],
      "description": "",
      "backend_keywords": ""
    },
    "flipkart": {
      "title": "",
      "description": "",
      "search_keywords": ""
    },
    "meesho": {
      "title": "",
      "description": "",
      "attributes": {}
    }
  },
  "image_prompts": {
    "1_lifestyle_usecase": {"purpose": "", "prompt": ""},
    "2_before_after": {"purpose": "", "prompt": ""},
    "3_how_to_use": {"purpose": "", "prompt": ""},
    "4_enhanced_hero": {"purpose": "", "prompt": ""}
  },
  "compliance_checklist": {"amazon": [], "flipkart": [], "meesho": []}
}"""

VALUE_RULES = """PERCEIVED VALUE — write like a premium brand (think how Apple presents a phone), so the price feels earned:
- Lead with the outcome and the feeling of owning it, then prove it with a concrete detail (material, quantity, measurement, how it is made). Never lead with a spec list.
- One idea per sentence. Short, confident, calm. No hype words: no "best", "amazing", "No.1", "ultimate", "!!", no ALL-CAPS sentences in descriptions.
- Specifics beat superlatives: "80 wipes — about three months of daily use" beats "long lasting".
- Justify the price: cost-per-use, what it replaces, time saved, what it protects (e.g. the shoes it keeps new). Fill brand_positioning.price_justification with 3 such reasons and weave them into the description.
- brand_positioning.core_promise = the single benefit everything else supports. tagline = ≤6 words.
- Honesty is non-negotiable: do NOT invent certifications, awards, clinical claims, test results, ratings, or "dermatologist/lab tested" unless visible on the pack. Only state what the image or hint supports."""

SEO_RULES = """SEO — every title and keyword field must be built from the REAL SHOPPER SEARCHES given above:
- seo.primary_keyword = the highest-demand search that truly describes this product. It goes in the first 40 characters of every title.
- Titles: Brand + primary keyword + key differentiator + size/pack. Readable, no keyword stuffing, no repeated words.
- Amazon: title ≤75 chars. 5 bullets, each starts with a 2-4 word CAPS benefit header, then one natural sentence containing a secondary or long-tail search phrase. backend_keywords ≤250 bytes, space-separated, lowercase, NO commas, NO words already in the title or bullets, NO brand names, NO competitor names — use it for synonyms, long-tail and Hinglish/regional spellings.
- Flipkart: title built from the FLIPKART searches (Flipkart shoppers search differently). search_keywords = comma-separated phrases taken from the Flipkart list.
- Use phrases from the lists word-for-word where they fit; marketplaces index exact words.
- Skip any search that names another brand, another retailer, a city, or a different product type.
- List the searches you actually used in seo.amazon_keywords_used and seo.flipkart_keywords_used."""

IMAGE_RULES = """IMAGE PROMPTS — art-direct like a premium brand campaign, 2-3 specific sentences each, photorealistic, 1:1 square:
- One hero subject, generous negative space, a restrained 2-3 colour palette drawn from the product, nothing cluttered.
- Controlled studio or soft window light, gentle falloff, real shadows and reflections, visible material texture.
- 1_lifestyle_usecase: an aspirational, tidy real-life moment — the product in use by its ideal owner, shallow depth of field.
- 2_before_after: one clean, believable transformation, same angle and light on both sides. Honest, not exaggerated.
- 3_how_to_use: a single calm gesture showing the key step — hands and product, minimal backdrop.
- 4_enhanced_hero: product alone, centred, pure white seamless background, filling ~85% of frame, soft shadow beneath.
- The product must look exactly like the reference photo: same pack, colours, label. No extra text, logos, badges or props that are not real."""


def _vision_json(system_prompt, user_prompt, img_b64, mime, max_tokens=8192, temperature=0.7):
    """Send image + prompt through the listing model chain; return parsed JSON. Raises RuntimeError if all fail."""
    last_error = None
    for model in LISTING_MODELS:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                    {"type": "text", "text": user_prompt}
                ]}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        code = 0
        for attempt in range(3):
            result = or_request(payload)
            if not result or result.get("_http_error"):
                code = result.get("_http_error", 0) if result else 0
                if code == 429:
                    wait = 2 * (attempt + 1)
                    print(f"    -> {model}: rate limited, retrying in {wait}s (attempt {attempt+1}/3)", file=sys.stderr)
                    time.sleep(wait)
                    continue
                last_error = f"{model}: {result.get('_error_msg', 'failed')}" if result else "Request failed"
                break
            if "choices" not in result:
                last_error = f"{model}: no choices key"
                print(f"    -> {model}: no choices key — {str(result)[:200]}", file=sys.stderr)
                break
            msg = result["choices"][0].get("message", {})
            raw = msg.get("content")
            if raw is None:
                finish = result["choices"][0].get("finish_reason", "?")
                refusals = msg.get("refusals", [])
                print(f"    -> {model}: null content (finish={finish}, refusals={refusals})", file=sys.stderr)
                last_error = f"{model}: null content"
                break
            raw = raw.strip()
            if not raw:
                last_error = f"{model}: empty content"
                break
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                raw = raw.rsplit("```", 1)[0].strip()
            # Try full parse first, then extract first JSON object
            for parser in (json.loads, lambda s: json.loads(s[s.find("{"):s.rfind("}")+1])):
                try:
                    return parser(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
            last_error = f"{model}: JSON parse error"
            print(f"    -> {model}: JSON parse error — response preview: {raw[:300]}...", file=sys.stderr)
            break

        if attempt == 2 and code == 429:
            last_error = f"{model}: rate limited after 3 retries"
        if last_error and "rate limited" not in last_error:
            continue
        time.sleep(2)

    print(f"  ERROR: All models failed. Last error: {last_error}", file=sys.stderr)
    raise RuntimeError(f"All listing models failed: {last_error}")


def find_seed_keywords(img_b64, mime, product_hint=None):
    """Quick first look: what would a shopper type to find this product?"""
    hint_block = f"\nPRODUCT HINT: {product_hint}\n" if product_hint else ""
    prompt = ("Look at this product." + hint_block + """
Return ONLY JSON: {"product_name": "", "seed_keywords": ["", "", "", ""]}
seed_keywords = the 4 most common generic 2-3 word phrases an Indian shopper would type into Amazon or Flipkart search to find this kind of product. Generic product terms only — no brand names, no sizes, no colours.""")
    try:
        result = _vision_json(SEO_PERSONA, prompt, img_b64, mime, max_tokens=300, temperature=0.2)
        seeds = [s for s in result.get("seed_keywords", []) if isinstance(s, str) and s.strip()]
    except RuntimeError:
        seeds = []
    if not seeds and product_hint:
        seeds = [product_hint]
    return seeds


def _keyword_block(research_data):
    if not research_data or not research_data.get("ranked"):
        return "\n(No live search data available — choose keywords from your own marketplace knowledge.)\n"
    lines = ["\nREAL SHOPPER SEARCHES (live autocomplete, most popular first):"]
    lines.append("AMAZON.IN: " + "; ".join(research_data["amazon"][:25]))
    # Flipkart blocks some server IPs — fall back to the Amazon list rather than an empty one
    lines.append("FLIPKART: " + ("; ".join(research_data["flipkart"][:25]) or "(unavailable — use the AMAZON.IN searches)"))
    lines.append("GOOGLE INDIA: " + "; ".join(research_data["google"][:10]))
    lines.append("TOP COMBINED: " + "; ".join(r["keyword"] for r in research_data["ranked"][:15]))
    return "\n".join(lines) + "\n"


def _trim_bytes(text, limit=250):
    """Amazon ignores the whole backend field if it exceeds 250 bytes — cut at a word boundary."""
    words, out = str(text or "").replace(",", " ").split(), []
    for w in words:
        if len(" ".join(out + [w]).encode("utf-8")) > limit:
            break
        if w not in out:
            out.append(w)
    return " ".join(out)


def analyze_image(image_path, product_hint=None):
    print(f"  Phase 1: Analyzing product image...", file=sys.stderr)
    if product_hint:
        print(f"    Product hint: {product_hint}", file=sys.stderr)
    img_b64 = encode_image(image_path)
    mime = mime_for(image_path)

    # 1a. What do shoppers actually search for?
    import keyword_research
    research_data = None
    seeds = find_seed_keywords(img_b64, mime, product_hint)
    if seeds:
        print(f"    Keyword research: {', '.join(seeds)}", file=sys.stderr)
        try:
            research_data = keyword_research.research(seeds)
            print(f"    -> {len(research_data['amazon'])} Amazon, {len(research_data['flipkart'])} Flipkart, "
                  f"{len(research_data['google'])} Google searches found", file=sys.stderr)
        except Exception as e:
            print(f"    -> keyword research failed: {e}", file=sys.stderr)
    else:
        print(f"    Keyword research skipped (no seed keywords)", file=sys.stderr)

    # 1b. Write the listing around those searches
    hint_block = "\nPRODUCT HINT (use this to guide your analysis): " + product_hint + "\n" if product_hint else ""
    user_prompt = (
        "Analyze this product image and generate a complete e-commerce listing.\n" + hint_block
        + _keyword_block(research_data)
        + "\nOutput ONLY valid JSON with this exact structure:\n\n" + LISTING_SCHEMA + "\n\n"
        + VALUE_RULES + "\n\n" + SEO_RULES + "\n\n" + IMAGE_RULES + """

OTHER RULES:
- MRP > selling price
- For unknown fields, provide sensible defaults for Indian market (INR, India)"""
    )
    listing = _vision_json(SEO_PERSONA, user_prompt, img_b64, mime)

    # 1c. Enforce hard limits and record what was researched vs. used
    fields = listing.setdefault("listing_fields", {})
    amazon = listing.setdefault("platform_specific", {}).setdefault("amazon", {})
    fields["backend_search_terms"] = _trim_bytes(fields.get("backend_search_terms"))
    amazon["backend_keywords"] = _trim_bytes(amazon.get("backend_keywords"))
    if research_data:
        listing["keyword_research"] = research_data
        listing["keyword_coverage"] = keyword_research.coverage(research_data, listing)
        for plat, rep in listing["keyword_coverage"].items():
            print(f"    {plat}: {rep['coverage_pct']}% of top searches covered", file=sys.stderr)
    return listing


# ── Phase 2: Generate images ────────────────────────────────────────────────
# Priority: NVIDIA FLUX (free) → Gemini OpenRouter (paid) → Pollinations (free/low-quality)

PREMIUM_STYLE = (
    "Premium brand campaign photography. One hero subject, generous negative space, "
    "restrained colour palette, soft controlled lighting with real shadows, crisp material detail. "
    "The product must match the reference exactly — same pack, colours and label. "
    "Uncluttered, no added text, logos or badges. Photorealistic, 1:1 square."
)

IMAGE_FALLBACK_SCENES = {
    "1_lifestyle_usecase": "The product in use in a tidy, aspirational real-life setting, shallow depth of field.",
    "2_before_after": "Split-screen before and after, same angle and light. Left: the problem. Right: the clean result. Believable, not exaggerated.",
    "3_how_to_use": "A single calm gesture showing how the product is used — hands and product on a minimal light backdrop.",
    "4_enhanced_hero": "Product alone, centred on a pure white seamless background, filling 85% of the frame, soft shadow beneath.",
}


def premium_image_prompt(image_key, user_prompt, product_name, max_len=None):
    """Scene written for this product by the listing model + the shared premium art direction."""
    scene = (user_prompt or "").strip() or IMAGE_FALLBACK_SCENES.get(image_key, "")
    if image_key == "4_enhanced_hero":
        # Marketplaces require a pure white main image — never let the scene override that
        scene = IMAGE_FALLBACK_SCENES[image_key]
    prompt = f"Product: {product_name}. {scene} {PREMIUM_STYLE}"
    return prompt[:max_len] if max_len else prompt


POLLINATION_MODELS = ["seedream5", "zimage", "flux"]


def build_nvidia_prompt(image_key, user_prompt, product_name, category):
    """Prompt for FLUX models on NVIDIA."""
    return premium_image_prompt(image_key, user_prompt, product_name)


def generate_via_nvidia(image_key, user_prompt, ref_image_path, product_name, category):
    """Generate image via NVIDIA FLUX.1-schnell (free, fast, 1024x1024)."""
    structured = build_nvidia_prompt(image_key, user_prompt, product_name, category)
    seed = hash(image_key + str(time.time())) % 10000000

    payload = {
        "prompt": structured,
        "width": 1024,
        "height": 1024,
        "seed": seed,
        "steps": 4,
    }

    response = nvidia_request(payload, "flux.1-schnell", timeout=120)
    if response.get("_success"):
        artifacts = response["data"].get("artifacts", [])
        if artifacts and "base64" in artifacts[0]:
            img_data = base64.b64decode(artifacts[0]["base64"])
            if len(img_data) > 500:
                print(f"    -> NVIDIA FLUX success ({len(img_data)//1024} KB)", file=sys.stderr)
                return ("jpg", img_data)

    print(f"    -> NVIDIA: error {response.get('_http_error', '?')}", file=sys.stderr)
    return None


def generate_via_gemini(image_key, user_prompt, ref_image_path, product_name, category):
    """Fallback: generate via Gemini 3.1 Flash Image on OpenRouter."""
    ref_b64 = encode_image(ref_image_path)
    ref_mime = mime_for(ref_image_path)

    prompt = premium_image_prompt(image_key, user_prompt, product_name)

    payload = {
        "model": GEMINI_IMAGE_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{ref_mime};base64,{ref_b64}"}},
            {"type": "text", "text": f"Reference image above. Generate: {prompt}\nNo text in image."}
        ]}],
        "max_tokens": 4096,
    }

    result = or_request(payload, timeout=300)
    if not result or result.get("_http_error"):
        code = result.get("_http_error", 0) if result else 0
        if code == 402:
            print(f"    -> Gemini: insufficient credits", file=sys.stderr)
        else:
            print(f"    -> Gemini: error {code}", file=sys.stderr)
        return None

    msg = result.get("choices", [{}])[0].get("message", {})
    images_data = msg.get("images")

    if images_data and isinstance(images_data, list):
        for img in images_data:
            if not isinstance(img, dict):
                continue
            # Standard format: {"type": "image_url", "image_url": {"url": "data:..."}}
            if img.get("type") == "image_url":
                url = img.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    try:
                        _, b64data = url.split(",", 1)
                        mime = url.split(":")[1].split(";")[0]
                        ext = {"image/png": "png", "image/jpeg": "jpg"}.get(mime, "png")
                        return (ext, base64.b64decode(b64data))
                    except Exception:
                        pass
            # Alt format: {"url": "data:...", "content_type": "..."}
            url = img.get("url", "")
            if url.startswith("data:"):
                try:
                    _, b64data = url.split(",", 1)
                    ct = img.get("content_type", "image/png")
                    ext = {"image/png": "png", "image/jpeg": "jpg"}.get(ct, "png")
                    return (ext, base64.b64decode(b64data))
                except Exception:
                    pass

    content = msg.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if "inline_data" in part:
                    d = part["inline_data"]
                    return ("png", base64.b64decode(d["data"]))
                if "image_url" in part:
                    url = part["image_url"]["url"]
                    if url.startswith("data:"):
                        _, b = url.split(",", 1)
                        return ("png", base64.b64decode(b))

    return None


def generate_via_pollinations(image_key, user_prompt, product_name, category):
    """Last resort: Pollinations.ai free API."""
    # Prompt travels in the URL — keep it short
    structured = premium_image_prompt(image_key, user_prompt, product_name, max_len=600)

    for model in POLLINATION_MODELS:
        size = 2048 if model == "seedream5" else 1024
        seed = hash(image_key + model) % 10000000
        params = {"width": size, "height": size, "model": model,
                  "enhance": "false", "nologo": "true", "private": "true", "seed": str(seed)}
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{POLLINATIONS_URL}/{urllib.parse.quote(structured)}?{qs}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "EcomListingPro/1.0"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = resp.read()
            if len(data) >= 500:
                return ("jpg", data)
        except Exception:
            continue
    return None


def generate_image(image_key, user_prompt, ref_image_path, product_name, category, image_num, total, image_model="deapi"):
    """Generate image using specified model or fallback chain."""
    print(f"  Phase 2: Image {image_num}/{total} — {image_key}", file=sys.stderr)

    if image_model == "gemini":
        result = generate_via_gemini(image_key, user_prompt, ref_image_path, product_name, category)
        if result:
            print(f"    -> Gemini success ({len(result[1])//1024} KB, {result[0]})", file=sys.stderr)
            return result
        print(f"    -> Gemini failed", file=sys.stderr)
        return None

    if image_model == "nvidia":
        if not NVIDIA_API_KEY:
            print(f"    -> NVIDIA: key not available", file=sys.stderr)
            return None
        result = generate_via_nvidia(image_key, user_prompt, ref_image_path, product_name, category)
        return result

    if image_model == "pollinations":
        result = generate_via_pollinations(image_key, user_prompt, product_name, category)
        return result

    # Default: deAPI.ai → NVIDIA → Gemini → Pollinations fallback chain
    if DE_API_KEY:
        result = generate_via_deapi(image_key, user_prompt, ref_image_path, product_name, category)
        if result:
            return result
    else:
        print(f"    -> deAPI.ai: key not available", file=sys.stderr)

    if NVIDIA_API_KEY:
        result = generate_via_nvidia(image_key, user_prompt, ref_image_path, product_name, category)
        if result:
            return result
    else:
        print(f"    -> NVIDIA: key not available", file=sys.stderr)

    result = generate_via_gemini(image_key, user_prompt, ref_image_path, product_name, category)
    if result:
        return result

    print(f"    -> Trying Pollinations.ai fallback...", file=sys.stderr)
    result = generate_via_pollinations(image_key, user_prompt, product_name, category)
    if result:
        print(f"    -> Pollinations fallback", file=sys.stderr)
        return result

    print(f"    -> ALL SOURCES FAILED", file=sys.stderr)
    return None


def generate_all_images(listing, ref_image_path, image_model="deapi"):
    prompts = listing.get("image_prompts", {})
    analysis = listing.get("product_analysis", {})
    product_name = analysis.get("product_name", "")
    category = analysis.get("category", "")

    delay = 3 if image_model != "deapi" else 15

    images = {}
    total = len(prompts)
    for i, (key, info) in enumerate(prompts.items(), 1):
        user_prompt = info.get("prompt", "")
        if not user_prompt:
            continue
        result = generate_image(key, user_prompt, ref_image_path, product_name, category, i, total, image_model)
        if result:
            ext, data = result
            images[key] = (ext, data)
        if i < total:
            time.sleep(delay)
    return images


# ── Phase 3: Save everything ────────────────────────────────────────────────

def save_listing(listing, images):
    product_name = listing.get("product_analysis", {}).get("product_name", "product")
    slug = slugify(product_name)
    out_dir = os.path.join(PRODUCTS_DIR, slug)
    os.makedirs(out_dir, exist_ok=True)

    print(f"  Phase 3: Saving to {out_dir}/", file=sys.stderr)

    with open(os.path.join(out_dir, "listing.json"), "w") as f:
        json.dump(listing, f, indent=2, ensure_ascii=False)

    name_map = {
        "1_lifestyle_usecase": "lifestyle_usecase",
        "2_before_after": "before_after",
        "3_how_to_use": "how_to_use",
        "4_enhanced_hero": "enhanced_hero",
    }
    for key, (ext, data) in images.items():
        clean_name = name_map.get(key, key)
        path = os.path.join(out_dir, f"{clean_name}.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        print(f"    {clean_name}.{ext}", file=sys.stderr)

    for platform in ["amazon", "flipkart", "meesho"]:
        info = listing.get("platform_specific", {}).get(platform, {})
        if info:
            path = os.path.join(out_dir, f"{platform}_listing.txt")
            with open(path, "w") as f:
                f.write(f"=== {platform.upper()} Listing ===\n\n")
                for k, v in info.items():
                    if isinstance(v, list):
                        f.write(f"{k}:\n" + "\n".join(f"  - {i}" for i in v) + "\n\n")
                    else:
                        f.write(f"{k}: {v}\n\n")

    print(f"\n  All files saved to: {out_dir}/", file=sys.stderr)
    return out_dir


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} [--image-model <deapi|nvidia|gemini|pollinations>] [--hint <product-hint>] <product-image-path>", file=sys.stderr)
        sys.exit(1)

    image_model = "deapi"
    product_hint = None
    image_path = sys.argv[-1]
    args = sys.argv[1:-1]
    for i, a in enumerate(args):
        if a == "--image-model" and i + 1 < len(args):
            image_model = args[i + 1]
        if a == "--hint" and i + 1 < len(args):
            product_hint = args[i + 1]

    if image_model not in ("deapi", "nvidia", "gemini", "pollinations"):
        print(f"ERROR: invalid --image-model '{image_model}'. Use deapi, nvidia, gemini, or pollinations.", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(image_path):
        print(f"ERROR: file not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    listing = analyze_image(image_path, product_hint)
    images = generate_all_images(listing, image_path, image_model)
    out_dir = save_listing(listing, images)

    from local_export import export_product
    print(f"\n  Phase 4: Exporting to NeedKart product folder...", file=sys.stderr)
    nk_dir = export_product(listing, images)
    print(f"  NeedKart: {nk_dir}", file=sys.stderr)

    print(f"\n  Phase 5: Publishing to NeedKart store...", file=sys.stderr)
    from needkart_client import NeedKartClient
    product_url, product_id = NeedKartClient().publish_listing(listing, images)
    if product_url:
        print(f"  NeedKart: {product_url}", file=sys.stderr)
    else:
        print(f"  NeedKart: publish skipped/failed", file=sys.stderr)

    src_map = {"deapi": "deAPI.ai FLUX.2-klein/img2img", "nvidia": "NVIDIA FLUX.1-schnell", "gemini": "Gemini/OpenRouter", "pollinations": "Pollinations.ai"}
    print(f"\nImages: {len(images)}/4 | Source: {src_map[image_model]}", file=sys.stderr)
    print(f"Output: {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
