#!/usr/bin/env python3
"""NeedKart Admin API client — login, upload images, create products, set stock."""

import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = (
    os.environ.get("NEEDKART_API_BASE") or
    os.environ.get("NeedKart_Api_Baseurl") or
    ""
)
ADMIN_EMAIL = (
    os.environ.get("ADMIN_EMAIL") or
    os.environ.get("NeedKart_Api_Username") or
    ""
)
ADMIN_PASSWORD = (
    os.environ.get("ADMIN_PASSWORD") or
    os.environ.get("NeedKart_Api_Password") or
    os.environ.get("Needkart_API_Password") or
    ""
)

CREDENTIALS_OK = bool(BASE and ADMIN_EMAIL and ADMIN_PASSWORD)


def _json_req(url, data=None, method="POST", token=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        try:
            return json.loads(err)
        except json.JSONDecodeError:
            return {"_http_error": e.code, "_error": err[:200]}
    except Exception as e:
        return {"_error": str(e)}


class NeedKartClient:
    """Thin wrapper around the NeedKart / Medusa admin API."""

    def __init__(self):
        self.token = None
        self.prereqs = {}
        self.logged_in = False

    # ── Auth ────────────────────────────────────────────────────────────────

    def login(self):
        if not CREDENTIALS_OK:
            print("    NeedKart: credentials not set (NEEDKART_API_BASE, ADMIN_EMAIL, ADMIN_PASSWORD)", file=sys.stderr)
            return False
        result = _json_req(f"{BASE}/auth/user/emailpass", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        token = result.get("token")
        if not token:
            msg = result.get("message", result.get("_error", "unknown"))
            print(f"    NeedKart: login failed — {msg}", file=sys.stderr)
            return False
        self.token = token
        self.logged_in = True
        print(f"    NeedKart: logged in (token {token[:12]}...)", file=sys.stderr)
        return True

    # ── Prerequisites (fetch once, cache) ───────────────────────────────────

    def _fetch_prereqs(self):
        if self.prereqs:
            return self.prereqs
        if not self.token:
            return {}

        def get(path, key, label="id"):
            result = _json_req(f"{BASE}{path}", method="GET", token=self.token)
            items = result.get(key, [])
            if label == "id":
                return {item.get("name", "?"): item["id"] for item in items if "id" in item}
            return items

        self.prereqs = {
            "sales_channels": get("/admin/sales-channels", "sales_channels"),
            "shipping_profiles": get("/admin/shipping-profiles", "shipping_profiles"),
            "regions": get("/admin/regions", "regions"),
            "categories": get("/admin/product-categories", "product_categories"),
            "stock_locations": get("/admin/stock-locations", "stock_locations"),
        }
        print(f"    NeedKart: fetched prereqs — {len(self.prereqs.get('categories', {}))} categories, "
              f"{len(self.prereqs.get('sales_channels', {}))} channels", file=sys.stderr)
        return self.prereqs

    # ── Upload an image → returns URL ───────────────────────────────────────

    def upload_image(self, image_data, filename="product.png"):
        """Upload binary image data, return hosted URL."""
        if not self.token:
            return None

        boundary = "----NeedKartUpload" + hex(int(time.time() * 1e6))[2:]
        body = []
        body.append(f"--{boundary}\r\n".encode())
        body.append(
            f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'.encode()
        )
        body.append(b"Content-Type: image/png\r\n\r\n")
        body.append(image_data)
        body.append(b"\r\n")
        body.append(f"--{boundary}--\r\n".encode())

        req = urllib.request.Request(
            f"{BASE}/admin/uploads",
            data=b"".join(body),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode()[:200]
            print(f"    NeedKart: upload failed — HTTP {e.code}: {err}", file=sys.stderr)
            return None

        files = result.get("files", [])
        if not files:
            print(f"    NeedKart: upload returned no files", file=sys.stderr)
            return None
        url = files[0].get("url", "")
        if url:
            print(f"    NeedKart: uploaded -> {url}", file=sys.stderr)
        return url

    # ── Create product ──────────────────────────────────────────────────────

    def create_product(self, listing, image_urls):
        """Create a product on NeedKart from the listing JSON + image URLs.

        Returns the created product dict, or None on failure.
        """
        prereqs = self._fetch_prereqs()
        if not prereqs:
            return None

        analysis = listing.get("product_analysis", {})
        fields = listing.get("listing_fields", {})

        category_name = analysis.get("category", "")

        # Map AI-detected categories to NeedKart categories
        CATEGORY_MAP = {
            "shoe": "Shoe Care",
            "footwear": "Shoe Care",
            "shoe care": "Shoe Care",
            "kitchen": "Kitchen",
            "car": "Car Care",
            "automotive": "Car Care",
            "automobile": "Car Care",
            "health": "Shoe Care",
            "personal care": "Shoe Care",
            "baby": "Shoe Care",
            "oral care": "Shoe Care",
            "home": "Kitchen",
        }
        category_id = prereqs.get("categories", {}).get(category_name)
        if not category_id:
            for name, cid in prereqs.get("categories", {}).items():
                if category_name.lower() in name.lower():
                    category_id = cid
                    break
        if not category_id:
            cat_lower = category_name.lower()
            for key, mapped in CATEGORY_MAP.items():
                if key in cat_lower:
                    category_id = prereqs.get("categories", {}).get(mapped)
                    if category_id:
                        print(f"    NeedKart: mapped '{category_name}' -> '{mapped}'", file=sys.stderr)
                        break
        if not category_id:
            print(f"    NeedKart: no category match for '{category_name}'", file=sys.stderr)
            return None

        sales_channel_id = None
        for name, sid in prereqs.get("sales_channels", {}).items():
            sales_channel_id = sid
            break
        if not sales_channel_id:
            print(f"    NeedKart: no sales channel found", file=sys.stderr)
            return None

        shipping_profile_id = None
        for name, spid in prereqs.get("shipping_profiles", {}).items():
            shipping_profile_id = spid
            break

        title = fields.get("product_title", analysis.get("product_name", "Product"))
        handle = slugify(title)[:60]
        description = fields.get("description", "")
        price_str = fields.get("selling_price", "499").replace(",", "").replace("₹", "")
        mrp_str = fields.get("mrp", "999").replace(",", "").replace("₹", "")
        try:
            price = int(float(price_str))
        except ValueError:
            price = 499
        try:
            mrp = int(float(mrp_str))
        except ValueError:
            mrp = price * 2

        sku = fields.get("sku", f"NK-{handle.upper()}")
        weight = fields.get("weight", "200")
        try:
            weight = int(weight.replace("g", "").replace("G", ""))
        except ValueError:
            weight = 200

        bullet_1 = fields.get("bullet_points", ["", "", "", "", ""])
        bullet_text = "\n".join(f"- {b}" for b in bullet_1 if b)

        payload = {
            "title": title,
            "handle": handle,
            "description": f"{description}\n\n{bullet_text}".strip(),
            "status": "published",
            "category_ids": [category_id],
            "weight": weight,
            "metadata": {"mrp": mrp},
            "options": [{"title": "Size", "values": ["Standard"]}],
            "variants": [{
                "title": "Standard",
                "sku": sku,
                "manage_inventory": True,
                "options": {"Size": "Standard"},
                "prices": [{"amount": price, "currency_code": "inr"}],
            }],
            "sales_channels": [{"id": sales_channel_id}],
            "images": [{"url": url} for url in image_urls],
        }
        if shipping_profile_id:
            payload["shipping_profile_id"] = shipping_profile_id

        result = _json_req(f"{BASE}/admin/products", payload, token=self.token, timeout=60)
        if result.get("product"):
            prod = result["product"]
            print(f"    NeedKart: created product '{prod.get('title', '?')}' (id={prod.get('id', '?')[:12]}...)", file=sys.stderr)
            return prod
        if result.get("_http_error"):
            print(f"    NeedKart: create product failed — HTTP {result['_http_error']}: {result.get('_error', '')}", file=sys.stderr)
        else:
            err = result.get("message", json.dumps(result)[:200])
            print(f"    NeedKart: create product failed — {err}", file=sys.stderr)
        return None

    # ── Set inventory ───────────────────────────────────────────────────────

    def set_inventory(self, product, quantity=1000):
        """Set stock for the first variant's inventory item."""
        if not self.token:
            return False
        product_id = product.get("id") if isinstance(product, dict) else product
        if not product_id:
            return False

        # Fetch variant inventory item
        result = _json_req(
            f"{BASE}/admin/products/{product_id}?fields=*variants.inventory_items",
            method="GET",
            token=self.token,
        )
        prod = result.get("product", {})
        variants = prod.get("variants", [])
        inv_item_id = None
        for v in variants:
            invs = v.get("inventory_items", [])
            if invs:
                inv_item_id = invs[0].get("id")
                break
        if not inv_item_id:
            print(f"    NeedKart: no inventory item found for product", file=sys.stderr)
            return False

        prereqs = self._fetch_prereqs()
        location_id = None
        for name, lid in prereqs.get("stock_locations", {}).items():
            location_id = lid
            break
        if not location_id:
            print(f"    NeedKart: no stock location found", file=sys.stderr)
            return False

        result = _json_req(
            f"{BASE}/admin/inventory-items/{inv_item_id}/location-levels",
            {"location_id": location_id, "stocked_quantity": quantity},
            token=self.token,
            timeout=30,
        )
        if result.get("inventory_item"):
            print(f"    NeedKart: set stock = {quantity} units", file=sys.stderr)
            return True
        print(f"    NeedKart: set stock failed — {result.get('message', '?')}", file=sys.stderr)
        return False

    # ── Full publish pipeline ───────────────────────────────────────────────

    def publish_listing(self, listing, images):
        """Full flow: login → upload images → create product → set stock.

        Returns (product_url, product_id) or (None, None).
        """
        if not self.login():
            return None, None

        image_urls = []
        name_map = {
            "1_lifestyle_usecase": "lifestyle_usecase.png",
            "2_before_after": "before_after.png",
            "3_how_to_use": "how_to_use.png",
            "4_enhanced_hero": "enhanced_hero.png",
        }
        for key, (ext, data) in sorted(images.items()):
            filename = name_map.get(key, f"{key}.{ext}")
            url = self.upload_image(data, filename)
            if url:
                image_urls.append(url)

        if not image_urls:
            print(f"    NeedKart: no images uploaded — creating product without images", file=sys.stderr)

        product = self.create_product(listing, image_urls)
        if not product:
            return None, None

        self.set_inventory(product)

        product_id = product.get("id", "")
        handle = product.get("handle", "")
        store_url = BASE.replace("api.", "").replace("://api", "://")
        product_url = f"{store_url}/products/{handle}" if handle else ""
        print(f"    NeedKart: published -> {product_url}", file=sys.stderr)
        return product_url, product_id


def slugify(text):
    s = text.lower().strip()
    for ch in " -/\\":
        s = s.replace(ch, "-")
    s = "".join(c for c in s if c.isalnum() or c == "-")
    return s.strip("-")[:60]
