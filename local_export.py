#!/usr/bin/env python3
"""Local NeedKart export — writes each generated product into the NeedKart repo.

Runs alongside the remote Admin API push. Layout:

    <PRODUCT_ROOT>/<product-name>/
        product.json        store-ready fields (title, price, sku, category, images ...)
        listing.json        full AI-generated listing
        keyword_research.json  live Amazon/Flipkart/Google searches + coverage report
        <platform>_listing.txt
        image_prompts.md    ready-to-paste prompts for making the images by hand
        reference.<ext>     the original product photo, to attach alongside each prompt
        images/             the product images (generated, or dropped in by hand)
        published.json      written by publish_local.py once the product is on the store
"""

import json
import os
import shutil
import sys

from listing_agent import slugify

# On the Mac this is the NeedKart repo; anywhere else (e.g. the VM) fall back to a folder beside the agent
_NEEDKART_REPO = "/Users/naash/Documents/Projects/NeedKart"
PRODUCT_ROOT = os.environ.get("NEEDKART_PRODUCT_DIR") or (
    os.path.join(_NEEDKART_REPO, "product") if os.path.isdir(_NEEDKART_REPO)
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "needkart_product")
)

IMAGE_NAMES = {
    "1_lifestyle_usecase": "lifestyle_usecase",
    "2_before_after": "before_after",
    "3_how_to_use": "how_to_use",
    "4_enhanced_hero": "enhanced_hero",
}


def _num(value, default):
    try:
        return int(float(str(value).replace(",", "").replace("₹", "").replace("g", "").strip()))
    except (ValueError, TypeError):
        return default


def build_product(listing, image_files):
    analysis = listing.get("product_analysis", {})
    fields = listing.get("listing_fields", {})
    title = str(fields.get("product_title") or analysis.get("product_name") or "Product")
    handle = slugify(title)
    price = _num(fields.get("selling_price"), 499)
    bullets = [b for b in fields.get("bullet_points", []) if isinstance(b, str) and b]
    description = str(fields.get("description") or "")
    return {
        "title": title,
        "handle": handle,
        "brand": analysis.get("brand"),
        "category": analysis.get("category"),
        "description": f"{description}\n\n" + "\n".join(f"- {b}" for b in bullets),
        "price": price,
        "mrp": _num(fields.get("mrp"), price * 2),
        "currency": "inr",
        "sku": str(fields.get("sku") or f"NK-{handle.upper()}"),
        "weight": _num(fields.get("weight"), 200),
        "images": image_files,
        "tagline": listing.get("brand_positioning", {}).get("tagline"),
        "price_justification": listing.get("brand_positioning", {}).get("price_justification", []),
        "seo": listing.get("seo", {}),
    }


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def list_images(product_dir):
    """Image files in a product's images/ folder — hero first, then by name."""
    img_dir = os.path.join(product_dir, "images")
    if not os.path.isdir(img_dir):
        return []
    files = [f for f in os.listdir(img_dir) if f.lower().endswith(IMAGE_EXTS) and not f.startswith(".")]
    return sorted(files, key=lambda f: ("hero" not in f.lower(), f.lower()))


def write_image_prompts(listing, out_dir):
    """Prompts for generating the images by hand (e.g. in ChatGPT), one block per image."""
    from listing_agent import premium_image_prompt
    name = listing.get("product_analysis", {}).get("product_name", "product")
    prompts = listing.get("image_prompts", {})
    appearance = listing.get("product_analysis", {}).get("appearance", "")
    lines = [
        f"# Image prompts — {name}",
        "",
        f"{len(prompts)} separate images. Send ONE prompt per message — never paste them together,",
        "or you get a single collage. For each: attach the product photo (`reference.*` in this folder),",
        "paste the prompt, then save the result into `images/` with the file name shown.",
        "",
    ]
    for n, (key, info) in enumerate(prompts.items(), 1):
        stem = IMAGE_NAMES.get(key, key)
        prompt = premium_image_prompt(key, (info or {}).get("prompt", ""), name, appearance=appearance)
        # A before/after is one picture with two halves; everything else is a single scene
        layout = ("One single image showing one side-by-side comparison — no other panels."
                  if key == "2_before_after" else
                  "One single scene filling the whole frame.")
        lines += [
            f"## Image {n} of {len(prompts)} — {stem}",
            f"Save as: `images/{stem}.png`",
            "",
            "```",
            f"Generate exactly ONE standalone image for this prompt only. {layout} "
            f"Not a collage, grid, contact sheet or set of variations. "
            f"Use the attached photo as the exact product reference. {prompt}",
            "```",
            "",
        ]
    path = os.path.join(out_dir, "image_prompts.md")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def export_product(listing, images, ref_image_path=None):
    """Write the product folder. Returns its path."""
    name = listing.get("product_analysis", {}).get("product_name", "product")
    out_dir = os.path.join(PRODUCT_ROOT, slugify(name))
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    for key, (ext, data) in sorted(images.items()):
        fname = f"{IMAGE_NAMES.get(key, key)}.{ext}"
        with open(os.path.join(img_dir, fname), "wb") as f:
            f.write(data)
    # Whatever is in images/ now — including files dropped in by hand on an earlier run
    image_files = [f"images/{f}" for f in list_images(out_dir)]

    if ref_image_path and os.path.isfile(ref_image_path):
        ext = os.path.splitext(ref_image_path)[1].lower() or ".jpg"
        shutil.copyfile(ref_image_path, os.path.join(out_dir, f"reference{ext}"))
    write_image_prompts(listing, out_dir)

    with open(os.path.join(out_dir, "product.json"), "w") as f:
        json.dump(build_product(listing, image_files), f, indent=2, ensure_ascii=False)
    with open(os.path.join(out_dir, "listing.json"), "w") as f:
        json.dump(listing, f, indent=2, ensure_ascii=False)

    if listing.get("keyword_research"):
        with open(os.path.join(out_dir, "keyword_research.json"), "w") as f:
            json.dump({"research": listing["keyword_research"], "coverage": listing.get("keyword_coverage", {})},
                      f, indent=2, ensure_ascii=False)

    for platform, info in listing.get("platform_specific", {}).items():
        if not info:
            continue
        with open(os.path.join(out_dir, f"{platform}_listing.txt"), "w") as f:
            f.write(f"=== {platform.upper()} Listing ===\n\n")
            for k, v in info.items():
                if isinstance(v, list):
                    f.write(f"{k}:\n" + "\n".join(f"  - {i}" for i in v) + "\n\n")
                else:
                    f.write(f"{k}: {v}\n\n")

    print(f"    NeedKart: exported locally -> {out_dir}", file=sys.stderr)
    return out_dir
