


import os, io, json, logging, re, urllib.request
from pathlib import Path
from copy import deepcopy
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, InputMediaPhoto,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
    filters, ContextTypes,
)
from telegram.error import BadRequest

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0"))
DATA_FILE = Path("user_data.json")
FONT_DIR  = Path("fonts")
PADDING   = 30

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("wmarkbot")

# ── States ─────────────────────────────────────────────────────────────────────
(
    ST_IDLE,          # bot is waiting for a photo
    ST_COLLECTING,    # user is adding batch photos
    ST_DASH,          # main editor dashboard
    ST_EDIT_TEXT,     # typing new watermark text  (isolated)
    ST_EDIT_LOGO,     # sending logo file          (isolated)
    ST_ENTER_RGB,     # typing a hex / RGB value   (isolated)
    ST_NAME_PRESET,   # typing a preset name       (isolated)
) = range(7)

# ── Fonts ──────────────────────────────────────────────────────────────────────
# Direct raw GitHub links to the TTF — all open-source (OFL / Apache 2.0)
FONT_URLS = {
    "Roboto":      "https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Bold.ttf",
    "Montserrat":  "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf",
    "Oswald":      "https://github.com/googlefonts/OswaldFont/raw/main/fonts/ttf/Oswald-Bold.ttf",
    "Playfair":    "https://github.com/clauseggers/Playfair-Display/raw/master/fonts/Playfair_Display/static/PlayfairDisplay-Bold.ttf",
    "Dancing":     "https://github.com/googlefonts/dancing-script/raw/main/fonts/ttf/DancingScript-Bold.ttf",
    "Raleway":     "https://github.com/impallari/Raleway/raw/master/fonts/Raleway-Bold.ttf",
    "Lato":        "https://github.com/googlefonts/lato/raw/main/fonts/ttf/Lato-Bold.ttf",
    "Bebas":       "https://github.com/dharmatype/Bebas-Neue/raw/master/fonts/BebasNeue(2018)ByDharmaType/BebasNeue-Regular.ttf",
}

SYSTEM_FONT_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
]

LOADED_FONTS: dict[str, Path] = {}   # populated by _boot_fonts()


def _boot_fonts():
    """Download missing fonts, register all available ones."""
    FONT_DIR.mkdir(exist_ok=True)
    for name, url in FONT_URLS.items():
        dest = FONT_DIR / f"{name}.ttf"
        if not dest.exists():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    dest.write_bytes(r.read())
                logger.info("Font downloaded: %s", name)
            except Exception as exc:
                logger.warning("Font download failed (%s): %s", name, exc)
        if dest.exists():
            LOADED_FONTS[name] = dest

    for fp in SYSTEM_FONT_FALLBACKS:
        if Path(fp).exists():
            LOADED_FONTS.setdefault("System", Path(fp))
            break

    logger.info("Fonts ready: %s", list(LOADED_FONTS.keys()))


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    p = LOADED_FONTS.get(name) or (next(iter(LOADED_FONTS.values()), None) if LOADED_FONTS else None)
    if p:
        try:
            return ImageFont.truetype(str(p), size)
        except Exception:
            pass
    return ImageFont.load_default()


# ── Default settings ───────────────────────────────────────────────────────────
DEFAULTS: dict = {
    "mode":     "text",          # "text" | "image"
    "text":     "© Your Brand",
    "logo":     None,            # bytes or None
    "font":     "Roboto",
    "rgb":      [255, 255, 255],
    "opacity":  80,              # 0–100
    "size":     18,              # % of shorter image side
    "rotation": 0,               # degrees
    "pos":      [4, 4],          # [row, col] in 5×5 grid
    "effect":   "shadow",
    "quality":  92,
    "bg":       "photo",         # "photo" | "light" | "dark"
}

# ── Position grid (5×5) ────────────────────────────────────────────────────────
# 0 = far edge, 4 = far edge.  Anchor = watermark centre.
_PX = [0.04, 0.25, 0.50, 0.75, 0.96]   # col fractions
_PY = [0.04, 0.25, 0.50, 0.75, 0.96]   # row fractions

# Named anchors for the caption summary
_POS_NAME = {
    (0,0):"Top-Left", (0,2):"Top-Center", (0,4):"Top-Right",
    (2,0):"Mid-Left", (2,2):"Center",     (2,4):"Mid-Right",
    (4,0):"Bot-Left", (4,2):"Bot-Center", (4,4):"Bot-Right",
}

def _pos_xy(pos, iw, ih, ww, wh):
    r, c = pos
    cx = int(_PX[c] * iw - ww / 2)
    cy = int(_PY[r] * ih - wh / 2)
    return max(0, min(iw - ww, cx)), max(0, min(ih - wh, cy))

def _pos_name(pos):
    t = tuple(pos)
    return _POS_NAME.get(t, f"R{pos[0]+1}·C{pos[1]+1}")

# ── Colour palette (24 swatches for quick pick) ────────────────────────────────
PALETTE = [
    ("⬜",(255,255,255)), ("🔲",(200,200,200)), ("▪️",(100,100,100)), ("⬛",(0,0,0)),
    ("🟥",(220,50,50)),   ("🟧",(255,140,0)),   ("🟨",(255,215,0)),  ("🩷",(255,105,180)),
    ("🟦",(30,144,255)),  ("🩵",(0,200,220)),   ("🟩",(50,200,50)),  ("🟣",(147,0,211)),
    ("🔵",(0,102,204)),   ("🔴",(255,59,48)),   ("🟤",(162,120,80)), ("🩶",(160,160,175)),
    ("💛",(255,255,0)),   ("💚",(57,255,20)),   ("💙",(31,111,235)), ("❤️",(255,45,85)),
    ("✨",(255,200,50)),  ("🤍",(240,238,235)), ("🖤",(25,25,28)),   ("🧡",(255,128,0)),
]

# ── Effects ────────────────────────────────────────────────────────────────────
EFFECTS = ["shadow","hard_shadow","outline","double_outline","glow","none"]
EFX_LABEL = {
    "shadow":         "🌑 Shadow",
    "hard_shadow":    "🔳 Hard Shadow",
    "outline":        "🔲 Outline",
    "double_outline": "⬛ Double Outline",
    "glow":           "💫 Glow",
    "none":           "✨ None",
}

ROTATIONS     = [0, 15, 30, 45, 90, 135, 180, 270]
QUALITY_STEPS = [72, 82, 92, 100]

# ── Storage ────────────────────────────────────────────────────────────────────
def _db_load() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {}

def _db_save(d: dict):
    DATA_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False), "utf-8")

def db_get(uid: int) -> dict:
    return _db_load().get(str(uid), {"count": 0, "presets": {}})

def db_put(uid: int, u: dict):
    d = _db_load(); d[str(uid)] = u; _db_save(d)

def db_inc(uid: int):
    u = db_get(uid); u["count"] = u.get("count", 0) + 1; db_put(uid, u)

# ── Watermark renderer ─────────────────────────────────────────────────────────
def _hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)

def _stamp_text(text, font_name, size_px, rgb, effect, alpha, rotation):
    fnt = _font(font_name, size_px)
    tmp_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bb = tmp_draw.textbbox((0, 0), text, font=fnt)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    pad = max(8, size_px // 5)
    W, H = tw + pad * 4, th + pad * 4
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    ox, oy = pad * 2, pad * 2
    a = int(255 * alpha)
    r, g, b = rgb

    if effect == "shadow":
        so = max(3, size_px // 10)
        draw.text((ox+so, oy+so), text, font=fnt, fill=(0, 0, 0, min(a, 200)))
        draw.text((ox, oy), text, font=fnt, fill=(r, g, b, a))

    elif effect == "hard_shadow":
        for d in range(1, 5):
            draw.text((ox+d, oy+d), text, font=fnt, fill=(0, 0, 0, min(a, 230)))
        draw.text((ox, oy), text, font=fnt, fill=(r, g, b, a))

    elif effect == "outline":
        thick = max(2, size_px // 14)
        cr, cg, cb = (0 if v > 128 else 255 for v in (r, g, b))
        for dx in range(-thick, thick+1):
            for dy in range(-thick, thick+1):
                if dx or dy:
                    draw.text((ox+dx, oy+dy), text, font=fnt, fill=(cr, cg, cb, min(a, 230)))
        draw.text((ox, oy), text, font=fnt, fill=(r, g, b, a))

    elif effect == "double_outline":
        for thick, col in [(5, (0,0,0)), (2, (255,255,255))]:
            for dx in range(-thick, thick+1):
                for dy in range(-thick, thick+1):
                    if dx or dy:
                        draw.text((ox+dx, oy+dy), text, font=fnt, fill=(*col, min(a, 220)))
        draw.text((ox, oy), text, font=fnt, fill=(r, g, b, a))

    elif effect == "glow":
        glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        for off in range(10, 0, -2):
            for ddx, ddy in [(off,0),(-off,0),(0,off),(0,-off),(off,off),(-off,-off)]:
                gd.text((ox+ddx, oy+ddy), text, font=fnt, fill=(r, g, b, 22))
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=5))
        canvas = Image.alpha_composite(canvas, glow_layer)
        ImageDraw.Draw(canvas).text((ox, oy), text, font=fnt, fill=(r, g, b, a))

    else:  # none
        draw.text((ox, oy), text, font=fnt, fill=(r, g, b, a))

    if rotation:
        canvas = canvas.rotate(-rotation, expand=True, resample=Image.BICUBIC)
    return canvas


def _stamp_logo(logo_bytes, size_px, opacity, rotation):
    wm = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
    scale = size_px / max(wm.width, wm.height)
    wm = wm.resize(
        (max(1, int(wm.width * scale)), max(1, int(wm.height * scale))),
        Image.LANCZOS,
    )
    r, g, b, a = wm.split()
    a = a.point(lambda v: int(v * opacity / 100))
    wm = Image.merge("RGBA", (r, g, b, a))
    if rotation:
        wm = wm.rotate(-rotation, expand=True, resample=Image.BICUBIC)
    return wm


def render_wm(img_bytes: bytes, s: dict, max_dim: int | None = None) -> bytes:
    base = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    W, H = base.size

    # Background override (for contrast check)
    bg = s.get("bg", "photo")
    if bg == "light":
        base = Image.new("RGBA", (W, H), (215, 215, 215, 255))
    elif bg == "dark":
        base = Image.new("RGBA", (W, H), (20, 20, 20, 255))

    if max_dim:
        sc = min(1.0, max_dim / W, max_dim / H)
        base = base.resize((max(1, int(W*sc)), max(1, int(H*sc))), Image.LANCZOS)
        W, H = base.size

    size_px = max(14, int(min(W, H) * s.get("size", 18) / 100))
    pos = s.get("pos", [4, 4])

    if s.get("mode") == "image" and s.get("logo"):
        stamp = _stamp_logo(s["logo"], size_px, s.get("opacity", 80), s.get("rotation", 0))
    else:
        stamp = _stamp_text(
            s.get("text", "© Your Brand"),
            s.get("font", "Roboto"),
            size_px,
            tuple(s.get("rgb", [255, 255, 255])),
            s.get("effect", "shadow"),
            s.get("opacity", 80) / 100,
            s.get("rotation", 0),
        )

    x, y = _pos_xy(pos, W, H, stamp.width, stamp.height)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    overlay.paste(stamp, (x, y), stamp)
    result = Image.alpha_composite(base, overlay).convert("RGB")

    out = io.BytesIO()
    result.save(out, format="JPEG", quality=s.get("quality", 92))
    return out.getvalue()


def make_preview(img_bytes: bytes, s: dict) -> bytes:
    return render_wm(img_bytes, s, max_dim=900)


# ── Caption for the dashboard ──────────────────────────────────────────────────
def dash_caption(s: dict) -> str:
    mode = s.get("mode", "text")
    if mode == "text":
        wm_line = f'✏️  *"{s.get("text","")}"*  ·  {s.get("font","Roboto")}  ·  {_hex(s.get("rgb",[255,255,255]))}'
    else:
        wm_line = "🖼  *Image / Logo*"

    pos_name = _pos_name(s.get("pos", [4, 4]))
    efx_name = EFX_LABEL.get(s.get("effect","shadow"), "?")
    bg_name  = {"photo":"Original","light":"Light BG","dark":"Dark BG"}.get(s.get("bg","photo"),"?")

    return (
        f"*Watermark Editor*\n\n"
        f"{wm_line}\n"
        f"✨  {efx_name}   🔄  {s.get('rotation',0)}°\n"
        f"🔆  Opacity {s.get('opacity',80)}%   📐  Size {s.get('size',18)}%\n"
        f"📍  {pos_name}   🖼  {bg_name}   📊  Q{s.get('quality',92)}\n\n"
        f"_Tap any button — preview updates live_"
    )


# ── Dashboard keyboard ─────────────────────────────────────────────────────────
def dash_kb(s: dict) -> InlineKeyboardMarkup:
    mode   = s.get("mode", "text")
    pos    = s.get("pos", [4, 4])
    effect = s.get("effect", "shadow")
    rot    = s.get("rotation", 0)
    size   = s.get("size", 18)
    op     = s.get("opacity", 80)
    q      = s.get("quality", 92)
    bg     = s.get("bg", "photo")
    font   = s.get("font", "Roboto")

    fonts   = list(LOADED_FONTS.keys()) or ["System"]
    fidx    = fonts.index(font) if font in fonts else 0
    fnext   = fonts[(fidx+1) % len(fonts)]

    eidx    = EFFECTS.index(effect) if effect in EFFECTS else 0
    enext   = EFFECTS[(eidx+1) % len(EFFECTS)]

    ridx    = ROTATIONS.index(rot) if rot in ROTATIONS else 0
    rnext   = ROTATIONS[(ridx+1) % len(ROTATIONS)]

    qidx    = QUALITY_STEPS.index(q) if q in QUALITY_STEPS else 2
    qnext   = QUALITY_STEPS[(qidx+1) % len(QUALITY_STEPS)]

    bg_next = {"photo":"light","light":"dark","dark":"photo"}[bg]
    bg_icon = {"photo":"🖼","light":"☀️","dark":"🌙"}[bg]

    rows = []

    # ── Section 1: CONTENT ─────────────────────────────────────────────────────
    if mode == "text":
        rows.append([
            InlineKeyboardButton("✏️  Edit text",     callback_data="d:edit_text"),
            InlineKeyboardButton("🖼  Use image logo", callback_data="d:to_image"),
        ])
        rows.append([
            InlineKeyboardButton(f"🔤  {font}",       callback_data="d:font"),
            InlineKeyboardButton(f"→ {fnext}",         callback_data="d:font"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("🖼  Replace logo",   callback_data="d:edit_logo"),
            InlineKeyboardButton("✏️  Use text",       callback_data="d:to_text"),
        ])

    # ── Section 2: COLOUR ─────────────────────────────────────────────────────
    rows.append([
        InlineKeyboardButton("🎨  Colour palette",   callback_data="d:palette"),
        InlineKeyboardButton("🖊  Custom #hex / RGB", callback_data="d:rgb"),
    ])

    # ── Section 3: EFFECT ─────────────────────────────────────────────────────
    rows.append([
        InlineKeyboardButton(f"{EFX_LABEL[effect]}",   callback_data="d:effect"),
        InlineKeyboardButton(f"→ {EFX_LABEL[enext]}",  callback_data="d:effect"),
    ])

    # ── Section 4: OPACITY ────────────────────────────────────────────────────
    rows.append([
        InlineKeyboardButton("−",              callback_data="d:op-"),
        InlineKeyboardButton(f"🔆  {op}%",     callback_data="d:noop"),
        InlineKeyboardButton("+",              callback_data="d:op+"),
    ])

    # ── Section 5: SIZE ───────────────────────────────────────────────────────
    rows.append([
        InlineKeyboardButton("−",              callback_data="d:sz-"),
        InlineKeyboardButton(f"📐  {size}%",   callback_data="d:noop"),
        InlineKeyboardButton("+",              callback_data="d:sz+"),
    ])

    # ── Section 6: ROTATION ───────────────────────────────────────────────────
    rows.append([
        InlineKeyboardButton(f"🔄  {rot}° → {rnext}°", callback_data="d:rot"),
        InlineKeyboardButton("↩  Reset 0°",            callback_data="d:rot0"),
    ])

    # ── Section 7: POSITION (5×5 grid) ────────────────────────────────────────
    # Header row (label only — not a button)
    rows.append([InlineKeyboardButton("📍  Position", callback_data="d:noop")])
    for ri in range(5):
        row = []
        for ci in range(5):
            active = (pos == [ri, ci] or pos == (ri, ci))
            # Corners and center get emoji labels; rest get dots
            label_map = {
                (0,0):"↖",(0,2):"⬆",(0,4):"↗",
                (2,0):"◀",(2,2):"✚",(2,4):"▶",
                (4,0):"↙",(4,2):"⬇",(4,4):"↘",
            }
            sym = label_map.get((ri,ci), "·")
            lbl = f"[{sym}]" if active else sym
            row.append(InlineKeyboardButton(lbl, callback_data=f"d:pos:{ri}:{ci}"))
        rows.append(row)

    # ── Section 8: BACKGROUND / QUALITY ──────────────────────────────────────
    rows.append([
        InlineKeyboardButton(f"{bg_icon}  Preview BG → {bg_next}", callback_data="d:bg"),
        InlineKeyboardButton(f"📊  Quality {q}% → {qnext}%",        callback_data="d:quality"),
    ])

    # ── Section 9: ACTIONS ────────────────────────────────────────────────────
    rows.append([
        InlineKeyboardButton("✅  Done — send photo",  callback_data="d:apply"),
        InlineKeyboardButton("💾  Save preset",        callback_data="d:save"),
    ])
    rows.append([
        InlineKeyboardButton("📋  Load preset",        callback_data="d:presets"),
        InlineKeyboardButton("❌  Cancel",             callback_data="d:cancel"),
    ])

    return InlineKeyboardMarkup(rows)


def palette_kb() -> InlineKeyboardMarkup:
    rows = []
    row  = []
    for i, (em, rgb) in enumerate(PALETTE):
        row.append(InlineKeyboardButton(em, callback_data=f"pal:{rgb[0]}:{rgb[1]}:{rgb[2]}"))
        if len(row) == 6:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("← Back to editor", callback_data="pal:back")])
    return InlineKeyboardMarkup(rows)


def presets_kb(uid: int) -> InlineKeyboardMarkup:
    presets = db_get(uid).get("presets", {})
    rows = []
    for name in presets:
        rows.append([
            InlineKeyboardButton(f"📌  {name}", callback_data=f"pr:load:{name}"),
            InlineKeyboardButton("🗑",           callback_data=f"pr:del:{name}"),
        ])
    rows.append([InlineKeyboardButton("← Back to editor", callback_data="pr:back")])
    return InlineKeyboardMarkup(rows)


# ── Helper: safe dashboard refresh ────────────────────────────────────────────
async def _refresh(q, ctx):
    """Re-render preview and update the dashboard in-place."""
    s      = ctx.user_data.get("settings", deepcopy(DEFAULTS))
    photos = ctx.user_data.get("photos", [])

    if photos:
        try:
            pv_bytes = make_preview(photos[0], s)
        except Exception as e:
            logger.error("Preview render error: %s", e)
            pv_bytes = photos[0]

        try:
            await q.edit_message_media(
                media=InputMediaPhoto(
                    media=io.BytesIO(pv_bytes),
                    caption=dash_caption(s),
                    parse_mode="Markdown",
                ),
                reply_markup=dash_kb(s),
            )
            return
        except BadRequest:
            pass

    # Fallback: just update caption + keyboard
    try:
        await q.edit_message_caption(
            caption=dash_caption(s),
            reply_markup=dash_kb(s),
            parse_mode="Markdown",
        )
    except BadRequest:
        pass


async def _open_editor(message, ctx):
    """Send a fresh dashboard with preview photo."""
    s      = ctx.user_data.get("settings", deepcopy(DEFAULTS))
    photos = ctx.user_data.get("photos", [])

    if photos:
        try:
            pv_bytes = make_preview(photos[0], s)
            await message.reply_photo(
                photo=io.BytesIO(pv_bytes),
                caption=dash_caption(s),
                reply_markup=dash_kb(s),
                parse_mode="Markdown",
            )
            return
        except Exception as e:
            logger.error("Open editor error: %s", e)

    await message.reply_text(
        dash_caption(s),
        reply_markup=dash_kb(s),
        parse_mode="Markdown",
    )


def _collect_kb(n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🎨  Open Editor ({n} photo{'s' if n>1 else ''})", callback_data="col:open"),
        InlineKeyboardButton("➕  Add more",     callback_data="col:more"),
        InlineKeyboardButton("❌",               callback_data="col:cancel"),
    ]])


async def _get_img(msg):
    if msg.photo:
        return await msg.photo[-1].get_file()
    if msg.document and msg.document.mime_type \
            and msg.document.mime_type.startswith("image/"):
        return await msg.document.get_file()
    return None


# ── Commands ───────────────────────────────────────────────────────────────────
ONBOARDING = (
    "👋 *Hey! Welcome to TheWaterMarkBot.*\n\n"
    "I'll add a watermark to your photos — text or your own logo — "
    "with live preview so you see exactly what you're getting.\n\n"
    "📸 *Just send me a photo to begin.*\n\n"
    "_No forms. No accounts. No waiting._"
)

RETURNING  = (
    "👋 *Welcome back!*\n\n"
    "📸 Send me a photo and we'll pick up right where you left off."
)


async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    c.user_data.clear()
    c.user_data["settings"] = deepcopy(DEFAULTS)
    uid  = u.effective_user.id
    user = db_get(uid)
    msg  = RETURNING if user.get("count", 0) > 0 else ONBOARDING
    if user.get("presets"):
        msg += f"\n\n💾 You have *{len(user['presets'])} saved preset(s)*."
    await u.message.reply_text(msg, parse_mode="Markdown")
    return ST_IDLE


async def cmd_help(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "📖 *How to use TheWaterMarkBot*\n\n"
        "1. Send a photo (up to 20 at once)\n"
        "2. Tap *Open Editor*\n"
        "3. Adjust the settings — preview updates live\n"
        "4. Tap *✅ Done* — get your watermarked photo\n\n"
        "*In the editor you can:*\n"
        "• Change text or switch to your own logo\n"
        "• Pick from 8 fonts\n"
        "• Choose any colour (24 swatches + custom hex)\n"
        "• Set effect: Shadow / Outline / Glow / and more\n"
        "• Adjust opacity, size, rotation\n"
        "• Place it anywhere on a 5×5 position grid\n"
        "• Save settings as a preset for next time\n\n"
        "*Commands:* /start  /help  /mystats  /reset  /cancel",
        parse_mode="Markdown",
    )


async def cmd_mystats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid  = u.effective_user.id
    user = db_get(uid)
    cnt  = user.get("count", 0)
    prs  = user.get("presets", {})
    names = "\n".join(f"  📌 {n}" for n in prs) or "  None yet — save one from the editor!"
    await u.message.reply_text(
        f"📊 *Your stats*\n\n"
        f"Photos watermarked: *{cnt}*\n"
        f"Saved presets ({len(prs)}/5):\n{names}",
        parse_mode="Markdown",
    )


async def cmd_admin(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and u.effective_user.id != ADMIN_ID:
        return
    d     = _db_load()
    total = sum(v.get("count", 0) for v in d.values())
    top   = sorted(d.items(), key=lambda x: x[1].get("count", 0), reverse=True)[:10]
    lines = "\n".join(f"  {i+1}. uid:{k} — {v.get('count',0)}" for i,(k,v) in enumerate(top))
    await u.message.reply_text(
        f"🛠 *Admin*\n\nUsers: {len(d)}\nTotal photos: {total}\n\nTop:\n{lines}",
        parse_mode="Markdown",
    )


async def cmd_reset(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid  = u.effective_user.id
    user = db_get(uid)
    user["presets"] = {}
    db_put(uid, user)
    await u.message.reply_text("🗑 All presets deleted.")


async def cmd_cancel(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    c.user_data.clear()
    c.user_data["settings"] = deepcopy(DEFAULTS)
    await u.message.reply_text("❌ Cancelled.\n\n📸 Send a photo whenever you're ready.")
    return ST_IDLE


# ── Photo collection ───────────────────────────────────────────────────────────
async def recv_photo(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    f = await _get_img(u.message)
    if not f:
        await u.message.reply_text("📸 Please send a photo or image file.")
        return ST_IDLE

    if "settings" not in c.user_data:
        c.user_data["settings"] = deepcopy(DEFAULTS)

    photos = c.user_data.setdefault("photos", [])
    photos.append(bytes(await f.download_as_bytearray()))
    n = len(photos)

    if n == 1:
        await u.message.reply_text(
            "✅ *Photo received!*\n\n"
            "You can add more photos for batch processing, "
            "or open the editor now.",
            parse_mode="Markdown",
            reply_markup=_collect_kb(n),
        )
    elif n < 20:
        await u.message.reply_text(
            f"✅ *Photo {n} added.*  Send more or open the editor.",
            parse_mode="Markdown",
            reply_markup=_collect_kb(n),
        )
    else:
        await u.message.reply_text("📦 20 photos queued. Opening editor…")
        await _open_editor(u.message, c)
        return ST_DASH

    return ST_COLLECTING


async def recv_photo_batch(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    return await recv_photo(u, c)


async def cb_collect(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()

    if q.data == "col:cancel":
        c.user_data.clear()
        c.user_data["settings"] = deepcopy(DEFAULTS)
        try:
            await q.edit_message_text("❌ Cancelled. Send a photo to start again.")
        except BadRequest:
            pass
        return ST_IDLE

    if q.data == "col:more":
        await q.answer("Send more photos now.", show_alert=False)
        return ST_COLLECTING

    # col:open
    try:
        await q.message.delete()
    except Exception:
        pass
    await _open_editor(q.message, c)
    return ST_DASH


# ── Dashboard callbacks ────────────────────────────────────────────────────────
async def cb_dash(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    q   = u.callback_query
    await q.answer()
    s   = c.user_data.setdefault("settings", deepcopy(DEFAULTS))
    uid = u.effective_user.id
    cmd = q.data[2:]   # strip "d:"

    # ── No-op label buttons ───────────────────────────────────────────────────
    if cmd == "noop":
        return ST_DASH

    # ── Cancel ────────────────────────────────────────────────────────────────
    if cmd == "cancel":
        c.user_data.clear()
        c.user_data["settings"] = deepcopy(DEFAULTS)
        try:
            await q.edit_message_caption(caption="❌ Cancelled — send a photo anytime!")
        except BadRequest:
            try:
                await q.edit_message_text("❌ Cancelled — send a photo anytime!")
            except BadRequest:
                pass
        return ST_IDLE

    # ── Apply / Done ──────────────────────────────────────────────────────────
    if cmd == "apply":
        photos = c.user_data.get("photos", [])
        if not photos:
            await q.answer("No photos — send a photo first!", show_alert=True)
            return ST_DASH

        try:
            await q.edit_message_caption(caption="⏳ Rendering…")
        except BadRequest:
            pass

        for i, pb in enumerate(photos):
            try:
                out = render_wm(pb, s)
                pos_n = _pos_name(s.get("pos", [4, 4]))
                cap   = (
                    f"✅  Photo {i+1}/{len(photos)}\n"
                    f"📍 {pos_n}  ·  🔆 {s.get('opacity',80)}%  ·  📐 {s.get('size',18)}%"
                )
                await q.message.reply_photo(io.BytesIO(out), caption=cap)
                db_inc(uid)
            except Exception as e:
                logger.exception("Render failed photo %d", i + 1)
                await q.message.reply_text(f"❌ Error on photo {i+1}: {e}")

        n = len(photos)
        await q.message.reply_text(
            f"🎉 *{n} photo{'s' if n>1 else ''}* done!\n\n"
            "_Send another photo whenever you're ready._",
            parse_mode="Markdown",
        )
        c.user_data.clear()
        c.user_data["settings"] = deepcopy(DEFAULTS)
        return ST_IDLE

    # ── Edit text ─────────────────────────────────────────────────────────────
    if cmd == "edit_text":
        cur = s.get("text", "")
        try:
            await q.edit_message_caption(
                caption=f"✏️ *Edit watermark text*\n\nCurrent: `{cur}`\n\nType your new text:",
                parse_mode="Markdown",
            )
        except BadRequest:
            await q.edit_message_text(f'✏️ Type new text (current: "{cur}"):')
        return ST_EDIT_TEXT

    # ── Edit logo ─────────────────────────────────────────────────────────────
    if cmd == "edit_logo":
        try:
            await q.edit_message_caption(
                caption="🖼 *Send your logo image*\n\n"
                        "Tip: send it as a *File* (tap 📎 → File) to keep maximum quality.",
                parse_mode="Markdown",
            )
        except BadRequest:
            await q.edit_message_text("🖼 Send your logo now. Use 📎 → File for best quality.")
        return ST_EDIT_LOGO

    # ── Switch to image mode ──────────────────────────────────────────────────
    if cmd == "to_image":
        s["mode"] = "image"
        if not s.get("logo"):
            try:
                await q.edit_message_caption(
                    caption="🖼 *Image logo mode*\n\n"
                            "Send your logo now. Use 📎 → File for best quality.",
                    parse_mode="Markdown",
                )
            except BadRequest:
                await q.edit_message_text("🖼 Send logo now — use 📎 → File for best quality.")
            return ST_EDIT_LOGO
        # Already have a logo — just switch and refresh
        await _refresh(q, c)
        return ST_DASH

    # ── Switch to text mode ───────────────────────────────────────────────────
    if cmd == "to_text":
        s["mode"] = "text"

    # ── Font cycle ────────────────────────────────────────────────────────────
    elif cmd == "font":
        fonts = list(LOADED_FONTS.keys()) or ["System"]
        idx   = fonts.index(s.get("font", "Roboto")) if s.get("font") in fonts else 0
        s["font"] = fonts[(idx + 1) % len(fonts)]

    # ── Open palette ──────────────────────────────────────────────────────────
    elif cmd == "palette":
        try:
            await q.edit_message_reply_markup(reply_markup=palette_kb())
        except BadRequest:
            pass
        return ST_DASH

    # ── Custom RGB ────────────────────────────────────────────────────────────
    elif cmd == "rgb":
        cur_hex = _hex(s.get("rgb", [255, 255, 255]))
        try:
            await q.edit_message_caption(
                caption=f"🖊 *Custom colour*\n\nCurrent: `{cur_hex}`\n\n"
                        "Send a hex code or three numbers:\n"
                        "`#FF8000`  or  `255 128 0`",
                parse_mode="Markdown",
            )
        except BadRequest:
            await q.edit_message_text(f"🖊 Send colour: `#FF8000`  or  `255 128 0`", parse_mode="Markdown")
        return ST_ENTER_RGB

    # ── Effect cycle ──────────────────────────────────────────────────────────
    elif cmd == "effect":
        idx = EFFECTS.index(s.get("effect", "shadow")) if s.get("effect") in EFFECTS else 0
        s["effect"] = EFFECTS[(idx + 1) % len(EFFECTS)]

    # ── Opacity ───────────────────────────────────────────────────────────────
    elif cmd == "op-":
        s["opacity"] = max(0, s.get("opacity", 80) - 5)
    elif cmd == "op+":
        s["opacity"] = min(100, s.get("opacity", 80) + 5)

    # ── Size ──────────────────────────────────────────────────────────────────
    elif cmd == "sz-":
        s["size"] = max(3, s.get("size", 18) - 2)
    elif cmd == "sz+":
        s["size"] = min(60, s.get("size", 18) + 2)

    # ── Rotation ──────────────────────────────────────────────────────────────
    elif cmd == "rot":
        rot = s.get("rotation", 0)
        idx = ROTATIONS.index(rot) if rot in ROTATIONS else 0
        s["rotation"] = ROTATIONS[(idx + 1) % len(ROTATIONS)]
    elif cmd == "rot0":
        s["rotation"] = 0

    # ── Position ──────────────────────────────────────────────────────────────
    elif cmd.startswith("pos:"):
        _, r, col = cmd.split(":")
        s["pos"] = [int(r), int(col)]

    # ── Background ────────────────────────────────────────────────────────────
    elif cmd == "bg":
        s["bg"] = {"photo":"light","light":"dark","dark":"photo"}[s.get("bg","photo")]

    # ── Quality ───────────────────────────────────────────────────────────────
    elif cmd == "quality":
        qidx   = QUALITY_STEPS.index(s.get("quality",92)) if s.get("quality") in QUALITY_STEPS else 2
        s["quality"] = QUALITY_STEPS[(qidx+1) % len(QUALITY_STEPS)]

    # ── Presets menu ──────────────────────────────────────────────────────────
    elif cmd == "presets":
        presets = db_get(uid).get("presets", {})
        if not presets:
            await q.answer("No presets saved yet. Save one from the editor!", show_alert=True)
            return ST_DASH
        try:
            await q.edit_message_reply_markup(reply_markup=presets_kb(uid))
        except BadRequest:
            pass
        return ST_DASH

    # ── Save preset ───────────────────────────────────────────────────────────
    elif cmd == "save":
        try:
            await q.edit_message_caption(
                caption="💾 *Save preset*\n\nType a short name (max 20 chars):",
                parse_mode="Markdown",
            )
        except BadRequest:
            await q.edit_message_text("💾 Type a name for this preset:")
        return ST_NAME_PRESET

    await _refresh(q, c)
    return ST_DASH


# ── Palette callback ───────────────────────────────────────────────────────────
async def cb_palette(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()

    if q.data == "pal:back":
        await _refresh(q, c)
        return ST_DASH

    parts = q.data.split(":")  # "pal:R:G:B"
    if len(parts) == 4:
        c.user_data["settings"]["rgb"] = [int(parts[1]), int(parts[2]), int(parts[3])]

    await _refresh(q, c)
    return ST_DASH


# ── Preset callbacks ───────────────────────────────────────────────────────────
async def cb_preset(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    q   = u.callback_query
    await q.answer()
    uid = u.effective_user.id
    cmd = q.data[3:]  # strip "pr:"

    if cmd == "back":
        await _refresh(q, c)
        return ST_DASH

    if cmd.startswith("load:"):
        name   = cmd[5:]
        preset = db_get(uid).get("presets", {}).get(name)
        if preset:
            c.user_data["settings"].update(preset)
            await q.answer(f"✅ '{name}' loaded!", show_alert=False)
        await _refresh(q, c)
        return ST_DASH

    if cmd.startswith("del:"):
        name = cmd[4:]
        user = db_get(uid)
        user.get("presets", {}).pop(name, None)
        db_put(uid, user)
        await q.answer(f"🗑 '{name}' deleted.")
        # Refresh presets menu
        try:
            await q.edit_message_reply_markup(reply_markup=presets_kb(uid))
        except BadRequest:
            pass
        return ST_DASH

    return ST_DASH


# ── Isolated text input ────────────────────────────────────────────────────────
async def recv_edit_text(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    text = u.message.text.strip()
    if not text:
        await u.message.reply_text("⚠️ Text can't be empty. Send your watermark text:")
        return ST_EDIT_TEXT
    c.user_data["settings"]["text"] = text
    c.user_data["settings"]["mode"] = "text"
    await u.message.reply_text(f'✅ Text set to: *"{text}"*', parse_mode="Markdown")
    await _open_editor(u.message, c)
    return ST_DASH


# ── Isolated logo input ────────────────────────────────────────────────────────
async def recv_edit_logo(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    """Completely isolated — the only thing expected here is the logo image."""
    f = await _get_img(u.message)
    if not f:
        await u.message.reply_text(
            "⚠️ Please send an image.\n\n"
            "Tip: send it as a *File* (📎 → File) to avoid Telegram compression.",
            parse_mode="Markdown",
        )
        return ST_EDIT_LOGO
    c.user_data["settings"]["logo"] = bytes(await f.download_as_bytearray())
    c.user_data["settings"]["mode"] = "image"
    await u.message.reply_text("✅ Logo saved!")
    await _open_editor(u.message, c)
    return ST_DASH


# ── Isolated RGB input ─────────────────────────────────────────────────────────
async def recv_rgb(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    raw = u.message.text.strip()
    rgb = None

    # Try #RRGGBB or RRGGBB
    m = re.match(r"^#?([0-9A-Fa-f]{6})$", raw)
    if m:
        h = m.group(1)
        rgb = [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)]
    else:
        parts = re.split(r"[\s,;]+", raw)
        if len(parts) == 3:
            try:
                vals = [int(p) for p in parts]
                if all(0 <= v <= 255 for v in vals):
                    rgb = vals
            except ValueError:
                pass

    if not rgb:
        await u.message.reply_text(
            "❌ Didn't recognise that.\n\n"
            "Try: `#FF8000`  or  `255 128 0`",
            parse_mode="Markdown",
        )
        return ST_ENTER_RGB

    c.user_data["settings"]["rgb"] = rgb
    await u.message.reply_text(f"✅ Colour set to `{_hex(rgb)}`", parse_mode="Markdown")
    await _open_editor(u.message, c)
    return ST_DASH


# ── Isolated preset name input ─────────────────────────────────────────────────
async def recv_preset_name(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    uid  = u.effective_user.id
    name = u.message.text.strip()[:20]
    if not name:
        await u.message.reply_text("⚠️ Name can't be empty. Send a name:")
        return ST_NAME_PRESET

    user = db_get(uid)
    p    = user.setdefault("presets", {})
    if len(p) >= 5:
        # Remove oldest
        del p[next(iter(p))]
    # Don't store raw logo bytes in JSON
    safe = {k: v for k, v in c.user_data["settings"].items() if k != "logo"}
    p[name] = safe
    db_put(uid, user)

    await u.message.reply_text(f"✅ Preset *{name}* saved!", parse_mode="Markdown")
    await _open_editor(u.message, c)
    return ST_DASH


# ── Post-init ──────────────────────────────────────────────────────────────────
async def _post_init(app: Application):
    _boot_fonts()
    await app.bot.set_my_commands([
        BotCommand("start",   "Start / restart"),
        BotCommand("help",    "How to use the bot"),
        BotCommand("mystats", "Your stats & presets"),
        BotCommand("reset",   "Clear saved presets"),
        BotCommand("cancel",  "Cancel & go back"),
    ])
    logger.info("Bot ready. Fonts: %s", list(LOADED_FONTS.keys()))


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError(
            "\n\nBOT_TOKEN not set!\n"
            "Run: export BOT_TOKEN='your_token_from_BotFather'\n"
        )

    IS_IMG = filters.PHOTO | filters.Document.IMAGE

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(IS_IMG,  recv_photo),
        ],
        states={
            ST_IDLE: [
                MessageHandler(IS_IMG, recv_photo),
            ],
            ST_COLLECTING: [
                MessageHandler(IS_IMG, recv_photo_batch),
                CallbackQueryHandler(cb_collect, pattern="^col:"),
            ],
            ST_DASH: [
                CallbackQueryHandler(cb_dash,    pattern="^d:"),
                CallbackQueryHandler(cb_palette, pattern="^pal:"),
                CallbackQueryHandler(cb_preset,  pattern="^pr:"),
                # Extra photos added while editor is open
                MessageHandler(IS_IMG, recv_photo_batch),
            ],
            ST_EDIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_edit_text),
            ],
            ST_EDIT_LOGO: [
                # Completely isolated — no other handler here
                MessageHandler(IS_IMG, recv_edit_logo),
            ],
            ST_ENTER_RGB: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_rgb),
            ],
            ST_NAME_PRESET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_preset_name),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=1200,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("mystats",     cmd_mystats))
    app.add_handler(CommandHandler("admin_stats", cmd_admin))
    app.add_handler(CommandHandler("reset",       cmd_reset))

    logger.info("TheWaterMarkBot running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
