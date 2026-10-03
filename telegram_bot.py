#!/usr/bin/env python3
"""Telegram bot for EcomListing Pro — receive product photo, get back a complete listing."""

import asyncio
import glob
import io
import json
import os
import sys
import tempfile
import traceback

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_DIR = os.path.join(SCRIPT_DIR, "products")
sys.path.insert(0, SCRIPT_DIR)

import listing_agent

TOKEN_ENV_VAR = "TELEGRAM_BOT_TOKEN"
BOT_TOKEN = os.environ.get(TOKEN_ENV_VAR)

if not BOT_TOKEN:
    print(f"ERROR: {TOKEN_ENV_VAR} not set in environment.", file=sys.stderr)
    sys.exit(1)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👟 *EcomListing Pro Bot*\n\n"
        "Send me a photo of a product and I'll generate a complete "
        "e-commerce listing for Amazon, Flipkart, and Meesho — "
        "including 4 product images.\n\n"
        "💡 *Tip:* Add a caption to your photo with the product name "
        "or details — I'll use it as a hint for better results.\n\n"
        "Commands:\n"
        "/start — this message\n"
        "/help — usage guide\n"
        "/model — set image source (manual / gemini / pollinations / nvidia / deapi)\n"
        "/republish <slug> — re-export a saved product to the NeedKart folder and republish it without re-generating images\n"
        "/flipkart — check Flipkart API / auth status",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "1. Take or send a clear product photo\n"
        "2. Add a caption with the product name/details for better results\n"
        "3. Wait 2-5 minutes for analysis + image generation\n"
        "4. Receive listing JSON, platform files, and 4 images\n\n"
        "For best results: well-lit, single product, plain background, "
        "descriptive caption.",
    )


async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /model <name>\n\n"
            "Options: manual (default — you make the images from the prompts), gemini, nvidia, deapi, pollinations"
        )
        return
    model = args[0].lower()
    if model not in ("manual", "deapi", "nvidia", "gemini", "pollinations"):
        await update.message.reply_text(
            f"Invalid model '{model}'. Choose: manual, deapi, nvidia, gemini, pollinations"
        )
        return
    context.user_data["image_model"] = model
    await update.message.reply_text(f"Image model set to: {model}")


async def flipkart_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check Flipkart API auth and configuration status."""
    from flipkart_client import FlipkartClient, FLIPKART_SANDBOX
    msg = await update.message.reply_text("🔍 Checking Flipkart status...")
    try:
        fc = FlipkartClient()
        status = fc.status()
        lines = [
            f"🌐 *Environment:* {status['environment']}",
            f"🔑 *App ID set:* {'✅' if status['app_id_set'] else '❌'}",
            f"🔑 *App Secret set:* {'✅' if status['app_secret_set'] else '❌'}",
            f"📍 *Location ID:* `{status['location_id']}`",
            f"🔓 *Authenticated:* {'✅' if status['authenticated'] else '❌'}",
        ]
        if status["auth_error"]:
            lines.append(f"  └─ {status['auth_error']}")
        if status["locations"]:
            locs = status["locations"]
            if isinstance(locs, list):
                lines.append(f"  └─ Found {len(locs)} location(s)")
                for loc in locs[:3]:
                    lines.append(f"     • ID: `{loc.get('id','?')}` — {loc.get('status','?')}")
            elif isinstance(locs, dict):
                lines.append(f"  └─ API response: `{str(locs)[:200]}`")
            else:
                lines.append(f"  └─ `{str(locs)[:200]}`")
        if not status["authenticated"]:
            lines.append("")
            lines.append("⚡ *To fix:*")
            lines.append("1. Go to [Flipkart Seller Dashboard](https://seller.flipkart.com)")
            lines.append("2. *Manage Profile → Developer Access*")
            lines.append("3. Check if the app shows *Approved* or *Pending*")
            lines.append("4. If Pending, contact Flipkart support via Seller Help")
            lines.append("5. Or raise a ticket for *sandbox* access at seller portal")
        await msg.edit_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"❌ Flipkart check error: {e}", parse_mode="Markdown")


async def republish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        existing = [d for d in os.listdir(PRODUCTS_DIR) if os.path.isdir(os.path.join(PRODUCTS_DIR, d)) and d != ".DS_Store"]
        if existing:
            msg = "Usage: /republish <slug>\n\nAvailable products:\n" + "\n".join(f"  /republish {d}" for d in sorted(existing))
        else:
            msg = "Usage: /republish <slug>\n\nNo saved products found."
        await update.message.reply_text(msg)
        return
    slug = args[0].strip("/")
    product_dir = os.path.join(PRODUCTS_DIR, slug)
    listing_path = os.path.join(product_dir, "listing.json")
    if not os.path.exists(listing_path):
        await update.message.reply_text(f"❌ No saved listing found for `{slug}`.\nAvailable: {', '.join(d for d in os.listdir(PRODUCTS_DIR) if os.path.isdir(os.path.join(PRODUCTS_DIR, d)) and d != '.DS_Store')}", parse_mode="Markdown")
        return
    with open(listing_path) as f:
        listing = json.load(f)
    images = _load_existing_images(slug)
    if not images:
        await update.message.reply_text(f"❌ No images found for `{slug}` (need all 4).", parse_mode="Markdown")
        return
    msg = await update.message.reply_text(f"📤 Exporting *{listing['product_analysis']['product_name']}* to NeedKart folder...", parse_mode="Markdown")
    try:
        from local_export import export_product
        nk_dir = export_product(listing, images)
        from needkart_client import NeedKartClient
        needkart_url, product_id = NeedKartClient().publish_listing(listing, images)
        if needkart_url:
            await msg.edit_text(f"✅ *Exported & republished!*\n\n`{nk_dir}`\n[View on NeedKart]({needkart_url})", parse_mode="Markdown")
        else:
            await msg.edit_text(f"⚠️ Exported to `{nk_dir}` but NeedKart publish failed. Check logs.", parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ *Error:* {e}", parse_mode="Markdown")


def _load_existing_images(slug):
    """Check if images already exist for a product slug, return {key: (ext, data)} or None."""
    img_keys = {
        "1_lifestyle_usecase": "lifestyle_usecase",
        "2_before_after": "before_after",
        "3_how_to_use": "how_to_use",
        "4_enhanced_hero": "enhanced_hero",
    }
    product_dir = os.path.join(PRODUCTS_DIR, slug)
    if not os.path.isdir(product_dir):
        return None
    images = {}
    for key, stem in img_keys.items():
        found = False
        for ext in ("png", "jpg", "jpeg"):
            path = os.path.join(product_dir, f"{stem}.{ext}")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    data = f.read()
                images[key] = (ext, data)
                found = True
                break
        if not found:
            return None
    return images if len(images) == 4 else None


async def _manual_flow(update, msg, listing, photo_path):
    """Listing only: save it, export the product folder, and send the image prompts to make by hand."""
    from local_export import export_product
    name = listing['product_analysis']['product_name']
    listing_agent.save_listing(listing, {})
    nk_dir = export_product(listing, {}, photo_path)

    with open(os.path.join(nk_dir, "listing.json"), "rb") as f:
        await update.message.reply_document(document=f, filename="listing.json", caption="📄 Complete listing JSON")
    with open(os.path.join(nk_dir, "image_prompts.md"), "rb") as f:
        await update.message.reply_document(document=f, filename="image_prompts.md", caption="🖼 Image prompts")
    # Plain text so each prompt can be long-pressed and copied
    with open(os.path.join(nk_dir, "image_prompts.md")) as f:
        blocks = f.read().split("## ")[1:]
    for block in blocks:
        title, _, rest = block.partition("\n")
        prompt = rest.split("```")[1].strip() if "```" in rest else rest.strip()
        await update.message.reply_text(prompt)
        await update.message.reply_text(f"⬆️ {title} — send it to ChatGPT on its own, with the product photo attached.")

    slug = os.path.basename(nk_dir)
    await msg.edit_text(
        f"✅ Listing ready: {name}\n\n"
        f"Folder: product/{slug}/\n"
        f"1. Run each prompt above as a separate message (one image each), with the product photo attached\n"
        f"2. Save the images into product/{slug}/images/ on the Mac\n"
        f"3. Publish from the Mac: publish_local.py {slug}\n\n"
        f"Not published yet — waiting for images."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    image_model = context.user_data.get("image_model", "manual")
    caption = update.message.caption
    hint_text = f"\n\n💡 *Hint:* {caption}" if caption else ""
    msg = await update.message.reply_text(
        f"📸 Received photo. Analyzing product...{hint_text} (this may take 2-5 minutes)",
        parse_mode="Markdown",
    )

    photo_file = await update.message.photo[-1].get_file()
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        await photo_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        try:
            listing = listing_agent.analyze_image(tmp_path, product_hint=caption)
        except RuntimeError as e:
            await msg.edit_text(
                f"❌ *Analysis failed:* {e}\n\n"
                f"All AI listing models were unavailable (timeout / rate-limited). "
                f"Please try again later or use `/model` to switch image source.",
                parse_mode="Markdown",
            )
            return

        if image_model == "manual":
            await _manual_flow(update, msg, listing, tmp_path)
            return

        slug = listing_agent.slugify(listing['product_analysis']['product_name'])
        existing_images = _load_existing_images(slug)

        if existing_images:
            images = existing_images
            await msg.edit_text(
                f"✅ Product identified: *{listing['product_analysis']['product_name']}*\n"
                f"📂 Found existing images — skipping generation\n\n"
                f"📤 Exporting to NeedKart folder...",
                parse_mode="Markdown",
            )
        else:
            await msg.edit_text(
                f"✅ Product identified: *{listing['product_analysis']['product_name']}*\n"
                f"Category: {listing['product_analysis']['category']}\n\n"
                f"🖼 Generating 4 images...",
                parse_mode="Markdown",
            )
            images = listing_agent.generate_all_images(listing, tmp_path, image_model)

        await msg.edit_text("📦 Saving and preparing files...")

        out_dir = listing_agent.save_listing(listing, images)

        # Export to local NeedKart product folder
        needkart_dir = None
        try:
            from local_export import export_product
            await msg.edit_text("📤 Exporting to NeedKart folder...")
            needkart_dir = export_product(listing, images)
        except Exception as e:
            print(f"    NeedKart export error: {e}", file=sys.stderr)

        # Publish to NeedKart store
        needkart_url = None
        try:
            from needkart_client import NeedKartClient
            await msg.edit_text("📤 Publishing to NeedKart...")
            needkart_url, product_id = NeedKartClient().publish_listing(listing, images)
        except Exception as e:
            print(f"    NeedKart publish error: {e}", file=sys.stderr)

        # Send listing JSON
        json_path = os.path.join(out_dir, "listing.json")
        with open(json_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="listing.json",
                caption="📄 Complete listing JSON",
            )

        # Send platform files
        for platform in ("amazon", "flipkart", "meesho"):
            path = os.path.join(out_dir, f"{platform}_listing.txt")
            if os.path.exists(path):
                with open(path) as f:
                    text = f.read()
                if len(text) > 4000:
                    with open(path) as f:
                        await update.message.reply_document(
                            document=f,
                            filename=f"{platform}_listing.txt",
                            caption=f"📋 {platform.title()} listing",
                        )
                else:
                    await update.message.reply_text(
                        f"*{platform.title()} Listing*\n\n{text}",
                        parse_mode="Markdown",
                    )

        # Send images
        name_map = {
            "1_lifestyle_usecase": "🏠 Lifestyle / Use Case",
            "2_before_after": "📊 Before & After",
            "3_how_to_use": "📖 How to Use",
            "4_enhanced_hero": "⭐ Enhanced Hero",
        }
        image_count = len(images)
        for key, (ext, data) in images.items():
            label = name_map.get(key, key)
            bio = io.BytesIO(data)
            bio.name = f"{key}.{ext}"
            await update.message.reply_photo(
                photo=bio,
                caption=f"{label}",
            )

        await msg.edit_text(
            f"✅ *Done!*\n\n"
            f"Product: {listing['product_analysis']['product_name']}\n"
            f"Images: {'loaded from cache' if existing_images else f'{image_count}/4 (new)'}\n"
            f"Image source: {'cache' if existing_images else image_model}\n"
            f"Output: `{out_dir}`" +
            (f"\n\n📁 *NeedKart folder:* `{needkart_dir}`" if needkart_dir else "") +
            (f"\n🛒 *NeedKart:* [View Product]({needkart_url})" if needkart_url else ""),
            parse_mode="Markdown",
        )

    except Exception as e:
        tb = traceback.format_exc()
        await msg.edit_text(
            f"❌ *Error:* {e}\n\n```\n{tb[:1500]}\n```",
            parse_mode="Markdown",
        )
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("model", set_model))
    app.add_handler(CommandHandler("republish", republish))
    app.add_handler(CommandHandler("flipkart", flipkart_status))
    app.add_handler(CommandHandler("flipkart_status", flipkart_status))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("🤖 EcomListing Pro Telegram Bot is running...", file=sys.stderr)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
