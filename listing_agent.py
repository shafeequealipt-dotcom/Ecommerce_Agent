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
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
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
    prompt_map = {
        "1_lifestyle_usecase": (
            f"Product shot: {product_name}. Natural lifestyle use, person using product "
            f"in real-world setting, bright daylight, warm atmosphere, "
            f"professional e-commerce photography, sharp focus. 1:1 square."
        ),
        "2_before_after": (
            f"Product: {product_name}. Split-screen before-and-after comparison. "
            f"Left side: dirty untreated dull state. Right side: clean fresh vibrant state. "
            f"The product is visible. Studio lighting, photorealistic. 1:1 square."
        ),
        "3_how_to_use": (
            f"Product: {product_name}. Instructional shot showing how to use the product. "
            f"Clean minimal composition on light background, "
            f"bright lighting, professional e-commerce. 1:1 square."
        ),
        "4_enhanced_hero": (
            f"Product: {product_name}. Premium e-commerce hero shot. "
            f"Product centered on pure white background filling 85% of frame. "
            f"Soft studio lighting with subtle shadow for depth. "
            f"High-end commercial photography, true-to-life colors, every detail visible. 1:1 square."
        ),
    }
    prompt = prompt_map.get(image_key, user_prompt)

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

def analyze_image(image_path, product_hint=None):
    print(f"  Phase 1: Analyzing product image...", file=sys.stderr)
    if product_hint:
        print(f"    Product hint: {product_hint}", file=sys.stderr)
    img_b64 = encode_image(image_path)
    mime = mime_for(image_path)

    system_prompt = """You are EcomListing Pro, an expert e-commerce product listing agent for Indian marketplaces (Amazon.in, Flipkart, Meesho). You generate complete, platform-compliant product listings from a single product image. Output ONLY valid JSON — no markdown, no extra text."""

    hint_block = "\nPRODUCT HINT (use this to guide your analysis): " + product_hint + "\n" if product_hint else ""

    user_prompt = "Analyze this product image and generate a complete e-commerce listing.\n" + hint_block + """

Output ONLY valid JSON with this exact structure:

{
  "product_analysis": {
    "category": "",
    "product_name": "",
    "brand": "",
    "key_features": [],
    "target_audience": "",
    "use_case": ""
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
}

RULES:
- Product title ≤75 chars (Amazon 2026 rule)
- Amazon bullets: 5 benefit-first with CAPS header
- Backend keywords: ≤250 bytes
- MRP > selling price
- For unknown fields, provide sensible defaults for Indian market (INR, India)
- Image prompts: describe the SCENE not just product. Include setting, lighting, what the product looks like, action, composition. Be specific. Photorealistic, 1:1 square. 2-3 detailed sentences."""

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
            "temperature": 0.7,
            "max_tokens": 4096,
        }

        code = 0
        for attempt in range(3):
            result = or_request(payload)
            if not result or result.get("_http_error"):
                code = result.get("_http_error", 0) if result else 0
                if code == 429:
                    wait = 5 * (attempt + 1)
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
    sys.exit(1)


# ── Phase 2: Generate images ────────────────────────────────────────────────
# Priority: NVIDIA FLUX (free) → Gemini OpenRouter (paid) → Pollinations (free/low-quality)

POLLINATION_MODELS = ["seedream5", "zimage", "flux"]


def build_nvidia_prompt(image_key, user_prompt, product_name, category):
    """Concise, high-quality prompt optimized for FLUX models on NVIDIA."""
    templates = {
        "1_lifestyle_usecase": (
            f"Product shot: {product_name}. Lifestyle photo showing the product in use "
            f"in a real-world setting. Clean modern environment, natural daylight. "
            f"The product is the focal point, clearly visible and being used naturally. "
            f"Warm inviting atmosphere. Professional e-commerce photography, photorealistic, "
            f"sharp focus, accurate colors. 1:1 square."
        ),
        "2_before_after": (
            f"Product: {product_name}. Before-and-after comparison, split-screen. "
            f"Left: dirty/untreated state with dull colors. "
            f"Right: clean/treated state with vibrant fresh look. "
            f"The product is visible. Clean vertical division. "
            f"Studio lighting, photorealistic. 1:1 square."
        ),
        "3_how_to_use": (
            f"Product: {product_name}. Clean instructional shot showing how to use the product. "
            f"Simple minimal composition on light background. "
            f"Bright even lighting, sharp focus, professional. "
            f"Photorealistic product photography. 1:1 square."
        ),
        "4_enhanced_hero": (
            f"Product: {product_name}. Premium e-commerce hero shot. "
            f"Product centered on pure white background, filling 85% of frame. "
            f"Soft studio lighting with subtle shadow beneath for depth. "
            f"High-end commercial photography style, true-to-life colors, "
            f"sharp focus, every detail visible. 1:1 square."
        ),
    }
    return templates.get(image_key, user_prompt)


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

    prompt_map = {
        "1_lifestyle_usecase": f"Product: {product_name}. Lifestyle shot in use, natural setting. Photorealistic. 1:1 square.",
        "2_before_after": f"Product: {product_name}. Split-screen before-and-after. LEFT: problem. RIGHT: after. 1:1 square.",
        "3_how_to_use": f"Product: {product_name}. Instructional shot. Clean background. 1:1 square.",
        "4_enhanced_hero": f"Product: {product_name}. Hero shot on pure white bg. Centered, 85% frame. Studio lighting. 1:1 square.",
    }
    prompt = prompt_map.get(image_key, user_prompt)

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
    prompts = {
        "1_lifestyle_usecase": f"{product_name}. Lifestyle shot. Natural use. Photorealistic. 1:1 square.",
        "2_before_after": f"{product_name}. Before-after split. LEFT dirty RIGHT clean. 1:1 square.",
        "3_how_to_use": f"{product_name}. Instructional use. White bg. 1:1 square.",
        "4_enhanced_hero": f"{product_name}. Hero shot. White bg. Centered. Studio lighting. 1:1 square.",
    }
    structured = prompts.get(image_key, user_prompt)

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
        print(f"Usage: {sys.argv[0]} [--image-model <deapi|nvidia|gemini|pollinations>] [--hint <product-hint>] [--push] <product-image-path>", file=sys.stderr)
        sys.exit(1)

    image_model = "deapi"
    product_hint = None
    push_to_needkart = False
    image_path = sys.argv[-1]
    args = sys.argv[1:-1]
    for i, a in enumerate(args):
        if a == "--image-model" and i + 1 < len(args):
            image_model = args[i + 1]
        if a == "--hint" and i + 1 < len(args):
            product_hint = args[i + 1]
        if a == "--push":
            push_to_needkart = True

    if image_model not in ("deapi", "nvidia", "gemini", "pollinations"):
        print(f"ERROR: invalid --image-model '{image_model}'. Use deapi, nvidia, gemini, or pollinations.", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(image_path):
        print(f"ERROR: file not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    listing = analyze_image(image_path, product_hint)
    images = generate_all_images(listing, image_path, image_model)
    out_dir = save_listing(listing, images)

    if push_to_needkart:
        print(f"\n  Phase 4: Publishing to NeedKart...", file=sys.stderr)
        from needkart_client import NeedKartClient
        client = NeedKartClient()
        product_url, product_id = client.publish_listing(listing, images)
        if product_url:
            print(f"  NeedKart: {product_url}", file=sys.stderr)
        else:
            print(f"  NeedKart: publish skipped/failed", file=sys.stderr)

    src_map = {"deapi": "deAPI.ai FLUX.2-klein/img2img", "nvidia": "NVIDIA FLUX.1-schnell", "gemini": "Gemini/OpenRouter", "pollinations": "Pollinations.ai"}
    print(f"\nImages: {len(images)}/4 | Source: {src_map[image_model]}", file=sys.stderr)
    print(f"Output: {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
