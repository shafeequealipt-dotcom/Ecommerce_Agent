#!/usr/bin/env python3
"""Flipkart Seller API v3 client — auth, check listing, create/update listing."""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import base64

FLIPKART_SANDBOX = os.environ.get("FLIPKART_SANDBOX", "").lower() in ("1", "true", "yes")

if FLIPKART_SANDBOX:
    API_BASE = "https://sandbox-api.flipkart.net"
else:
    API_BASE = "https://api.flipkart.net"

AUTH_URL = f"{API_BASE}/oauth-service/oauth/token"
SELLER_BASE = f"{API_BASE}/sellers"

FLIPKART_APP_ID = os.environ.get("FLIPKART_APP_ID", "")
FLIPKART_APP_SECRET = os.environ.get("FLIPKART_APP_SECRET", "")
FLIPKART_LOCATION_ID = os.environ.get("FLIPKART_LOCATION_ID", "")


def _json_req(url, data=None, method="GET", token=None, headers_in=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if headers_in:
        headers.update(headers_in)
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
            return {"_http_error": e.code, "_error": err[:300]}
    except Exception as e:
        return {"_error": str(e)}


class FlipkartClient:
    def __init__(self):
        self.token = None
        self.token_expiry = 0

    # ── Auth ────────────────────────────────────────────────────────────────

    def login(self):
        if not FLIPKART_APP_ID or not FLIPKART_APP_SECRET:
            print("    Flipkart: FLIPKART_APP_ID / FLIPKART_APP_SECRET not set", file=sys.stderr)
            return False

        creds = base64.b64encode(f"{FLIPKART_APP_ID}:{FLIPKART_APP_SECRET}".encode()).decode()
        url = f"{AUTH_URL}?grant_type=client_credentials&scope=Seller_Api"
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"    Flipkart: auth failed — HTTP {e.code}: {body[:200]}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"    Flipkart: auth error — {e}", file=sys.stderr)
            return False

        self.token = result.get("access_token")
        if not self.token:
            print(f"    Flipkart: no access_token in response", file=sys.stderr)
            return False

        expires = result.get("expires_in", 5184000)
        self.token_expiry = time.time() + expires - 120
        print(f"    Flipkart: logged in (token valid {expires//86400}d)", file=sys.stderr)
        return True

    def ensure_login(self):
        if not self.token or time.time() >= self.token_expiry:
            return self.login()
        return True

    # ── Check listing by SKU ───────────────────────────────────────────────

    def check_listing(self, sku):
        """Check if a listing exists for this SKU. Returns dict or None."""
        if not self.ensure_login():
            return None
        result = _json_req(f"{SELLER_BASE}/skus/{sku}/listings", token=self.token)
        if result.get("listingId"):
            return result
        return None

    # ── Create listing ─────────────────────────────────────────────────────

    def build_listing_payload(self, listing, sku, product_id=None):
        """Build the Flipkart listing payload from our listing.json data."""
        analysis = listing.get("product_analysis", {})
        fields = listing.get("listing_fields", {})

        title = fields.get("product_title", analysis.get("product_name", "Product"))
        nums_price = re.findall(r'\d+\.?\d*', str(fields.get("selling_price", "499")).replace(",", ""))
        price = int(float(nums_price[0])) if nums_price else 499
        nums_mrp = re.findall(r'\d+\.?\d*', str(fields.get("mrp", "999")).replace(",", ""))
        mrp = int(float(nums_mrp[0])) if nums_mrp else price * 2
        hsn = fields.get("hsn_code", "")
        tax_code = _gst_to_taxcode(fields.get("gst_rate", ""))
        weight_raw = str(fields.get("weight", "200")).lower().replace("g", "").replace("gm", "").replace("gram", "").replace("kg", "").strip()
        nums = re.findall(r'\d+\.?\d*', weight_raw)
        weight_g = int(float(nums[0])) if nums else 200
        stock_raw = str(fields.get("stock_quantity", "100"))
        stock_nums = re.findall(r'\d+', stock_raw)
        stock = int(stock_nums[0]) if stock_nums else 100
        mfg_date = fields.get("manufacturing_date", "")
        shelf_life = fields.get("shelf_life", "")

        manufacturer = fields.get("manufacturer_details", title)
        packer = fields.get("packer_details", manufacturer)
        importer = fields.get("importer_details", "")
        country = fields.get("country_of_origin", "India")
        country_code = {"India": "IN", "China": "CN", "USA": "US"}.get(country, "IN")

        dimensions = fields.get("package_dimensions", "")
        dims = re.findall(r'\d+\.?\d*', dimensions.replace(",", "."))
        if len(dims) >= 3:
            length, breadth, height = float(dims[0]), float(dims[1]), float(dims[2])
        else:
            length, breadth, height = 15, 10, 5

        payload = {
            "product_id": product_id or "",
            "price": {
                "mrp": mrp,
                "selling_price": price,
                "currency": "INR",
            },
            "tax": {
                "hsn": hsn,
                "tax_code": tax_code,
            },
            "listing_status": "ACTIVE",
            "shipping_fees": {
                "local": 0,
                "zonal": 0,
                "national": 0,
                "currency": "INR",
            },
            "fulfillment_profile": "NON_FBF",
            "fulfillment": {
                "dispatch_sla": 2,
                "shipping_provider": "SELLER",
                "procurement_type": "REGULAR",
            },
            "packages": [{
                "name": "default",
                "dimensions": {
                    "length": length,
                    "breadth": breadth,
                    "height": height,
                },
                "weight": weight_g / 1000.0 if weight_g > 100 else weight_g,
                "description": "",
            }],
            "locations": [{
                "id": FLIPKART_LOCATION_ID or "default",
                "status": "ENABLED",
                "inventory": stock,
            }],
            "address_label": {
                "manufacturer_details": [manufacturer],
                "packer_details": [packer],
                "countries_of_origin": [country_code],
            },
        }

        if importer:
            payload["address_label"]["importer_details"] = [importer]

        if mfg_date or shelf_life:
            dating = {}
            if mfg_date:
                try:
                    from datetime import datetime
                    dt = datetime.strptime(mfg_date[:10], "%Y-%m-%d")
                    dating["mfg_date"] = int(dt.timestamp())
                except ValueError:
                    pass
            if shelf_life:
                years = 0
                parts = shelf_life.lower().split()
                for i, p in enumerate(parts):
                    if "year" in p:
                        try:
                            years = int(parts[i - 1])
                        except (ValueError, IndexError):
                            years = 2
                    elif "month" in p:
                        try:
                            years = int(parts[i - 1]) / 12.0
                        except (ValueError, IndexError):
                            years = 2
                if years > 0:
                    dating["shelf_life"] = int(years * 365.25 * 86400)
            if dating:
                payload["dating_label"] = dating

        return payload

    def create_listing(self, sku, listing_data):
        """Create or overwrite a listing for one SKU."""
        if not self.ensure_login():
            return None
        payload = {sku: listing_data}
        result = _json_req(
            f"{SELLER_BASE}/listings/v3",
            data=payload, method="POST", token=self.token, timeout=60
        )
        return result

    def create_or_update_listing(self, listing, sku, product_id=None):
        """Check if exists → update or create. Returns result dict."""
        existing = self.check_listing(sku)
        payload = self.build_listing_payload(listing, sku, product_id)

        if existing:
            listing_id = existing.get("listingId")
            if listing_id:
                result = _json_req(
                    f"{SELLER_BASE}/skus/listings/{listing_id}",
                    data={"listingId": listing_id, "skuId": sku,
                          "attributeValues": {
                              "mrp": str(payload["price"]["mrp"]),
                              "selling_price": str(payload["price"]["selling_price"]),
                              "stock_count": str(payload["locations"][0]["inventory"]),
                              "procurement_sla": str(payload["fulfillment"]["dispatch_sla"]),
                              "listing_status": "ACTIVE",
                          }},
                    method="POST", token=self.token, timeout=30
                )
                print(f"    Flipkart: updated listing {listing_id}", file=sys.stderr)
                return result

        result = self.create_listing(sku, payload)
        if result and result.get(sku, {}).get("status") == "success":
            print(f"    Flipkart: created listing for SKU {sku}", file=sys.stderr)
        else:
            err = result.get(sku, result) if result else {"_error": "no response"}
            print(f"    Flipkart: create listing result — {json.dumps(err)[:200]}", file=sys.stderr)
        return result

    # ── Update price / inventory (simpler endpoints) ───────────────────────

    def update_price(self, sku, selling_price, mrp=None):
        """Update selling price for a listing."""
        if not self.ensure_login():
            return None
        payload = {sku: {"selling_price": selling_price}}
        if mrp:
            payload[sku]["mrp"] = mrp
        return _json_req(
            f"{SELLER_BASE}/listings/v3/update/price",
            data=payload, method="POST", token=self.token
        )

    def update_inventory(self, sku, stock, location_id=None):
        """Update stock count at a location."""
        if not self.ensure_login():
            return None
        payload = {sku: {"inventory": stock, "location_id": location_id or FLIPKART_LOCATION_ID or "default"}}
        return _json_req(
            f"{SELLER_BASE}/listings/v3/update/inventory",
            data=payload, method="POST", token=self.token
        )

    # ── Status / diagnostics ──────────────────────────────────────────────

    def status(self):
        """Return a dict with auth status, environment, and config state.

        Does NOT require a successful login — designed for diagnostics.
        """
        result = {
            "environment": "sandbox" if FLIPKART_SANDBOX else "production",
            "app_id_set": bool(FLIPKART_APP_ID),
            "app_secret_set": bool(FLIPKART_APP_SECRET),
            "location_id": FLIPKART_LOCATION_ID or "not set (will use 'default')",
            "authenticated": False,
            "auth_error": None,
            "locations": None,
        }

        if not FLIPKART_APP_ID or not FLIPKART_APP_SECRET:
            result["auth_error"] = "FLIPKART_APP_ID or FLIPKART_APP_SECRET not set"
            return result

        ok = self.login()
        result["authenticated"] = ok
        if not ok:
            result["auth_error"] = "Login failed — app may not be approved yet"
            return result

        result["locations"] = self.get_locations()
        return result

    def get_locations(self):
        """Try to fetch location IDs from an existing listing (if any).

        Returns a list of location IDs found in existing listings,
        or an informational message.
        """
        if not self.ensure_login():
            return "auth required"

        # We need at least one existing SKU to extract locations.
        # Since we don't have one yet, return a message.
        return ("No listings exist yet. "
                "Location ID must be obtained from Seller Dashboard → "
                "Manage Profile → Locations, or set FLIPKART_LOCATION_ID in .env")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _gst_to_taxcode(gst_str):
    """Map GST rate string → Flipkart tax_code."""
    if not gst_str:
        return "GST_18"
    gst_str = str(gst_str).strip().replace("%", "").lower()
    if "apparel" in gst_str:
        return "GST_APPAREL"
    if "footwear" in gst_str:
        return "GST_Footwear"
    try:
        rate = float(gst_str.split()[0])
    except (ValueError, IndexError):
        return "GST_18"
    mapping = {0: "GST_0", 3: "GST_3", 5: "GST_5", 12: "GST_12", 18: "GST_18", 28: "GST_28"}
    return mapping.get(rate, "GST_18")


def generate_bulk_csv(listing, sku):
    """Generate a Flipkart bulk upload CSV string (for manual upload)."""
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
    full_desc = f"{desc}\n\n{bullet_text}".strip()

    csv = "FSN,SKU,Title,Description,MRP,Selling Price,Stock,HSN,GST,Image URLs\n"
    img_urls = "|".join(listing.get("image_urls", []))
    csv += f",{sku},{title},{full_desc.replace(chr(34), chr(39))},{mrp},{price},{stock},{hsn},{gst},{img_urls}\n"
    return csv


def search_existing_product(query):
    """Search Flipkart product catalog for a product_id by title/description.
    
    NOTE: Flipkart doesn't expose a public product search API for sellers.
    This is a placeholder — you'll need to provide the FSN manually or
    create the product via the Flipkart Seller Dashboard first.
    """
    print(f"    Flipkart: searching catalog for '{query}' — not available via API", file=sys.stderr)
    print(f"    Flipkart: please provide FSN manually or create product in Seller Dashboard", file=sys.stderr)
    return None
