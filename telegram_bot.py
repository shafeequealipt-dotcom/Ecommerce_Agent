#!/usr/bin/env python3
"""Telegram bot for EcomListing Pro — receive product photo, get back a complete listing."""

import asyncio
import io
import json
import os
import sys
import tempfile
import traceback

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
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
        "/model — set image model (gemini / pollinations / nvidia / deapi)",
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
            "Options: deapi (default), nvidia, gemini, pollinations"
        )
        return
    model = args[0].lower()
    if model not in ("deapi", "nvidia", "gemini", "pollinations"):
        await update.message.reply_text(
            f"Invalid model '{model}'. Choose: deapi, nvidia, gemini, pollinations"
        )
        return
    context.user_data["image_model"] = model
    await update.message.reply_text(f"Image model set to: {model}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    image_model = context.user_data.get("image_model", "gemini")
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
        listing = listing_agent.analyze_image(tmp_path, product_hint=caption)

        await msg.edit_text(
            f"✅ Product identified: *{listing['product_analysis']['product_name']}*\n"
            f"Category: {listing['product_analysis']['category']}\n\n"
            f"🖼 Generating 4 images...",
            parse_mode="Markdown",
        )

        images = listing_agent.generate_all_images(listing, tmp_path, image_model)

        await msg.edit_text("📦 Saving and preparing files...")

        out_dir = listing_agent.save_listing(listing, images)

        # Publish to NeedKart
        needkart_url = None
        try:
            from needkart_client import NeedKartClient
            client = NeedKartClient()
            await msg.edit_text("📤 Publishing to NeedKart...")
            needkart_url, product_id = client.publish_listing(listing, images)
            if needkart_url:
                await msg.edit_text(f"✅ Published!\n{needkart_url}")
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
            f"Images generated: {image_count}/4\n"
            f"Image source: {image_model}\n"
            f"Output: `{out_dir}`" +
            (f"\n\n🛒 *NeedKart:* [View Product]({needkart_url})" if needkart_url else ""),
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
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("🤖 EcomListing Pro Telegram Bot is running...", file=sys.stderr)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
