#!/usr/bin/env python3
"""Publish locally prepared products to the NeedKart store.

Reads each product folder (listing.json + whatever is in images/) and creates the product
on the store. A published.json marker stops a product being published twice.

Usage: publish_local.py --list            show every product folder and its state
       publish_local.py --all             publish everything that has images and is unpublished
       publish_local.py <slug> [<slug>]   publish specific products
       add --force to publish again despite the marker (creates a second product if the first still exists)
"""

import json
import os
import sys
import time

from local_export import PRODUCT_ROOT, _images_dir, collect_images, export_product, list_images


def state(slug):
    d = os.path.join(PRODUCT_ROOT, slug)
    collect_images(slug)
    if not os.path.isfile(os.path.join(d, "listing.json")):
        return "no listing.json"
    if os.path.isfile(os.path.join(d, "published.json")):
        return "published"
    return f"ready ({len(list_images(d))} images)" if list_images(d) else "waiting for images"


def all_slugs():
    if not os.path.isdir(PRODUCT_ROOT):
        return []
    return sorted(s for s in os.listdir(PRODUCT_ROOT) if os.path.isdir(os.path.join(PRODUCT_ROOT, s)))


def publish(slug, force=False):
    d = os.path.join(PRODUCT_ROOT, slug)
    st = state(slug)
    if st == "published" and not force:
        print(f"  {slug}: already published — skipping (use --force to publish again)")
        return False
    files = list_images(d)
    if st == "no listing.json" or not files:
        print(f"  {slug}: {st if not files else 'no images'} — skipping")
        return False

    with open(os.path.join(d, "listing.json")) as f:
        listing = json.load(f)
    # Number the keys so the store keeps this order (hero first -> thumbnail)
    images = {}
    for i, fname in enumerate(files):
        stem, ext = os.path.splitext(fname)
        with open(os.path.join(_images_dir(d), fname), "rb") as f:
            images[f"{i}_{stem}"] = (ext.lstrip(".").lower(), f.read())

    from needkart_client import NeedKartClient
    url, product_id = NeedKartClient().publish_listing(listing, images)
    if not url:
        print(f"  {slug}: publish FAILED")
        return False

    export_product(listing, {})  # refresh product.json so it lists the images
    with open(os.path.join(d, "published.json"), "w") as f:
        json.dump({"url": url, "product_id": product_id, "images": files,
                   "published_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)
    print(f"  {slug}: published -> {url}")
    return True


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if "--list" in flags or not (args or "--all" in flags):
        for slug in all_slugs():
            print(f"  {slug:<60} {state(slug)}")
        if not all_slugs():
            print(f"  no product folders in {PRODUCT_ROOT}")
        return
    slugs = args or [s for s in all_slugs() if state(s).startswith("ready")]
    if not slugs:
        print("  nothing ready to publish")
        return
    done = sum(publish(s.strip("/"), "--force" in flags) for s in slugs)
    print(f"\n  {done}/{len(slugs)} published")


if __name__ == "__main__":
    main()
