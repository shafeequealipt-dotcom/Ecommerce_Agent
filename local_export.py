#!/usr/bin/env python3
"""Local NeedKart export — writes each generated product into the NeedKart repo.

Runs alongside the remote Admin API push. Layout:

    <PRODUCT_ROOT>/<product-name>/
        product.json        store-ready fields (title, price, sku, category, images ...)
        listing.json        full AI-generated listing
        keyword_research.json  live Amazon/Flipkart/Google searches + coverage report
        <platform>_listing.txt
        images/             the generated images
"""

import json
import os
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


def export_product(listing, images):
    """Write the product folder. Returns its path."""
    name = listing.get("product_analysis", {}).get("product_name", "product")
    out_dir = os.path.join(PRODUCT_ROOT, slugify(name))
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    image_files = []
    for key, (ext, data) in sorted(images.items()):
        fname = f"{IMAGE_NAMES.get(key, key)}.{ext}"
        with open(os.path.join(img_dir, fname), "wb") as f:
            f.write(data)
        image_files.append(f"images/{fname}")

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
