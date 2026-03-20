"""
TheWaterMarkBot — Production-ready Telegram watermark bot
==========================================================
Features
--------
• Text watermark  — any string, white + shadow, readable on any background
• Image watermark — user sends their logo (as File for best quality)
• 9-position grid — 3×3 interactive keyboard
• Opacity control — 25 / 50 / 75 / 100 %
• Size control    — Small / Medium / Large
• Presets         — save up to 3 named configs, reuse with one tap
• Batch mode      — watermark up to 10 photos in one go
• /mystats        — personal photo count + saved presets
• /admin_stats    — owner-only: total users & photos (set ADMIN_ID)
• /reset          — clear your saved presets
• /help           — full command reference
• /cancel         — abort at any step

Setup
-----
1.  pip install -r requirements.txt
2.  export BOT_TOKEN="your_token_from_BotFather"
3.  export ADMIN_ID="your_numeric_telegram_id"   # optional
4.  python bot.py
"""

import os
import io
import json
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ── Configuration ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0"))
DATA_FILE = Path("user_data.json")
PADDING   = 22  # px gap from image edge

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
(
    WAITING_PHOTO,
    WAITING_BATCH_PHOTOS,
    WAITING_PRESET_CHOICE,
    WAITING_WM_TYPE,
    WAITING_WM_TEXT,
    WAITING_WM_IMAGE,
    WAITING_OPACITY,
    WAITING_SIZE,
    WAITING_POSITION,
    WAITING_PRESET_SAVE,
    WAITING_PRESET_NAME,
) = range(11)

# ── Lookup tables ──────────────────────────────────────────────────────────────
POSITIONS = {
    "top_left":     "↖ Top-Left",
    "top_center":   "⬆ Top-Center",
    "top_right":    "↗ Top-Right",
    "middle_left":  "◀ Mid-Left",
    "center":       "✚ Center",
    "middle_right": "▶ Mid-Right",
    "bottom_left":  "↙ Bot-Left",
    "bottom_center":"⬇ Bot-Center",
    "bottom_right": "↘ Bot-Right",
}
POSITIONS_FULL = {
    "top_left": "Top-Left", "top_center": "Top-Center", "top_right": "Top-Right",
    "middle_left": "Middle-Left", "center": "Center", "middle_right": "Middle-Right",
    "bottom_left": "Bottom-Left", "bottom_center": "Bottom-Center", "bottom_right": "Bottom-Right",
}
OPACITIES   = {"25": "25%", "50": "50%", "75": "75%", "100": "100%"}
SIZES       = {"small": "🔹 Small", "medium": "🔶 Medium", "large": "🔷 Large"}
SIZE_RATIOS = {"small": 0.08, "medium": 0.14, "large": 0.22}

# ── Persistent storage (simple JSON file) ─────────────────────────────────────
def _load() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def get_user(uid: int) -> dict:
    return _load().get(str(uid), {"count": 0, "presets": {}})

def update_user(uid: int, user: dict) -> None:
    data = _load()
    data[str(uid)] = user
    _save(data)

def inc_count(uid: int) -> None:
    u = get_user(uid)
    u["count"] = u.get("count", 0) + 1
    update_user(uid, u)

# ── Keyboard builders ──────────────────────────────────────────────────────────
def kb_wm_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Text watermark",  callback_data="wm_text"),
        InlineKeyboardButton("🖼 Image / Logo",    callback_data="wm_image"),
    ]])

def kb_opacity() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(v, callback_data=f"op_{k}")
        for k, v in OPACITIES.items()
    ]])

def kb_size() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(v, callback_data=f"sz_{k}")]
        for k, v in SIZES.items()
    ])

def kb_position() -> InlineKeyboardMarkup:
    keys = list(POSITIONS.keys())
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(POSITIONS[k], callback_data=f"pos_{k}") for k in keys[i:i+3]]
        for i in range(0, 9, 3)
    ])

def kb_presets(uid: int) -> InlineKeyboardMarkup:
    presets = get_user(uid).get("presets", {})
    rows = [[InlineKeyboardButton(f"📌 {n}", callback_data=f"preset_{n}")] for n in presets]
    rows += [
        [InlineKeyboardButton("➕ Configure new watermark", callback_data="preset_new")],
        [InlineKeyboardButton("⏭ Skip presets",             callback_data="preset_skip")],
    ]
    return InlineKeyboardMarkup(rows)

def kb_save_preset() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💾 Save as preset", callback_data="do_save"),
        InlineKeyboardButton("⏭ Skip",            callback_data="no_save"),
    ]])

def kb_batch_done() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done — watermark all!", callback_data="batch_done"),
        InlineKeyboardButton("❌ Cancel",                callback_data="batch_cancel"),
    ]])

# ── Watermark rendering engine ─────────────────────────────────────────────────
def _find_font(size: int) -> ImageFont.FreeTypeFont:
    """Try system font paths in order; fall back to Pillow default."""
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()

def _anchor_xy(pos: str, iw: int, ih: int, ww: int, wh: int) -> tuple:
    """Return (x, y) top-left corner for the watermark given the position key."""
    cx, cy = (iw - ww) // 2, (ih - wh) // 2
    return {
        "top_left":     (PADDING, PADDING),
        "top_center":   (cx, PADDING),
        "top_right":    (iw - ww - PADDING, PADDING),
        "middle_left":  (PADDING, cy),
        "center":       (cx, cy),
        "middle_right": (iw - ww - PADDING, cy),
        "bottom_left":  (PADDING, ih - wh - PADDING),
        "bottom_center":(cx, ih - wh - PADDING),
        "bottom_right": (iw - ww - PADDING, ih - wh - PADDING),
    }.get(pos, (cx, cy))

def render_text_watermark(
    img_bytes: bytes, text: str, pos: str, opacity: int, size_key: str
) -> bytes:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    w, h = img.size
    fs   = max(18, int(min(w, h) * SIZE_RATIOS.get(size_key, 0.14)))
    font = _find_font(fs)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    x, y   = _anchor_xy(pos, w, h, tw, th)

    alpha      = int(opacity * 2.55)   # 0–100 → 0–255
    shadow_off = max(2, fs // 16)

    # Shadow pass
    draw.text((x + shadow_off, y + shadow_off), text, font=font,
              fill=(0, 0, 0, min(alpha, 185)))
    # Main text
    draw.text((x, y), text, font=font, fill=(255, 255, 255, alpha))

    result = Image.alpha_composite(img, overlay).convert("RGB")
    out = io.BytesIO()
    result.save(out, format="JPEG", quality=92)
    return out.getvalue()

def render_image_watermark(
    img_bytes: bytes, wm_bytes: bytes, pos: str, opacity: int, size_key: str
) -> bytes:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    wm  = Image.open(io.BytesIO(wm_bytes)).convert("RGBA")
    w, h = img.size

    max_dim = int(min(w, h) * SIZE_RATIOS.get(size_key, 0.14))
    scale   = min(max_dim / wm.width, max_dim / wm.height)
    new_w   = max(1, int(wm.width  * scale))
    new_h   = max(1, int(wm.height * scale))
    wm      = wm.resize((new_w, new_h), Image.LANCZOS)

    # Apply opacity to alpha channel
    r, g, b, a = wm.split()
    a = a.point(lambda v: int(v * opacity / 100))
    wm = Image.merge("RGBA", (r, g, b, a))

    x, y    = _anchor_xy(pos, w, h, wm.width, wm.height)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay.paste(wm, (x, y), wm)

    result = Image.alpha_composite(img, overlay).convert("RGB")
    out = io.BytesIO()
    result.save(out, format="JPEG", quality=92)
    return out.getvalue()

def apply_watermark(img_bytes: bytes, settings: dict) -> bytes:
    pos      = settings.get("position", "center")
    opacity  = int(settings.get("opacity", 75))
    size_key = settings.get("size", "medium")

    if settings.get("wm_type") == "image" and settings.get("wm_image"):
        return render_image_watermark(img_bytes, settings["wm_image"], pos, opacity, size_key)
    return render_text_watermark(
        img_bytes, settings.get("wm_text", "© Watermark"), pos, opacity, size_key
    )

def settings_summary(s: dict) -> str:
    wm = f'"{s.get("wm_text", "")}"' if s.get("wm_type") == "text" else "image / logo"
    return (
        f"  • Type: {s.get('wm_type', 'text')}\n"
        f"  • Watermark: {wm}\n"
        f"  • Position: {POSITIONS_FULL.get(s.get('position', 'center'), '?')}\n"
        f"  • Opacity: {s.get('opacity', 75)}%\n"
        f"  • Size: {s.get('size', 'medium').capitalize()}"
    )

# ── Helper: get file object from any image message ────────────────────────────
async def _get_image_file(msg):
    if msg.photo:
        return await msg.photo[-1].get_file()
    if msg.document and msg.document.mime_type and \
       msg.document.mime_type.startswith("image/"):
        return await msg.document.get_file()
    return None

# ── /start ─────────────────────────────────────────────────────────────────────
async def cmd_start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    uid     = upd.effective_user.id
    u       = get_user(uid)
    presets = u.get("presets", {})
    tip     = (f"\n\n💾 You have *{len(presets)} preset(s)* saved. "
               f"They'll appear automatically when you send a photo.")  \
              if presets else ""

    await upd.message.reply_text(
        "👋 *Welcome to TheWaterMarkBot!*\n\n"
        "I'll add a watermark to any photo you send me.\n\n"
        "*How it works:*\n"
        "1️⃣ Send me a photo\n"
        "2️⃣ Choose text or image watermark\n"
        "3️⃣ Pick opacity & size\n"
        "4️⃣ Pick a position on the 3×3 grid\n"
        "5️⃣ Get your watermarked photo back!\n\n"
        "📸 *Send a photo to begin.*" + tip,
        parse_mode="Markdown",
    )
    return WAITING_PHOTO

# ── /help ──────────────────────────────────────────────────────────────────────
async def cmd_help(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await upd.message.reply_text(
        "📖 *TheWaterMarkBot — Help*\n\n"
        "*Commands:*\n"
        "/start — welcome message & restart\n"
        "/help — this message\n"
        "/mystats — your personal usage stats\n"
        "/reset — delete all your saved presets\n"
        "/cancel — stop the current operation\n\n"
        "*Features:*\n"
        "• Text or image/logo watermarks\n"
        "• 9 positions (3×3 grid)\n"
        "• Opacity: 25% / 50% / 75% / 100%\n"
        "• Size: Small / Medium / Large\n"
        "• Save up to 3 presets per user\n"
        "• Batch: watermark up to 10 photos at once\n\n"
        "💡 *Tips:*\n"
        "• Send your logo as a *File* (not a photo) to keep quality\n"
        "• Send multiple photos before tapping ✅ Done for batch mode",
        parse_mode="Markdown",
    )

# ── /mystats ───────────────────────────────────────────────────────────────────
async def cmd_mystats(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = upd.effective_user.id
    u    = get_user(uid)
    cnt  = u.get("count", 0)
    prs  = u.get("presets", {})
    names = "\n".join(f"  • {n}" for n in prs) if prs else "  (none yet)"
    await upd.message.reply_text(
        f"📊 *Your stats*\n\n"
        f"🖼 Photos watermarked: *{cnt}*\n"
        f"💾 Saved presets ({len(prs)}/3):\n{names}",
        parse_mode="Markdown",
    )

# ── /admin_stats ───────────────────────────────────────────────────────────────
async def cmd_admin(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if ADMIN_ID and upd.effective_user.id != ADMIN_ID:
        await upd.message.reply_text("⛔ This command is for the bot owner only.")
        return
    data   = _load()
    total  = sum(v.get("count", 0) for v in data.values())
    top5   = sorted(data.items(), key=lambda x: x[1].get("count", 0), reverse=True)[:5]
    lines  = "\n".join(
        f"  {i+1}. uid {k} — {v.get('count', 0)} photos"
        for i, (k, v) in enumerate(top5)
    ) or "  (no data yet)"
    await upd.message.reply_text(
        f"🛠 *Admin Stats*\n\n"
        f"Total users: *{len(data)}*\n"
        f"Total photos processed: *{total}*\n\n"
        f"Top 5 users:\n{lines}",
        parse_mode="Markdown",
    )

# ── /reset ─────────────────────────────────────────────────────────────────────
async def cmd_reset(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = upd.effective_user.id
    u   = get_user(uid)
    u["presets"] = {}
    update_user(uid, u)
    await upd.message.reply_text("🗑 All your saved presets have been deleted.")

# ── /cancel ────────────────────────────────────────────────────────────────────
async def cmd_cancel(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await upd.message.reply_text(
        "❌ Cancelled.\n\nSend a photo whenever you're ready to start again!"
    )
    return WAITING_PHOTO

# ── Step 1: receive photo(s) ───────────────────────────────────────────────────
async def recv_photo(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    f = await _get_image_file(upd.message)
    if not f:
        await upd.message.reply_text("📸 Please send a photo or image file.")
        return WAITING_PHOTO

    data = bytes(await f.download_as_bytearray())
    batch = ctx.user_data.setdefault("batch", [])
    batch.append(data)
    uid = upd.effective_user.id

    if len(batch) == 1:
        presets = get_user(uid).get("presets", {})
        if presets:
            await upd.message.reply_text(
                f"✅ Photo received!\n\n"
                f"You can keep sending more photos for *batch mode* (up to 10 total).\n\n"
                f"💾 You have saved presets — want to use one?",
                parse_mode="Markdown",
                reply_markup=kb_presets(uid),
            )
            return WAITING_PRESET_CHOICE
        else:
            await upd.message.reply_text(
                "✅ Photo received!\n\n"
                "You can send more photos now for *batch mode* (up to 10).\n\n"
                "What kind of watermark do you want to add?",
                parse_mode="Markdown",
                reply_markup=kb_wm_type(),
            )
            return WAITING_WM_TYPE

    elif len(batch) < 10:
        await upd.message.reply_text(
            f"📎 *Photo {len(batch)}* added to the batch.\n"
            f"Send more, or tap ✅ Done when ready.",
            parse_mode="Markdown",
            reply_markup=kb_batch_done(),
        )
        return WAITING_BATCH_PHOTOS

    else:
        await upd.message.reply_text(
            "📦 You've reached the *10-photo limit*.\nChoose the watermark type:",
            parse_mode="Markdown",
            reply_markup=kb_wm_type(),
        )
        return WAITING_WM_TYPE

async def recv_batch_photo(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return await recv_photo(upd, ctx)

async def cb_batch(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = upd.callback_query
    await q.answer()

    if q.data == "batch_cancel":
        ctx.user_data.clear()
        await q.edit_message_text("❌ Batch cancelled. Send a photo to start fresh.")
        return WAITING_PHOTO

    n = len(ctx.user_data.get("batch", []))
    await q.edit_message_text(
        f"✅ *{n} photo{'s' if n > 1 else ''}* queued.\n\nChoose the watermark type:",
        parse_mode="Markdown",
        reply_markup=kb_wm_type(),
    )
    return WAITING_WM_TYPE

# ── Preset choice ──────────────────────────────────────────────────────────────
async def cb_preset_choice(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q   = upd.callback_query
    await q.answer()
    uid = upd.effective_user.id

    if q.data in ("preset_new", "preset_skip"):
        await q.edit_message_text(
            "What kind of watermark do you want to add?",
            reply_markup=kb_wm_type(),
        )
        return WAITING_WM_TYPE

    if q.data.startswith("preset_"):
        name   = q.data[len("preset_"):]
        preset = get_user(uid).get("presets", {}).get(name)
        if not preset:
            await q.edit_message_text(
                "❌ Preset not found. Please configure the watermark:",
                reply_markup=kb_wm_type(),
            )
            return WAITING_WM_TYPE

        ctx.user_data["settings"] = dict(preset)
        await q.edit_message_text(
            f"✅ Preset *{name}* loaded:\n\n{settings_summary(preset)}\n\n⚙️ Processing…",
            parse_mode="Markdown",
        )
        return await _do_process(q.message, ctx, uid)

    return WAITING_PRESET_CHOICE

# ── Step 2: watermark type ─────────────────────────────────────────────────────
async def cb_wm_type(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = upd.callback_query
    await q.answer()
    ctx.user_data.setdefault("settings", {})["wm_type"] = q.data.replace("wm_", "")

    if q.data == "wm_text":
        await q.edit_message_text(
            "✏️ *Text watermark*\n\nSend me the text you want on the photo.\n"
            "_(Examples: © MyName, Confidential, @MyBrand)_",
            parse_mode="Markdown",
        )
        return WAITING_WM_TEXT
    else:
        await q.edit_message_text(
            "🖼 *Image watermark*\n\nSend me your logo or signature image.\n\n"
            "💡 *Tip:* send it as a *File* (tap the 📎 paperclip → File) "
            "instead of a photo so Telegram doesn't compress it.",
            parse_mode="Markdown",
        )
        return WAITING_WM_IMAGE

# ── Step 3a: watermark text ────────────────────────────────────────────────────
async def recv_wm_text(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = upd.message.text.strip()
    if not text:
        await upd.message.reply_text("⚠️ Please send a non-empty text.")
        return WAITING_WM_TEXT

    ctx.user_data["settings"]["wm_text"] = text
    await upd.message.reply_text(
        f'📝 Watermark text set to: *"{text}"*\n\nNow choose the opacity:',
        parse_mode="Markdown",
        reply_markup=kb_opacity(),
    )
    return WAITING_OPACITY

# ── Step 3b: watermark image ───────────────────────────────────────────────────
async def recv_wm_image(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    f = await _get_image_file(upd.message)
    if not f:
        await upd.message.reply_text(
            "⚠️ That doesn't look like an image. Please send your logo as a photo or file."
        )
        return WAITING_WM_IMAGE

    ctx.user_data["settings"]["wm_image"] = bytes(await f.download_as_bytearray())
    await upd.message.reply_text(
        "✅ Logo received!\n\nNow choose the opacity:",
        reply_markup=kb_opacity(),
    )
    return WAITING_OPACITY

# ── Step 4: opacity ────────────────────────────────────────────────────────────
async def cb_opacity(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q       = upd.callback_query
    await q.answer()
    opacity = q.data.replace("op_", "")
    ctx.user_data["settings"]["opacity"] = int(opacity)
    await q.edit_message_text(
        f"🔆 Opacity set to *{opacity}%*\n\nNow choose the watermark size:",
        parse_mode="Markdown",
        reply_markup=kb_size(),
    )
    return WAITING_SIZE

# ── Step 5: size ───────────────────────────────────────────────────────────────
async def cb_size(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q    = upd.callback_query
    await q.answer()
    size = q.data.replace("sz_", "")
    ctx.user_data["settings"]["size"] = size
    await q.edit_message_text(
        f"📐 Size set to *{size.capitalize()}*\n\nNow pick the position on the photo:",
        parse_mode="Markdown",
        reply_markup=kb_position(),
    )
    return WAITING_POSITION

# ── Step 6: position ───────────────────────────────────────────────────────────
async def cb_position(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q   = upd.callback_query
    await q.answer()
    pos = q.data.replace("pos_", "")
    ctx.user_data["settings"]["position"] = pos
    s   = ctx.user_data["settings"]
    await q.edit_message_text(
        f"📍 Position: *{POSITIONS_FULL.get(pos, '?')}*\n\n"
        f"*Your settings:*\n{settings_summary(s)}\n\n"
        "💾 Want to save these settings as a preset for next time?",
        parse_mode="Markdown",
        reply_markup=kb_save_preset(),
    )
    return WAITING_PRESET_SAVE

# ── Step 7: save preset? ───────────────────────────────────────────────────────
async def cb_save_preset(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q   = upd.callback_query
    await q.answer()
    uid = upd.effective_user.id

    if q.data == "no_save":
        await q.edit_message_text("⚙️ Processing your photo(s)…")
        return await _do_process(q.message, ctx, uid)

    await q.edit_message_text(
        "💾 *Save preset*\n\nSend me a short name for this preset (max 20 characters):",
        parse_mode="Markdown",
    )
    return WAITING_PRESET_NAME

async def recv_preset_name(upd: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid  = upd.effective_user.id
    name = upd.message.text.strip()[:20]
    if not name:
        await upd.message.reply_text("⚠️ Name can't be empty. Send a short name:")
        return WAITING_PRESET_NAME

    u = get_user(uid)
    p = u.setdefault("presets", {})
    if len(p) >= 3:
        oldest = next(iter(p))
        del p[oldest]
        logger.info("Removed oldest preset '%s' for uid %s", oldest, uid)

    # Store without the raw image bytes (not serialisable to JSON)
    safe = {k: v for k, v in ctx.user_data["settings"].items() if k != "wm_image"}
    p[name] = safe
    update_user(uid, u)

    await upd.message.reply_text(
        f"✅ Preset *{name}* saved!\n\n⚙️ Processing your photo(s)…",
        parse_mode="Markdown",
    )
    return await _do_process(upd.message, ctx, uid)

# ── Core processing ────────────────────────────────────────────────────────────
async def _do_process(
    message, ctx: ContextTypes.DEFAULT_TYPE, uid: int
) -> int:
    photos   = ctx.user_data.get("batch", [])
    settings = ctx.user_data.get("settings", {})

    if not photos:
        await message.reply_text(
            "❌ No photos found. Please send a photo first."
        )
        ctx.user_data.clear()
        return WAITING_PHOTO

    for i, pb in enumerate(photos):
        try:
            result = apply_watermark(pb, settings)
            caption = (
                f"✅ Photo {i+1}/{len(photos)}\n"
                f"📍 {POSITIONS_FULL.get(settings.get('position','center'),'?')}  "
                f"🔆 {settings.get('opacity',75)}%  "
                f"📐 {settings.get('size','medium').capitalize()}"
            )
            await message.reply_photo(photo=io.BytesIO(result), caption=caption)
            inc_count(uid)
        except Exception as exc:
            logger.exception("Error processing photo %d for uid %s", i + 1, uid)
            await message.reply_text(
                f"❌ Something went wrong with photo {i+1}.\n"
                f"Error: {exc}\n\nPlease try again."
            )

    n = len(photos)
    await message.reply_text(
        f"🎉 Done! *{n} photo{'s' if n > 1 else ''}* watermarked.\n\n"
        "Send another photo anytime!",
        parse_mode="Markdown",
    )
    ctx.user_data.clear()
    return WAITING_PHOTO

# ── App setup & startup ────────────────────────────────────────────────────────
async def post_init(app: Application) -> None:
    """Register the command list that appears in the Telegram menu (/ button)."""
    await app.bot.set_my_commands([
        BotCommand("start",       "Welcome message & restart"),
        BotCommand("help",        "How to use the bot"),
        BotCommand("mystats",     "Your usage stats & presets"),
        BotCommand("reset",       "Delete all your saved presets"),
        BotCommand("cancel",      "Cancel the current operation"),
    ])
    logger.info("Bot commands registered.")

def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError(
            "No bot token found!\n"
            "Set the BOT_TOKEN environment variable:\n"
            "  export BOT_TOKEN='your_token_from_BotFather'"
        )

    IS_IMG = filters.PHOTO | filters.Document.IMAGE

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(IS_IMG, recv_photo),
        ],
        states={
            WAITING_PHOTO: [
                MessageHandler(IS_IMG, recv_photo),
            ],
            WAITING_BATCH_PHOTOS: [
                MessageHandler(IS_IMG, recv_batch_photo),
                CallbackQueryHandler(cb_batch, pattern="^batch_"),
            ],
            WAITING_PRESET_CHOICE: [
                CallbackQueryHandler(cb_preset_choice, pattern="^preset_"),
                MessageHandler(IS_IMG, recv_batch_photo),
                CallbackQueryHandler(cb_batch, pattern="^batch_"),
            ],
            WAITING_WM_TYPE: [
                CallbackQueryHandler(cb_wm_type, pattern="^wm_"),
                MessageHandler(IS_IMG, recv_batch_photo),
                CallbackQueryHandler(cb_batch, pattern="^batch_"),
            ],
            WAITING_WM_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_wm_text),
            ],
            WAITING_WM_IMAGE: [
                MessageHandler(IS_IMG, recv_wm_image),
            ],
            WAITING_OPACITY: [
                CallbackQueryHandler(cb_opacity, pattern="^op_"),
            ],
            WAITING_SIZE: [
                CallbackQueryHandler(cb_size, pattern="^sz_"),
            ],
            WAITING_POSITION: [
                CallbackQueryHandler(cb_position, pattern="^pos_"),
            ],
            WAITING_PRESET_SAVE: [
                CallbackQueryHandler(cb_save_preset, pattern="^(do|no)_save$"),
            ],
            WAITING_PRESET_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_preset_name),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=600,   # auto-reset if user goes quiet for 10 min
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("mystats",      cmd_mystats))
    app.add_handler(CommandHandler("admin_stats",  cmd_admin))
    app.add_handler(CommandHandler("reset",        cmd_reset))

    logger.info("TheWaterMarkBot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
