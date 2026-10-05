#!/usr/bin/env python3
"""Publish locally prepared products to the NeedKart store.

Reads each product folder (listing.json + whatever is in images/) and creates the product
on the store. A published.json marker stops a product being published twice.

Usage: publish_local.py --list            show every product folder and its state
       publish_local.py --all             publish everything that has images and is unpublished
       publish_local.py <slug> [<slug>]   publish specific products
       add --force to publish again despite the marker (creates a second product if the first still exists)
       add --no-sync to skip pulling new product folders from the bot VM first
       publish_local.py --auto            unattended mode (run by sync_products.sh every minute): publish each
                                          product once its full set of images has been sitting untouched for 2 minutes
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
    if os.path.isfile(os.path.join(d, "publish_error.json")) and list_images(d):
        return f"ready ({len(list_images(d))} images) — last auto-publish FAILED"
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
    if os.path.isfile(os.path.join(d, "publish_error.json")):
        os.remove(os.path.join(d, "publish_error.json"))
    print(f"  {slug}: published -> {url}")
    return True


SETTLE_SECONDS = 120  # images must be untouched this long, so a half-saved set is never published


def _newest_image_mtime(d):
    return max((os.path.getmtime(os.path.join(_images_dir(d), f)) for f in list_images(d)), default=0)


def auto_ready(slug):
    """Unattended publish only when the whole image set is present, settled, and not already failed."""
    d = os.path.join(PRODUCT_ROOT, slug)
    if not state(slug).startswith("ready"):
        return False
    try:
        with open(os.path.join(d, "listing.json")) as f:
            expected = len(json.load(f).get("image_prompts", {})) or 4
    except (OSError, ValueError):
        return False
    newest = _newest_image_mtime(d)
    if len(list_images(d)) < expected or time.time() - newest < SETTLE_SECONDS:
        return False
    # A failed attempt is not retried until the images change (or the marker is deleted)
    err = os.path.join(d, "publish_error.json")
    return not (os.path.isfile(err) and os.path.getmtime(err) >= newest)


def notify(message):
    """Best-effort macOS notification — auto mode has no terminal to print to."""
    import subprocess
    try:
        subprocess.run(["osascript", "-e", f'display notification {json.dumps(message)} with title "NeedKart"'],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def auto_publish():
    lock = os.path.join(PRODUCT_ROOT, ".publish.lock")
    if os.path.isdir(lock) and time.time() - os.path.getmtime(lock) < 900:
        return  # a previous run is still publishing
    slugs = [s for s in all_slugs() if auto_ready(s)]
    if not slugs:
        return
    os.makedirs(lock, exist_ok=True)
    os.utime(lock)
    try:
        for slug in slugs:
            d = os.path.join(PRODUCT_ROOT, slug)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                ok = publish(slug)
                error = "" if ok else "publish failed — see log above"
            except Exception as e:
                ok, error = False, str(e)
            if ok:
                with open(os.path.join(d, "published.json")) as f:
                    url = json.load(f).get("url", "")
                print(f"auto_publish: {stamp} {slug} -> {url}")
                notify(f"Published {slug}")
            else:
                with open(os.path.join(d, "publish_error.json"), "w") as f:
                    json.dump({"error": error, "failed_at": stamp}, f, indent=2)
                print(f"auto_publish: {stamp} {slug} FAILED — {error}")
                notify(f"Publish FAILED for {slug}")
    finally:
        os.rmdir(lock)


def sync_from_vm():
    """Pull new product folders from the bot VM first, so there is no separate sync step to remember."""
    import subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_products.sh")
    if not os.path.isfile(script):
        return
    try:
        # The sync script triggers auto-publish itself; we are already publishing, so tell it not to
        result = subprocess.run([script], capture_output=True, text=True, timeout=120,
                                env={**os.environ, "ECOM_NO_AUTOPUBLISH": "1"})
        new = [l.split(" ", 1)[1].split("/")[0] for l in result.stdout.splitlines() if l.startswith("cd+++")]
        if result.returncode != 0:
            print(f"  sync from VM failed — showing what is already on this Mac ({result.stderr.strip()[-120:]})")
        elif new:
            print(f"  synced from VM: {', '.join(sorted(set(new)))}")
    except Exception as e:
        print(f"  sync from VM failed — showing what is already on this Mac ({e})")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if "--auto" in flags:
        auto_publish()
        return
    if "--no-sync" not in flags:
        sync_from_vm()
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
