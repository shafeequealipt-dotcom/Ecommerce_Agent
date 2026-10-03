#!/usr/bin/env python3
"""Marketplace publisher — orchestrates publishing to Flipkart, Amazon, Meesho."""

import json
import os
import sys
import time

from listing_agent import slugify


class MarketplacePublisher:
    """Publishes a listing to multiple e-commerce platforms with dedup."""

    def __init__(self):
        self.results = {}

    # ── Dedup: check if product already listed ──────────────────────────────

    def already_listed_on(self, platform, sku):
        """Check if the SKU is already listed on a given platform.

        Returns dict with listing info if exists, None otherwise.
        Subclasses/imports handle platform-specific calls.
        """
        if platform == "flipkart":
            return self._check_flipkart(sku)
        if platform == "amazon":
            return self._check_amazon(sku)
        return None

    def _check_flipkart(self, sku):
        try:
            from flipkart_client import FlipkartClient
            fc = FlipkartClient()
            if not fc.ensure_login():
                return None
            return fc.check_listing(sku)
        except Exception as e:
            print(f"    Flipkart check error: {e}", file=sys.stderr)
            return None

    def _check_amazon(self, sku):
        # Placeholder for Amazon SP-API
        return None

    # ── Publish to single platform ──────────────────────────────────────────

    def publish_to_flipkart(self, listing, sku, product_id=None):
        """Publish a listing to Flipkart. Returns (success, message)."""
        from flipkart_client import FlipkartClient

        fc = FlipkartClient()
        if not fc.ensure_login():
            return False, "Flipkart auth failed"

        existing = fc.check_listing(sku)
        if existing:
            listing_id = existing.get("listingId", "")
            print(f"    Flipkart: SKU {sku} already listed (id={listing_id})", file=sys.stderr)

            payload = fc.build_listing_payload(listing, sku, product_id)
            attr = {
                "listingId": listing_id,
                "skuId": sku,
                "attributeValues": {
                    "mrp": str(payload["price"]["mrp"]),
                    "selling_price": str(payload["price"]["selling_price"]),
                    "stock_count": str(payload["locations"][0]["inventory"]),
                    "procurement_sla": str(payload["fulfillment"]["dispatch_sla"]),
                    "listing_status": "ACTIVE",
                },
            }
            result = fc.update_listing(listing_id, attr)
            if result.get("status") == "success":
                return True, f"Updated on Flipkart (SKU: {sku})"
            return False, f"Flipkart update failed: {json.dumps(result)[:200]}"

        if not product_id:
            csv = generate_flipkart_csv(listing, sku)
            csv_path = f"products/{slugify(listing.get('product_analysis', {}).get('product_name', 'product'))}/flipkart_bulk.csv"
            os.makedirs(os.path.dirname(csv_path), exist_ok=True)
            with open(csv_path, "w") as f:
                f.write(csv)
            print(f"    Flipkart: no FSN — saved bulk CSV to {csv_path}", file=sys.stderr)

            msg = (
                f"Flipkart listing requires a Product ID (FSN).\n"
                f"1. Create product at seller.flipkart.com → get FSN\n"
                f"2. Re-run with: marketplace_publisher.py --flipkart-fsn=<FSN>\n"
                f"Or upload the CSV manually: {csv_path}"
            )
            return False, msg

        result = fc.create_listing(sku, fc.build_listing_payload(listing, sku, product_id))
        if result and result.get(sku, {}).get("status") == "success":
            return True, f"Listed on Flipkart (SKU: {sku})"
        err = result.get(sku, result) if result else {"_error": "no response"}
        return False, f"Flipkart create failed: {json.dumps(err)[:300]}"

    def publish_to_amazon(self, listing, sku):
        """Publish a listing to Amazon. Placeholder for future implementation."""
        return False, "Amazon SP-API not yet implemented"

    def publish_to_meesho(self, listing, sku):
        """Generate Meesho bulk upload CSV. Placeholder for future implementation."""
        csv = _generate_meesho_csv(listing, sku)
        slug = slugify(listing.get("product_analysis", {}).get("product_name", "product"))
        path = f"products/{slug}/meesho_bulk.csv"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(csv)
        return False, f"Meesho has no API. CSV saved to {path} — upload manually via supplier.meesho.com"

    # ── Publish to all configured platforms ─────────────────────────────────

    def publish_all(self, listing, sku, platforms=None, flipkart_fsn=None):
        """Publish to specified platforms. platforms=None = all available."""
        if platforms is None:
            platforms = ["flipkart"]

        self.results = {}
        for p in platforms:
            if p == "flipkart":
                ok, msg = self.publish_to_flipkart(listing, sku, flipkart_fsn)
            elif p == "amazon":
                ok, msg = self.publish_to_amazon(listing, sku)
            elif p == "meesho":
                ok, msg = self.publish_to_meesho(listing, sku)
            else:
                ok, msg = False, f"Unknown platform: {p}"
            self.results[p] = {"success": ok, "message": msg}
            time.sleep(1)

        return self.results

    def summary(self):
        """Return a human-readable summary string."""
        lines = []
        for platform, r in self.results.items():
            icon = "\u2705" if r["success"] else "\u274c"
            lines.append(f"  {icon} {platform.title()}: {r['message']}")
        return "\n".join(lines)


# ── CSV generators for platforms without APIs ──────────────────────────────

def generate_flipkart_csv(listing, sku):
    """Generate Flipkart bulk upload CSV (for manual upload via Seller Hub)."""
    analysis = listing.get("product_analysis", {})
    fields = listing.get("listing_fields", {})

    title = fields.get("product_title", analysis.get("product_name", "Product"))
    price = fields.get("selling_price", "499")
    mrp = fields.get("mrp", "999")
    stock = fields.get("stock_quantity", "100")
    hsn = fields.get("hsn_code", "")
    gst = fields.get("gst_rate", "")
    desc = fields.get("description", "")
    bullets = fields.get("bullet_points", [])
    bullet_text = "\n".join(f"- {b}" for b in bullets if b)
    full_desc = f"{desc}\n\n{bullet_text}".strip().replace('"', "'")

    img_urls = "|".join(listing.get("image_urls", []))

    header = "FSN,SKU,Title,Description,MRP,Selling Price,Stock,HSN,GST,Image URLs\n"
    row = f",{sku},{title},{full_desc},{mrp},{price},{stock},{hsn},{gst},{img_urls}\n"
    return header + row


def _generate_meesho_csv(listing, sku):
    """Generate Meesho bulk upload CSV."""
    analysis = listing.get("product_analysis", {})
    fields = listing.get("listing_fields", {})

    title = fields.get("product_title", analysis.get("product_name", "Product"))
    price = fields.get("selling_price", "499")
    mrp = fields.get("mrp", "999")
    stock = fields.get("stock_quantity", "100")
    desc = fields.get("description", "").replace('"', "'")

    header = "SKU,Title,MRP,Selling Price,Stock,Description\n"
    row = f"{sku},{title},{mrp},{price},{stock},{desc}\n"
    return header + row
