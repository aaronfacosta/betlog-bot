import os
import json
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from datetime import date
import uuid

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
SUPA_URL    = os.environ.get("SUPA_URL", "")
SUPA_KEY    = os.environ.get("SUPA_KEY", "")
ALLOWED_IDS = [int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()]
EUR_TO_SOLES = 4

# ── STATES ───────────────────────────────────────────────────────────────────
(
    S_DESC, S_DATE, S_TIPSTER, S_BOOKIE, S_STAKE,
    S_CUOTA_OR_POT, S_CUOTA, S_POTENCIAL,
    S_MORE_TICKETS, S_INVESTORS, S_CONFIRM
) = range(11)

# ── SUPABASE HELPERS ─────────────────────────────────────────────────────────
HEADERS = lambda: {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json", "Prefer": "return=minimal"}

async def sb_get(table, params=""):
    url = f"{SUPA_URL}/rest/v1/{table}?{params}"
    async with httpx.AsyncClient() as c:
        r = await c.get(url, headers=HEADERS())
        return r.json()

async def sb_insert(table, data):
    url = f"{SUPA_URL}/rest/v1/{table}"
    async with httpx.AsyncClient() as c:
        r = await c.post(url, headers=HEADERS(), json=data)
        return r.status_code

async def sb_insert_many(table, rows):
    url = f"{SUPA_URL}/rest/v1/{table}"
    async with httpx.AsyncClient() as c:
        r = await c.post(url, headers=HEADERS(), json=rows)
        return r.status_code

def gen_id():
    return str(uuid.uuid4())[:12].replace("-","")

def fmt(n):
    return f"{n:,.2f}"

# ── AUTH CHECK ───────────────────────────────────────────────────────────────
def is_allowed(update: Update):
    if not ALLOWED_IDS:
        return True
    return update.effective_user.id in ALLOWED_IDS

# ── HELPERS ──────────────────────────────────────────────────────────────────
def get_session(ctx):
    if "session" not in ctx.user_data:
        ctx.user_data["session"] = {
            "desc": "", "date": str(date.today()),
            "tickets": [], "current": {},
            "tipsters": [], "bookies": [], "investors": []
        }
    return ctx.user_data["session"]

def reset_session(ctx):
    ctx.user_data["session"] = {
        "desc": "", "date": str(date.today()),
        "tickets": [], "current": {},
        "tipsters": [], "bookies": [], "investors": []
    }

async def load_lists(ctx):
    s = get_session(ctx)
    tp = await sb_get("tipsters", "order=name.asc")
    bk = await sb_get("bookies", "order=name.asc")
    iv = await sb_get("investors", "order=name.asc")
    s["tipsters"] = [t["name"] for t in tp] if isinstance(tp, list) else []
    s["bookies"]  = [b["name"] for b in bk] if isinstance(bk, list) else []
    s["investors"] = iv if isinstance(iv, list) else []

def ticket_summary(t):
    wnx = t.get("winamax")
    stake_str = f"{t['euros_raw']}€ (={fmt(t['stake'])} soles)" if wnx else fmt(t['stake'])
    return (f"  📌 #{t['num']} {t['tipster']} · {t['bookie']}\n"
            f"     Stake: {stake_str} · Cuota: {t['cuota']} · Pot: {fmt(t['potencial'])}")

def session_summary(s):
    lines = [f"📋 *{s['desc']}*", f"📅 {s['date']}", ""]
    for t in s["tickets"]:
        lines.append(ticket_summary(t))
    if s.get("inv_assignments"):
        lines.append("\n👥 Inversores:")
        for inv_name, data in s["inv_assignments"].items():
            if data["stake"] > 0:
                wnx_str = f" ({data['euros_raw']}€)" if data.get("euros_raw") else ""
                lines.append(f"  • {inv_name}: {fmt(data['stake'])} soles{wnx_str}")
    return "\n".join(lines)

# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text(
        "👋 *BetLog Bot*\n\nComandos:\n"
        "• /nueva — registrar apuesta\n"
        "• /pendientes — ver apuestas pendientes\n"
        "• /hoy — resumen del día\n"
        "• /cancelar — cancelar registro actual",
        parse_mode="Markdown"
    )

# ── /nueva ────────────────────────────────────────────────────────────────────
async def cmd_nueva(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    reset_session(ctx)
    await load_lists(ctx)
    await update.message.reply_text(
        "✍️ *Nueva apuesta*\n\n¿Cuál es la descripción? (ej: Real Madrid gana · PSG vs Bayern O2.5)",
        parse_mode="Markdown"
    )
    return S_DESC

async def recv_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(ctx)
    s["desc"] = update.message.text.strip()
    today = str(date.today())
    keyboard = [[
        InlineKeyboardButton(f"📅 Hoy ({today})", callback_data=f"date_{today}"),
        InlineKeyboardButton("✏️ Otra fecha", callback_data="date_custom")
    ]]
    await update.message.reply_text(
        f"📅 ¿Fecha de la apuesta?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return S_DATE

async def recv_date_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(ctx)
    if q.data == "date_custom":
        await q.edit_message_text("✏️ Escribe la fecha (formato YYYY-MM-DD):")
        return S_DATE
    s["date"] = q.data.replace("date_","")
    return await ask_tipster(q, ctx)

async def recv_date_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(ctx)
    txt = update.message.text.strip()
    try:
        from datetime import datetime
        datetime.strptime(txt, "%Y-%m-%d")
        s["date"] = txt
    except:
        await update.message.reply_text("⚠️ Formato incorrecto. Usa YYYY-MM-DD (ej: 2026-03-15)")
        return S_DATE
    return await ask_tipster(update, ctx)

async def ask_tipster(source, ctx):
    s = get_session(ctx)
    s["current"] = {"num": len(s["tickets"]) + 1}
    tips = s["tipsters"]
    if not tips:
        if hasattr(source, 'edit_message_text'):
            await source.edit_message_text("✍️ Escribe el nombre del tipster:")
        else:
            await source.reply_text("✍️ Escribe el nombre del tipster:")
        return S_TIPSTER
    rows = [[InlineKeyboardButton(t, callback_data=f"tip_{t}")] for t in tips]
    msg = "👤 *Tipster:*"
    if hasattr(source, 'edit_message_text'):
        await source.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    else:
        await source.reply_text(msg, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    return S_TIPSTER

async def recv_tipster_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(ctx)
    s["current"]["tipster"] = q.data.replace("tip_","")
    return await ask_bookie(q, ctx)

async def recv_tipster_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(ctx)
    s["current"]["tipster"] = update.message.text.strip()
    return await ask_bookie(update, ctx)

async def ask_bookie(source, ctx):
    s = get_session(ctx)
    bks = s["bookies"]
    if not bks:
        if hasattr(source, 'edit_message_text'):
            await source.edit_message_text("🏠 Escribe el bookie:")
        else:
            await source.reply_text("🏠 Escribe el bookie:")
        return S_BOOKIE
    rows = []
    row = []
    for i, b in enumerate(bks):
        row.append(InlineKeyboardButton(b, callback_data=f"bk_{b}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)
    msg = "🏠 *Bookie:*"
    if hasattr(source, 'edit_message_text'):
        await source.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    else:
        await source.reply_text(msg, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    return S_BOOKIE

async def recv_bookie_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(ctx)
    bookie = q.data.replace("bk_","")
    s["current"]["bookie"] = bookie
    s["current"]["winamax"] = "winamax" in bookie.lower()
    return await ask_stake(q, ctx)

async def recv_bookie_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(ctx)
    bookie = update.message.text.strip()
    s["current"]["bookie"] = bookie
    s["current"]["winamax"] = "winamax" in bookie.lower()
    return await ask_stake(update, ctx)

async def ask_stake(source, ctx):
    s = get_session(ctx)
    is_wnx = s["current"].get("winamax")
    label = "💶 ¿Cuántos *euros* apostás? (se guardará ×4 en soles)" if is_wnx else "💰 ¿Cuánto es el *stake* (soles)?"
    if hasattr(source, 'edit_message_text'):
        await source.edit_message_text(label, parse_mode="Markdown")
    else:
        await source.reply_text(label, parse_mode="Markdown")
    return S_STAKE

async def recv_stake(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(ctx)
    try:
        raw = float(update.message.text.strip().replace(",","."))
    except:
        await update.message.reply_text("⚠️ Ingresa un número válido:")
        return S_STAKE
    is_wnx = s["current"].get("winamax")
    if is_wnx:
        s["current"]["euros_raw"] = raw
        s["current"]["stake"] = round(raw * EUR_TO_SOLES, 2)
        await update.message.reply_text(f"✅ {raw}€ = *{fmt(s['current']['stake'])} soles*", parse_mode="Markdown")
    else:
        s["current"]["stake"] = raw
    keyboard = [[
        InlineKeyboardButton("@ Cuota", callback_data="mode_cuota"),
        InlineKeyboardButton("↩ Retorno total", callback_data="mode_retorno")
    ]]
    await update.message.reply_text("¿Ingresas cuota o retorno total?", reply_markup=InlineKeyboardMarkup(keyboard))
    return S_CUOTA_OR_POT

async def recv_mode_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(ctx)
    mode = q.data.replace("mode_","")
    s["current"]["mode"] = mode
    if mode == "cuota":
        await q.edit_message_text(f"📊 ¿Cuál es la *cuota*? (ej: 1.90)", parse_mode="Markdown")
        return S_CUOTA
    else:
        await q.edit_message_text(f"💵 ¿Cuál es el *retorno total* esperado? (ej: 285)", parse_mode="Markdown")
        return S_POTENCIAL

async def recv_cuota(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(ctx)
    try:
        cuota = float(update.message.text.strip().replace(",","."))
        if cuota <= 1: raise ValueError
    except:
        await update.message.reply_text("⚠️ Cuota debe ser mayor a 1.0:")
        return S_CUOTA
    stake = s["current"]["stake"]
    pot = round(stake * cuota, 2)
    gan = round(pot - stake, 2)
    s["current"]["cuota"] = cuota
    s["current"]["potencial"] = pot
    await update.message.reply_text(
        f"✅ Cuota *{cuota}* → Potencial *{fmt(pot)}* soles · Ganancia neta *+{fmt(gan)}*",
        parse_mode="Markdown"
    )
    return await ask_more_tickets(update, ctx)

async def recv_potencial(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(ctx)
    try:
        pot = float(update.message.text.strip().replace(",","."))
        stake = s["current"]["stake"]
        if pot <= stake: raise ValueError
    except:
        await update.message.reply_text("⚠️ El retorno debe ser mayor al stake:")
        return S_POTENCIAL
    cuota = round(pot / stake, 3)
    gan = round(pot - stake, 2)
    s["current"]["cuota"] = cuota
    s["current"]["potencial"] = pot
    await update.message.reply_text(
        f"✅ Retorno *{fmt(pot)}* → Cuota implícita *{cuota}* · Ganancia neta *+{fmt(gan)}*",
        parse_mode="Markdown"
    )
    return await ask_more_tickets(update, ctx)

async def ask_more_tickets(source, ctx):
    s = get_session(ctx)
    s["tickets"].append(dict(s["current"]))
    keyboard = [[
        InlineKeyboardButton("➕ Otro ticket", callback_data="more_yes"),
        InlineKeyboardButton("✅ Listo", callback_data="more_no")
    ]]
    n = len(s["tickets"])
    await source.reply_text(
        f"Ticket #{n} agregado. ¿Agregar otro ticket a esta apuesta?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return S_MORE_TICKETS

async def recv_more_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(ctx)
    if q.data == "more_yes":
        s["current"] = {"num": len(s["tickets"]) + 1, "tipster": s["tickets"][0]["tipster"]}
        return await ask_bookie(q, ctx)
    return await ask_investors(q, ctx)

async def ask_investors(source, ctx):
    s = get_session(ctx)
    if not s["investors"]:
        s["inv_assignments"] = {}
        return await show_confirm(source, ctx)
    s["inv_idx"] = 0
    s["inv_assignments"] = {}
    return await ask_one_investor(source, ctx)

async def ask_one_investor(source, ctx):
    s = get_session(ctx)
    idx = s["inv_idx"]
    if idx >= len(s["investors"]):
        return await show_confirm(source, ctx)
    inv = s["investors"][idx]
    is_wnx = any(t.get("winamax") for t in s["tickets"])
    label = f"💶 Stake de *{inv['name']}* en euros (0 si no participa):" if is_wnx else f"💰 Stake de *{inv['name']}* (0 si no participa):"
    keyboard = [[InlineKeyboardButton("⏭ Saltar", callback_data="inv_skip")]]
    if hasattr(source, 'edit_message_text'):
        await source.edit_message_text(label, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await source.reply_text(label, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return S_INVESTORS

async def recv_investor_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(ctx)
    s["inv_idx"] += 1
    return await ask_one_investor(q, ctx)

async def recv_investor_stake(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(ctx)
    try:
        raw = float(update.message.text.strip().replace(",","."))
    except:
        await update.message.reply_text("⚠️ Número válido:")
        return S_INVESTORS
    idx = s["inv_idx"]
    inv = s["investors"][idx]
    is_wnx = any(t.get("winamax") for t in s["tickets"])
    if is_wnx and raw > 0:
        soles = round(raw * EUR_TO_SOLES, 2)
        s["inv_assignments"][inv["name"]] = {"investor_id": inv["id"], "stake": soles, "euros_raw": raw}
        await update.message.reply_text(f"✅ {inv['name']}: {raw}€ = *{fmt(soles)} soles*", parse_mode="Markdown")
    else:
        s["inv_assignments"][inv["name"]] = {"investor_id": inv["id"], "stake": raw}
    s["inv_idx"] += 1
    return await ask_one_investor(update, ctx)

async def show_confirm(source, ctx):
    s = get_session(ctx)
    summary = session_summary(s)
    keyboard = [[
        InlineKeyboardButton("✅ Confirmar y guardar", callback_data="confirm_yes"),
        InlineKeyboardButton("❌ Cancelar", callback_data="confirm_no")
    ]]
    msg = f"{summary}\n\n¿Guardar esta apuesta?"
    if hasattr(source, 'edit_message_text'):
        await source.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await source.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return S_CONFIRM

async def recv_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = get_session(ctx)
    if q.data == "confirm_no":
        reset_session(ctx)
        await q.edit_message_text("❌ Apuesta cancelada.")
        return ConversationHandler.END
    # SAVE TO SUPABASE
    try:
        gid = gen_id()
        await sb_insert("bet_groups", {"id": gid, "date": s["date"], "descr": s["desc"], "status": "pending"})
        ticket_rows = []
        for t in s["tickets"]:
            tid = gen_id()
            t["_id"] = tid
            ticket_rows.append({
                "id": tid, "group_id": gid,
                "tipster": t["tipster"], "casa": t["bookie"],
                "stake": t["stake"], "cuota": t["cuota"],
                "potencial": t["potencial"], "status": "pending", "returned": None
            })
        await sb_insert_many("tickets", ticket_rows)
        # Investor assignments — apply to all tickets proportionally
        inv_rows = []
        for inv_name, data in s.get("inv_assignments", {}).items():
            if data["stake"] <= 0: continue
            for t in s["tickets"]:
                inv_rows.append({
                    "id": gen_id(), "ticket_id": t["_id"],
                    "investor_id": data["investor_id"],
                    "stake": round(data["stake"] / len(s["tickets"]), 2)
                })
        if inv_rows:
            await sb_insert_many("ticket_investors", inv_rows)
        total_stake = sum(t["stake"] for t in s["tickets"])
        total_pot = sum(t["potencial"] for t in s["tickets"])
        await q.edit_message_text(
            f"✅ *¡Apuesta guardada!*\n\n"
            f"📋 {s['desc']}\n"
            f"📅 {s['date']} · {len(s['tickets'])} ticket(s)\n"
            f"💰 Apostado: {fmt(total_stake)} soles\n"
            f"🎯 Potencial: {fmt(total_pot)} soles\n"
            f"📈 Ganancia neta: +{fmt(total_pot - total_stake)} soles",
            parse_mode="Markdown"
        )
        reset_session(ctx)
    except Exception as e:
        await q.edit_message_text(f"❌ Error al guardar: {str(e)}")
    return ConversationHandler.END

# ── /pendientes ───────────────────────────────────────────────────────────────
async def cmd_pendientes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    groups = await sb_get("bet_groups", "status=eq.pending&order=date.desc")
    tickets = await sb_get("tickets", "status=eq.pending")
    if not isinstance(groups, list) or not groups:
        await update.message.reply_text("✅ No hay apuestas pendientes.")
        return
    tmap = {}
    for t in (tickets if isinstance(tickets, list) else []):
        tmap.setdefault(t["group_id"], []).append(t)
    lines = [f"⏳ *{len(groups)} apuesta(s) pendiente(s):*\n"]
    for g in groups[:10]:
        ts = tmap.get(g["id"], [])
        total_stake = sum(t["stake"] for t in ts)
        total_pot = sum(t["potencial"] for t in ts)
        lines.append(f"📋 *{g['descr'] or '(sin desc)'}*")
        lines.append(f"   📅 {g['date']} · Stake: {fmt(total_stake)} · Pot: {fmt(total_pot)}")
        for t in ts:
            lines.append(f"   └ {t['tipster']} · {t['casa']} · @{t['cuota']}")
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── /hoy ──────────────────────────────────────────────────────────────────────
async def cmd_hoy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    today = str(date.today())
    groups = await sb_get("bet_groups", f"date=eq.{today}&order=created_at.desc")
    if not isinstance(groups, list) or not groups:
        await update.message.reply_text(f"📭 Sin apuestas registradas hoy ({today}).")
        return
    tickets_all = await sb_get("tickets", f"order=group_id.asc")
    tmap = {}
    for t in (tickets_all if isinstance(tickets_all, list) else []):
        tmap.setdefault(t["group_id"], []).append(t)
    lines = [f"📊 *Apuestas de hoy ({today}):*\n"]
    total_stake = 0
    for g in groups:
        ts = tmap.get(g["id"], [])
        gs = sum(t["stake"] for t in ts)
        total_stake += gs
        status_icon = "⏳" if g["status"] == "pending" else "✅"
        lines.append(f"{status_icon} *{g['descr'] or '(sin desc)'}* — {fmt(gs)} soles")
    lines.append(f"\n💰 Total apostado hoy: *{fmt(total_stake)} soles*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── /cancelar ─────────────────────────────────────────────────────────────────
async def cmd_cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset_session(ctx)
    await update.message.reply_text("❌ Registro cancelado.")
    return ConversationHandler.END

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("nueva", cmd_nueva)],
        states={
            S_DESC:         [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_desc)],
            S_DATE:         [CallbackQueryHandler(recv_date_cb, pattern="^date_"),
                             MessageHandler(filters.TEXT & ~filters.COMMAND, recv_date_text)],
            S_TIPSTER:      [CallbackQueryHandler(recv_tipster_cb, pattern="^tip_"),
                             MessageHandler(filters.TEXT & ~filters.COMMAND, recv_tipster_text)],
            S_BOOKIE:       [CallbackQueryHandler(recv_bookie_cb, pattern="^bk_"),
                             MessageHandler(filters.TEXT & ~filters.COMMAND, recv_bookie_text)],
            S_STAKE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_stake)],
            S_CUOTA_OR_POT: [CallbackQueryHandler(recv_mode_cb, pattern="^mode_")],
            S_CUOTA:        [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_cuota)],
            S_POTENCIAL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_potencial)],
            S_MORE_TICKETS: [CallbackQueryHandler(recv_more_cb, pattern="^more_")],
            S_INVESTORS:    [CallbackQueryHandler(recv_investor_skip, pattern="^inv_skip"),
                             MessageHandler(filters.TEXT & ~filters.COMMAND, recv_investor_stake)],
            S_CONFIRM:      [CallbackQueryHandler(recv_confirm, pattern="^confirm_")],
        },
        fallbacks=[CommandHandler("cancelar", cmd_cancelar)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(CommandHandler("hoy", cmd_hoy))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))

    print("🤖 BetLog Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
