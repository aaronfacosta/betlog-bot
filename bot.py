import os, re, uuid, base64, json
from datetime import date, datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters)

BOT_TOKEN      = os.environ.get("BOT_TOKEN","")
SUPA_URL       = os.environ.get("SUPA_URL","")
SUPA_KEY       = os.environ.get("SUPA_KEY","")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY","")
OPENAI_KEY     = os.environ.get("OPENAI_API_KEY","")
ALLOWED_IDS    = [int(x) for x in os.environ.get("ALLOWED_USER_IDS","").split(",") if x.strip()]
EUR            = 4
TIMEOUT        = 900

(S_TIPSTER, S_BOOKIE, S_TICKETS, S_MORE, S_DESC, S_CONFIRM) = range(6)
(P_LIST, P_DETAIL, P_EXACT, P_CONFIRM_RES, P_DEL) = range(10,15)

MENU_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📝 Nueva apuesta"), KeyboardButton("⏳ Pendientes")],
    [KeyboardButton("📊 Hoy"),           KeyboardButton("❌ Cancelar")]
], resize_keyboard=True)

BACK = [[InlineKeyboardButton("↩ Atrás", callback_data="back")]]

import httpx

def H():
    return {"apikey":SUPA_KEY,"Authorization":f"Bearer {SUPA_KEY}",
            "Content-Type":"application/json","Prefer":"return=minimal"}

async def sb_get(table, params=""):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{SUPA_URL}/rest/v1/{table}?{params}", headers=H())
        return r.json()

async def sb_post(table, data):
    async with httpx.AsyncClient() as c:
        return (await c.post(f"{SUPA_URL}/rest/v1/{table}", headers=H(), json=data)).status_code

async def sb_patch(table, col, val, data):
    h = H(); h["Prefer"] = "return=minimal"
    async with httpx.AsyncClient() as c:
        return (await c.patch(f"{SUPA_URL}/rest/v1/{table}?{col}=eq.{val}", headers=h, json=data)).status_code

async def sb_delete(table, col, val):
    async with httpx.AsyncClient() as c:
        return (await c.delete(f"{SUPA_URL}/rest/v1/{table}?{col}=eq.{val}", headers=H())).status_code

def gid():    return str(uuid.uuid4())[:12].replace("-","")
def fmt(n):   return f"{n:,.2f}"
def is_ok(u): return not ALLOWED_IDS or u.effective_user.id in ALLOWED_IDS
def now_str(): return datetime.now().strftime("%d-%m-%Y · %H:%M")

async def try_del(msg):
    try: await msg.delete()
    except: pass

async def clear(ctx, chat_id, bot):
    for m in ctx.user_data.get("_msgs",[]):
        try: await bot.delete_message(chat_id=chat_id, message_id=m)
        except: pass
    ctx.user_data["_msgs"] = []

async def track(ctx, msg):
    ctx.user_data.setdefault("_msgs",[]).append(msg.message_id)

async def delete_old_confirm(group_id, bot):
    try:
        g = await sb_get("bet_groups", f"id=eq.{group_id}&select=tg_chat_id,tg_msg_id")
        if isinstance(g,list) and g and g[0].get("tg_msg_id") and g[0].get("tg_chat_id"):
            await bot.delete_message(chat_id=g[0]["tg_chat_id"], message_id=g[0]["tg_msg_id"])
    except: pass

# ── Vision API ────────────────────────────────────────────────────────────────
async def analyze_bet_photo(image_bytes: bytes) -> dict:
    key = ANTHROPIC_KEY or OPENAI_KEY
    if not key:
        return {"error": "No hay API key configurada"}
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    prompt = """Analiza esta imagen de una apuesta deportiva. Extrae SOLO un JSON:
{
  "descripcion": "descripcion ultra concisa del pick",
  "tickets": [{"monto": 100.0, "cuota": 1.90}]
}

Reglas para la descripcion:
- Si es 1 seleccion: pon solo lo esencial. Ej: "Madrid -5.0" o "Over 2.5 tarjetas" o "Madrid gana" o "Alianza -1.5"
- Si son multiples selecciones (parlay/combinada): pon cada pick separado por " + ". Ej: "Mana gana + Rune Eaters gana + BIG gana + Game Hunters gana"
- Si una seleccion fue anulada/void: incluyela igual en el texto
- Omite nombres de equipos perdedores, nombres de torneos, IDs, fechas
- Usa el nombre del ganador/favorito o el mercado especifico. Sé ultra breve
- Nunca pongas "Múltiples partidos" ni descripciones vagas

Si ves retorno en vez de cuota: cuota = retorno/monto. Solo JSON, sin markdown."""
    try:
        async with httpx.AsyncClient(timeout=40) as cl:
            if ANTHROPIC_KEY:
                r = await cl.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
                    json={"model":"claude-haiku-4-5-20251001","max_tokens":512,"messages":[{"role":"user","content":[
                        {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}},
                        {"type":"text","text":prompt}]}]})
                data = r.json()
                if "content" not in data: return {"error":data.get("error",{}).get("message","Error API")[:200]}
                text = data["content"][0]["text"].strip()
            else:
                r = await cl.post("https://api.openai.com/v1/chat/completions",
                    headers={"Authorization":f"Bearer {OPENAI_KEY}","Content-Type":"application/json"},
                    json={"model":"gpt-4o-mini","messages":[{"role":"user","content":[
                        {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}","detail":"low"}},
                        {"type":"text","text":prompt}]}],"temperature":0,"max_tokens":512})
                data = r.json()
                if "choices" not in data: return {"error":data.get("error",{}).get("message","Error API")[:200]}
                text = data["choices"][0]["message"]["content"].strip()
        text = re.sub(r"```(?:json)?","",text).strip().rstrip("```").strip()
        return json.loads(text)
    except Exception as e:
        return {"error": str(e)}

# ── Ticket parser ─────────────────────────────────────────────────────────────
def parse_tickets(text, wnx=False):
    tickets, errors = [], []
    for i, line in enumerate(text.strip().splitlines(), 1):
        line = line.strip()
        if not line: continue
        m = re.match(r'^([\d.,]+)\s+@?([\d.,]+)$', line)
        if not m:
            errors.append(f"Línea {i}: `{line}` — usa `500 @1.90` o `500 285`")
            continue
        raw = float(m.group(1).replace(",","."))
        val = float(m.group(2).replace(",","."))
        stake = round(raw * EUR, 2) if wnx else raw
        has_at = "@" in line
        if has_at:
            if val <= 1: errors.append(f"Línea {i}: cuota debe ser > 1"); continue
            cuota = val; pot = round(stake * cuota, 2)
        else:
            ret = round(val * EUR, 2) if wnx else val
            if ret <= stake: errors.append(f"Línea {i}: retorno debe ser > stake"); continue
            pot = ret; cuota = round(pot / stake, 3)
        tickets.append({"stake":stake,"cuota":cuota,"potencial":pot,"eur":raw if wnx else None})
    return tickets, errors

# ── Auto investor stakes ──────────────────────────────────────────────────────
def get_auto_inv_stakes(s, tipster_name, total_stake):
    result = {}
    for inv in s.get("investors",[]):
        match = next((x for x in s.get("inv_tipster_stakes",[])
            if x["investor_id"]==inv["id"]
            and x["tipster"].lower()==tipster_name.lower()), None)
        if match and float(match["percentage"]) > 0:
            stake = round(total_stake * float(match["percentage"]) / 100, 2)
            result[inv["name"]] = {"stake":stake,"pct":float(match["percentage"]),"id":inv["id"]}
    return result

# ── State helpers ─────────────────────────────────────────────────────────────
def gs(ctx):
    ctx.user_data.setdefault("s",{
        "tipster":"","tipsters":[],"bookies_list":[],"investors":[],
        "inv_tipster_stakes":[],"desc":"","date":str(date.today()),
        "bookies":[],"cur_bookie":"","cur_wnx":False,"inv_stakes":{}
    })
    return ctx.user_data["s"]

def rs(ctx):
    ctx.user_data["s"] = {
        "tipster":"","tipsters":[],"bookies_list":[],"investors":[],
        "inv_tipster_stakes":[],"desc":"","date":str(date.today()),
        "bookies":[],"cur_bookie":"","cur_wnx":False,"inv_stakes":{}
    }
    ctx.user_data["_msgs"] = []

async def load_db(ctx):
    s = gs(ctx)
    tp  = await sb_get("tipsters","order=name.asc")
    bk  = await sb_get("bookies","order=name.asc")
    iv  = await sb_get("investors","order=name.asc")
    its = await sb_get("investor_tipster_stakes","")
    s["tipsters"]           = [t["name"] for t in tp]  if isinstance(tp,list) else []
    s["bookies_list"]       = [b["name"] for b in bk]  if isinstance(bk,list) else []
    s["investors"]          = iv                        if isinstance(iv,list) else []
    s["inv_tipster_stakes"] = its                       if isinstance(its,list) else []

# ── Build messages ────────────────────────────────────────────────────────────
def build_preview(s):
    all_t = [t for b in s["bookies"] for t in b["tickets"]]
    total_s = sum(t["stake"] for t in all_t)
    total_p = sum(t["potencial"] for t in all_t)
    cuota   = round(total_p/total_s,3) if total_s>0 else 0
    SEP = "╠═══════════════════════"
    lines = [
        "╔═══════════════════════",
        f"║ 📅 {now_str()}",
        f"║ 📋 {s['desc']}",
        f"║ 👤 {s['tipster']}",
        SEP,
    ]
    for b in s["bookies"]:
        lines.append(f"║ 🏠 {b['bookie']}")
        for t in b["tickets"]:
            eur = f" ({t['eur']}€)" if t.get("eur") else ""
            lines.append(f"║   {fmt(t['stake'])}{eur} @{t['cuota']} → pot {fmt(t['potencial'])}")
    lines += [SEP, f"║ @{cuota}  {fmt(total_s)} apostado  +{fmt(total_p-total_s)}"]
    if s["inv_stakes"]:
        for name, stake in s["inv_stakes"].items():
            if stake > 0:
                pot = round(stake * cuota, 2)
                lines.append(f"║ 💼 {name}: {fmt(stake)} → +{fmt(pot-stake)}")
    lines.append("╚═══════════════════════")
    return "\n".join(lines)

def build_result_msg(desc, tickets, rets, inv_group=None, comb_c=1, orig_date=None):
    ts  = sum(t["stake"] for t in tickets)
    tr  = sum(rets.values())
    pnl = round(tr - ts, 2)
    if pnl > 0.01:    status = "✅ *Apuesta ganada*"
    elif pnl < -0.01: status = "❌ *Apuesta perdida*"
    else:             status = "🔵 *Apuesta void*"
    d   = orig_date or now_str()
    SEP = "╠═══════════════════════"
    lines = ["╔═══════════════════════", f"║ 📅 {d}", f"║ 📋 {desc}", SEP]
    for t in tickets:
        r = rets.get(t["id"],0); sk = t["stake"]
        lbl = "✅" if r>sk else ("🔵" if abs(r-sk)<0.01 else "❌")
        lines.append(f"║ {lbl} {t.get('tipster','')} · {t.get('casa','')} → {fmt(r)}")
    lines += [SEP, f"║ P&L: *{'+' if pnl>=0 else ''}{fmt(pnl)}*"]
    if inv_group:
        for iid, data in inv_group.items():
            inv_pnl = round(data["stake"]*comb_c - data["stake"], 2)
            if abs(inv_pnl) >= 0.01:
                lines.append(f"║ 💼 {data.get('name','?')}: {'+' if inv_pnl>=0 else ''}{fmt(inv_pnl)}")
    lines += ["╚═══════════════════════", status]
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# NUEVA APUESTA
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_nueva(u:Update, ctx):
    if not is_ok(u): return
    rs(ctx); await load_db(ctx); await try_del(u.message)
    s = gs(ctx)
    rows = [[InlineKeyboardButton(t, callback_data=f"tip_{t}")] for t in s["tipsters"]] + BACK
    msg = await u.message.reply_text("👤 *Tipster:*",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track(ctx, msg); return S_TIPSTER

async def r_tipster(u:Update, ctx):
    q = u.callback_query; await q.answer()
    if q.data == "back":
        await q.edit_message_text("❌ Cancelado."); return ConversationHandler.END
    gs(ctx)["tipster"] = q.data[4:]
    return await ask_bookie(u, ctx)

async def ask_bookie(src, ctx):
    s = gs(ctx)
    await clear(ctx, src.effective_chat.id, ctx.bot)
    # Show already added bookies summary
    summary = ""
    if s["bookies"]:
        lines = []
        for b in s["bookies"]:
            for t in b["tickets"]:
                lines.append(f"  {b['bookie']} · {fmt(t['stake'])} @{t['cuota']}")
        summary = "_Ya agregados:_\n" + "\n".join(lines) + "\n\n"
    rows = []; row = []
    for b in s["bookies_list"]:
        row.append(InlineKeyboardButton(b, callback_data=f"bk_{b}"))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    # If already have bookies, add "Listo" button
    if s["bookies"]:
        rows.append([InlineKeyboardButton("✅ Listo, agregar descripción", callback_data="bk_done")])
    rows += BACK
    msg = await src.effective_message.reply_text(
        f"👤 *{s['tipster']}*\n\n{summary}🏠 *Elige bookie:*",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track(ctx, msg); return S_BOOKIE

async def r_bookie(u:Update, ctx):
    q = u.callback_query; await q.answer(); s = gs(ctx)
    if q.data == "back":
        if s["bookies"]:
            # Remove last bookie block
            s["bookies"].pop()
        return await ask_bookie(u, ctx)
    if q.data == "bk_done":
        return await ask_desc(u, ctx)
    bk = q.data[3:]
    s["cur_bookie"] = bk
    s["cur_wnx"] = "winamax" in bk.lower()
    await clear(ctx, u.effective_chat.id, ctx.bot)
    wnx_note = f"\n_Winamax: ingresa euros (×{EUR} = soles)_" if s["cur_wnx"] else ""
    msg = await u.effective_message.reply_text(
        f"🏠 *{bk}* — tickets:{wnx_note}\n\n"
        f"Una línea por ticket:\n`500 @1.90`  cuota\n`500 285`  retorno total",
        reply_markup=InlineKeyboardMarkup(BACK), parse_mode="Markdown")
    await track(ctx, msg); return S_TICKETS

async def r_tickets(u:Update, ctx):
    s = gs(ctx); await try_del(u.message)
    tickets, errors = parse_tickets(u.message.text, wnx=s["cur_wnx"])
    if errors:
        msg = await u.message.reply_text(
            "⚠️ Corrige y reenvía:\n" + "\n".join(errors),
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(BACK))
        await track(ctx, msg); return S_TICKETS
    for t in tickets:
        t["bookie"] = s["cur_bookie"]; t["tipster"] = s["tipster"]
    s["bookies"].append({"bookie": s["cur_bookie"], "tickets": tickets})
    return await ask_bookie(u, ctx)

async def ask_desc(src, ctx):
    s = gs(ctx)
    await clear(ctx, src.effective_chat.id, ctx.bot)
    # Show tickets summary so far
    all_t = [t for b in s["bookies"] for t in b["tickets"]]
    total_s = sum(t["stake"] for t in all_t)
    total_p = sum(t["potencial"] for t in all_t)
    cuota   = round(total_p/total_s,3) if total_s>0 else 0
    lines = ["📋 *Resumen tickets:*"]
    for b in s["bookies"]:
        lines.append(f"  🏠 {b['bookie']}")
        for t in b["tickets"]:
            eur = f" ({t['eur']}€)" if t.get("eur") else ""
            lines.append(f"    {fmt(t['stake'])}{eur} @{t['cuota']} → pot {fmt(t['potencial'])}")
    lines.append(f"\n@{cuota}  {fmt(total_s)} apostado  +{fmt(total_p-total_s)}")
    lines.append("\n✍️ ¿Descripción del pick?")
    msg = await src.effective_message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(BACK), parse_mode="Markdown")
    await track(ctx, msg); return S_DESC

async def r_desc(u:Update, ctx):
    s = gs(ctx); s["desc"] = u.message.text.strip(); s["date"] = str(date.today())
    await try_del(u.message)
    # Calculate auto inv stakes
    all_t = [t for b in s["bookies"] for t in b["tickets"]]
    total_stake = sum(t["stake"] for t in all_t)
    auto = get_auto_inv_stakes(s, s["tipster"], total_stake)
    s["inv_stakes"] = {k:v["stake"] for k,v in auto.items()} if auto else {}
    await clear(ctx, u.effective_chat.id, ctx.bot)
    preview = build_preview(s)
    msg = await u.message.reply_text(
        preview + "\n\n¿Confirmar?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirmar", callback_data="ok_yes"),
             InlineKeyboardButton("✏️ Editar desc", callback_data="ok_edit"),
             InlineKeyboardButton("❌ Cancelar",   callback_data="ok_no")]
        ]), parse_mode="Markdown")
    await track(ctx, msg); return S_CONFIRM

async def r_confirm(u:Update, ctx):
    q = u.callback_query; await q.answer(); s = gs(ctx)
    if q.data == "ok_no":
        rs(ctx); await q.edit_message_text("❌ Cancelado."); return ConversationHandler.END
    if q.data == "ok_edit":
        await q.edit_message_text("✍️ Nueva descripción:", parse_mode="Markdown")
        return S_DESC
    if q.data == "back":
        return await ask_desc(q, ctx)
    try:
        group_id = gid(); chat_id = u.effective_chat.id
        await sb_post("bet_groups",{"id":group_id,"date":s["date"],"descr":s["desc"],"status":"pending"})
        trows = []; all_tickets_flat = []
        for b in s["bookies"]:
            matched_bk = next((x for x in s["bookies_list"]
                if x.lower()==b["bookie"].lower() or x.lower() in b["bookie"].lower()
                or b["bookie"].lower() in x.lower()), b["bookie"])
            for t in b["tickets"]:
                tid = gid()
                trows.append({"id":tid,"group_id":group_id,"tipster":s["tipster"],"casa":matched_bk,
                    "stake":t["stake"],"cuota":t["cuota"],"potencial":t["potencial"],
                    "status":"pending","returned":None})
                all_tickets_flat.append({**t,"id":tid,"bookie":matched_bk})
        await sb_post("tickets", trows)
        ts_total = sum(t["stake"] for t in all_tickets_flat)
        irows = []
        for inv in s["investors"]:
            inv_stake = s["inv_stakes"].get(inv["name"], 0)
            if inv_stake <= 0: continue
            for t in all_tickets_flat:
                prop = t["stake"]/ts_total if ts_total>0 else 1/len(all_tickets_flat)
                irows.append({"id":gid(),"ticket_id":t["id"],"investor_id":inv["id"],
                    "stake":round(inv_stake*prop,2)})
        if irows: await sb_post("ticket_investors", irows)
        await clear(ctx, chat_id, ctx.bot)
        conf = build_preview(s) + "\n\n✅ *Apuesta registrada*"
        await q.edit_message_text(conf, parse_mode="Markdown")
        await sb_patch("bet_groups","id",group_id,{"tg_chat_id":chat_id,"tg_msg_id":q.message.message_id})
        rs(ctx)
    except Exception as e:
        await q.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

# ── Photo handler ─────────────────────────────────────────────────────────────
async def handle_photo(u: Update, ctx):
    if not is_ok(u): return
    if not ANTHROPIC_KEY and not OPENAI_KEY:
        await u.message.reply_text("⚠️ Sin API key de visión. Usa /nueva para ingresar manualmente.")
        return
    msg = await u.message.reply_text("🔍 Analizando imagen...")
    try:
        rs(ctx); await load_db(ctx)
        photo = u.message.photo[-1]
        file  = await ctx.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
        result = await analyze_bet_photo(bytes(img_bytes))
        if "error" in result:
            await msg.edit_text(f"❌ No pude analizar: {result['error']}"); return
        ctx.user_data["photo_result"] = result
        s = gs(ctx)
        rows = [[InlineKeyboardButton(t, callback_data=f"photo_tip_{t}")] for t in s["tipsters"]]
        if not rows:
            await msg.edit_text("⚠️ No hay tipsters registrados."); return
        desc = result.get("descripcion","(sin descripción)")
        tickets = result.get("tickets",[])
        preview = f"📋 *Detectado:*\n_{desc}_\n"
        for t in tickets:
            preview += f"\n  {t.get('monto',0)} @{t.get('cuota','?')}"
        await msg.edit_text(preview + "\n\nSelecciona tipster:",
            reply_markup=InlineKeyboardMarkup(
                rows + [[InlineKeyboardButton("❌ Cancelar", callback_data="photo_cancel")]]
            ), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

async def handle_photo_confirm(u: Update, ctx):
    q = u.callback_query; await q.answer()
    if q.data == "photo_cancel":
        await q.edit_message_text("❌ Cancelado."); return
    if q.data.startswith("photo_tip_"):
        rs(ctx); await load_db(ctx)
        s = gs(ctx); s["tipster"] = q.data[10:]
        result = ctx.user_data.get("photo_result",{})
        # We have desc and tickets but need bookie — go to bookie selection
        # Store photo tickets to pre-fill after bookie selected
        ctx.user_data["photo_tickets"] = result.get("tickets",[])
        s["desc"] = result.get("descripcion","")
        await q.edit_message_text(
            f"👤 *{s['tipster']}*\n\n🏠 Elige bookie para estos tickets:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(b, callback_data=f"photo_bk_{b}")] for b in s["bookies_list"]] +
                [[InlineKeyboardButton("❌ Cancelar", callback_data="photo_cancel")]]
            ), parse_mode="Markdown")

async def handle_photo_bookie(u: Update, ctx):
    q = u.callback_query; await q.answer()
    if q.data == "photo_cancel":
        await q.edit_message_text("❌ Cancelado."); return
    if q.data.startswith("photo_bk_"):
        s = gs(ctx); bk = q.data[9:]
        wnx = "winamax" in bk.lower()
        tickets_raw = ctx.user_data.get("photo_tickets",[])
        tickets = []
        for t in tickets_raw:
            try:
                # Handle both float and string with commas
                raw_monto = t.get("monto",0)
                raw_cuota = t.get("cuota",0)
                monto = float(str(raw_monto).replace(",",".").replace("€","").strip())
                cuota = float(str(raw_cuota).replace(",",".").strip())
                if monto > 0 and cuota > 1:
                    stake = round(monto * EUR, 2) if wnx else monto
                    pot   = round(stake * cuota, 2)
                    tickets.append({"stake":stake,"cuota":cuota,"potencial":pot,
                        "bookie":bk,"tipster":s["tipster"],"eur":monto if wnx else None})
                elif monto > 0 and cuota <= 0:
                    # cuota missing — skip
                    pass
            except: pass
        if not tickets:
            # Show raw data for debugging
            raw_str = str(tickets_raw)[:200]
            await q.edit_message_text(f"⚠️ No se detectaron tickets válidos.\n\n_Debug:_ `{raw_str}`",
                parse_mode="Markdown"); return
        s["bookies"].append({"bookie":bk,"tickets":tickets})
        # Calculate auto inv stakes
        total_stake = sum(t["stake"] for t in tickets)
        auto = get_auto_inv_stakes(s, s["tipster"], total_stake)
        s["inv_stakes"] = {k:v["stake"] for k,v in auto.items()} if auto else {}
        s["date"] = str(date.today())
        preview = build_preview(s)
        await q.edit_message_text(preview + "\n\n¿Confirmar?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar", callback_data="ok_yes"),
                 InlineKeyboardButton("❌ Cancelar",  callback_data="ok_no")]
            ]), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# PENDIENTES
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_pendientes(u:Update, ctx):
    if not is_ok(u): return
    await try_del(u.message); return await show_pending_list(u.message, ctx)

async def show_pending_list(src, ctx):
    groups   = await sb_get("bet_groups","status=eq.pending&order=date.desc")
    tickets  = await sb_get("tickets","order=group_id.asc")
    inv_rows = await sb_get("ticket_investors","")
    investors= await sb_get("investors","order=name.asc")
    if not isinstance(groups,list) or not groups:
        await src.reply_text("✅ Sin pendientes."); return P_LIST
    tmap = {}
    for t in (tickets if isinstance(tickets,list) else []): tmap.setdefault(t["group_id"],[]).append(t)
    imap = {}
    for ir in (inv_rows if isinstance(inv_rows,list) else []): imap.setdefault(ir["ticket_id"],[]).append(ir)
    inv_names = {i["id"]:i["name"] for i in (investors if isinstance(investors,list) else [])}
    ctx.user_data.update({"pd_groups":groups,"pd_tmap":tmap,"pd_imap":imap,"pd_inv_names":inv_names})
    rows = []
    for g in groups[:10]:
        ts = tmap.get(g["id"],[]); total_s = sum(t["stake"] for t in ts)
        total_p = sum(t["potencial"] for t in ts)
        cuota = round(total_p/total_s,3) if total_s>0 else 0
        rows.append([InlineKeyboardButton(
            f"▸ {g['descr'] or '(sin desc)'}  {fmt(total_s)} @{cuota}",
            callback_data=f"pd_{g['id']}")])
    await src.reply_text("⏳ *Pendientes:*",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    return P_LIST

async def r_pd_select(u:Update, ctx):
    q = u.callback_query; await q.answer()
    gid_val  = q.data[3:]
    groups   = ctx.user_data.get("pd_groups",[])
    g        = next((x for x in groups if x["id"]==gid_val), None)
    if not g: await q.edit_message_text("⚠️ No encontrado."); return ConversationHandler.END
    tmap     = ctx.user_data.get("pd_tmap",{})
    imap     = ctx.user_data.get("pd_imap",{})
    inv_names= ctx.user_data.get("pd_inv_names",{})
    ts       = tmap.get(gid_val,[])
    total_s  = sum(t["stake"] for t in ts)
    total_p  = sum(t["potencial"] for t in ts)
    cuota    = round(total_p/total_s,3) if total_s>0 else 0
    inv_totals = {}
    for t in ts:
        for ir in imap.get(t["id"],[]):
            inv_totals[ir["investor_id"]] = inv_totals.get(ir["investor_id"],0)+ir["stake"]
    lines = [f"*{g['descr']}*  _{g['date']}_",""]
    for i,t in enumerate(ts,1):
        lines.append(f"#{i} {t['tipster']} · {t['casa']}  {fmt(t['stake'])} @{t['cuota']} → {fmt(t['potencial'])}")
    lines += ["──────────────", f"@{cuota}  {fmt(total_s)} → pot {fmt(total_p)}"]
    if inv_totals:
        for iid,stake in inv_totals.items():
            pot = round(stake*cuota,2)
            lines.append(f"💼 {inv_names.get(iid,'?')}: {fmt(stake)} → +{fmt(pot-stake)}")
    ctx.user_data.update({"pd_gid":gid_val,"pd_tickets":ts})
    await q.edit_message_text("\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Win",    callback_data="pr_win"),
             InlineKeyboardButton("❌ Loss",   callback_data="pr_loss"),
             InlineKeyboardButton("🔵 Void",   callback_data="pr_void"),
             InlineKeyboardButton("✏️ Exacto", callback_data="pr_exact")],
            [InlineKeyboardButton("🗑 Eliminar", callback_data="pr_del"),
             InlineKeyboardButton("↩ Lista",    callback_data="pr_back")]
        ]), parse_mode="Markdown")
    return P_DETAIL

async def r_pd_action(u:Update, ctx):
    q = u.callback_query; await q.answer()
    if q.data == "pr_back": return await show_pending_list(q.message, ctx)
    if q.data == "pr_del":
        g = next((x for x in ctx.user_data.get("pd_groups",[])
            if x["id"]==ctx.user_data.get("pd_gid","")), {})
        await q.edit_message_text(f"⚠️ ¿Eliminar *{g.get('descr','')}*?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Sí", callback_data="pdel_yes"),
                InlineKeyboardButton("↩ No",  callback_data="pdel_no")
            ]]), parse_mode="Markdown")
        return P_DEL
    tickets = ctx.user_data.get("pd_tickets",[]); action = q.data[3:]
    ctx.user_data["pd_returns"] = {}; ctx.user_data["pd_idx"] = 0
    if action in ("win","loss","void"):
        for t in tickets:
            if action=="win":    ctx.user_data["pd_returns"][t["id"]] = t["potencial"]
            elif action=="loss": ctx.user_data["pd_returns"][t["id"]] = 0
            elif action=="void": ctx.user_data["pd_returns"][t["id"]] = t["stake"]
        if len(tickets)==1: return await save_result(q, ctx)
        return await show_result_summary(q, ctx)
    elif action=="exact": return await ask_exact_ticket(q, ctx)

async def ask_exact_ticket(src, ctx):
    tickets = ctx.user_data.get("pd_tickets",[]); idx = ctx.user_data.get("pd_idx",0)
    if idx >= len(tickets): return await show_result_summary(src, ctx)
    t = tickets[idx]
    try:
        await src.edit_message_text(
            f"✏️ *#{idx+1}/{len(tickets)}*  {t['tipster']} · {t['casa']}\n"
            f"{fmt(t['stake'])} @{t['cuota']} · pot {fmt(t['potencial'])}\n\nWin/Loss/Void o monto exacto:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Win",  callback_data="ex_win"),
                InlineKeyboardButton("❌ Loss", callback_data="ex_loss"),
                InlineKeyboardButton("🔵 Void", callback_data="ex_void"),
            ]]), parse_mode="Markdown")
    except:
        msg = await src.effective_message.reply_text(
            f"✏️ *#{idx+1}/{len(tickets)}*  {t['tipster']} · {t['casa']}\n"
            f"{fmt(t['stake'])} @{t['cuota']} · pot {fmt(t['potencial'])}\n\nWin/Loss/Void o monto exacto:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Win",  callback_data="ex_win"),
                InlineKeyboardButton("❌ Loss", callback_data="ex_loss"),
                InlineKeyboardButton("🔵 Void", callback_data="ex_void"),
            ]]), parse_mode="Markdown")
        await track(ctx, msg)
    return P_EXACT

async def r_exact_btn(u:Update, ctx):
    q = u.callback_query; await q.answer()
    tickets = ctx.user_data.get("pd_tickets",[]); idx = ctx.user_data.get("pd_idx",0)
    t = tickets[idx]
    if   q.data=="ex_win":  ctx.user_data["pd_returns"][t["id"]] = t["potencial"]
    elif q.data=="ex_loss": ctx.user_data["pd_returns"][t["id"]] = 0
    elif q.data=="ex_void": ctx.user_data["pd_returns"][t["id"]] = t["stake"]
    ctx.user_data["pd_idx"] += 1; return await ask_exact_ticket(q, ctx)

async def r_exact_txt(u:Update, ctx):
    try: ret = float(u.message.text.strip().replace(",",".")); assert ret>=0
    except: await u.message.reply_text("⚠️ Número válido:"); return P_EXACT
    await try_del(u.message)
    tickets = ctx.user_data.get("pd_tickets",[]); idx = ctx.user_data.get("pd_idx",0)
    ctx.user_data["pd_returns"][tickets[idx]["id"]] = ret
    ctx.user_data["pd_idx"] += 1; return await ask_exact_ticket(u.message, ctx)

async def show_result_summary(src, ctx):
    tickets = ctx.user_data.get("pd_tickets",[]); rets = ctx.user_data.get("pd_returns",{})
    tr = sum(rets.values()); ts = sum(t["stake"] for t in tickets); pnl = round(tr-ts,2)
    lines = []
    for t in tickets:
        r = rets.get(t["id"],0); s = t["stake"]
        lbl = "✅" if r>s else ("🔵" if abs(r-s)<0.01 else "❌")
        lines.append(f"{lbl} {t['tipster']} · {t['casa']}  {fmt(r)}")
    lines += ["──────────────", f"P&L  *{'+' if pnl>=0 else ''}{fmt(pnl)}*"]
    try:
        await src.edit_message_text("\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Guardar",  callback_data="rs_yes"),
                InlineKeyboardButton("❌ Cancelar", callback_data="rs_no")
            ]]), parse_mode="Markdown")
    except:
        msg = await src.effective_message.reply_text("\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Guardar",  callback_data="rs_yes"),
                InlineKeyboardButton("❌ Cancelar", callback_data="rs_no")
            ]]), parse_mode="Markdown")
        await track(ctx, msg)
    return P_CONFIRM_RES

async def r_result_confirm(u:Update, ctx):
    q = u.callback_query; await q.answer()
    if q.data=="rs_no": await q.edit_message_text("❌ Cancelado."); return ConversationHandler.END
    return await save_result(q, ctx)

async def save_result(src, ctx):
    tickets = ctx.user_data.get("pd_tickets",[]); rets = ctx.user_data.get("pd_returns",{})
    gid_val = ctx.user_data.get("pd_gid","")
    try:
        grp = await sb_get("bet_groups",f"id=eq.{gid_val}&select=descr,date")
        desc   = grp[0]["descr"] if isinstance(grp,list) and grp else "Apuesta"
        orig_d = grp[0].get("date","") if isinstance(grp,list) and grp else ""
        orig_date = (datetime.strptime(orig_d,"%Y-%m-%d").strftime("%d-%m-%Y")+" · "+
                     datetime.now().strftime("%H:%M")) if orig_d else None
        ts_total = sum(t["stake"] for t in tickets)
        tr_total = sum(rets.get(t["id"],0) for t in tickets)
        comb_c   = tr_total/ts_total if ts_total>0 else 1
        for t in tickets:
            await sb_patch("tickets","id",t["id"],{"returned":rets.get(t["id"],0),"status":"settled"})
        inv_group = {}
        investors_db = await sb_get("investors","order=name.asc")
        inv_names_db = {i["id"]:i["name"] for i in (investors_db if isinstance(investors_db,list) else [])}
        for t in tickets:
            rows = await sb_get("ticket_investors",f"ticket_id=eq.{t['id']}")
            if isinstance(rows,list):
                for ir in rows:
                    if ir["investor_id"] not in inv_group:
                        inv_group[ir["investor_id"]] = {"stake":0,"ticket_id":t["id"],
                            "name":inv_names_db.get(ir["investor_id"],"?")}
                    inv_group[ir["investor_id"]]["stake"] += ir["stake"]
        for iid, data in inv_group.items():
            inv_pnl = round(data["stake"]*comb_c - data["stake"],2)
            if abs(inv_pnl)<0.01: continue
            await sb_post("investor_movements",{"id":gid(),"investor_id":iid,
                "type":"bet_result","amount":inv_pnl,"note":desc,
                "date":str(date.today()),"ticket_id":data["ticket_id"]})
        await sb_patch("bet_groups","id",gid_val,{"status":"settled"})
        await delete_old_confirm(gid_val, ctx.bot)
        chat_id = src.message.chat_id if hasattr(src,"message") else src.effective_chat.id
        new_msg = await ctx.bot.send_message(chat_id=chat_id,
            text=build_result_msg(desc,tickets,rets,inv_group=inv_group,comb_c=comb_c,orig_date=orig_date),
            parse_mode="Markdown")
        await sb_patch("bet_groups","id",gid_val,{"tg_chat_id":chat_id,"tg_msg_id":new_msg.message_id})
        try:
            if hasattr(src,"message"): await src.message.delete()
        except: pass
    except Exception as e:
        if hasattr(src,"edit_message_text"): await src.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def r_pd_del_confirm(u:Update, ctx):
    q = u.callback_query; await q.answer()
    if q.data=="pdel_no": await q.edit_message_text("❌ Cancelado."); return ConversationHandler.END
    gid_val = ctx.user_data.get("pd_gid","")
    try:
        tix = await sb_get("tickets",f"group_id=eq.{gid_val}")
        if isinstance(tix,list):
            for t in tix:
                mvs = await sb_get("investor_movements",f"ticket_id=eq.{t['id']}")
                if isinstance(mvs,list):
                    for mv in mvs: await sb_delete("investor_movements","id",mv["id"])
                await sb_delete("ticket_investors","ticket_id",t["id"])
                await sb_delete("tickets","id",t["id"])
        await sb_delete("bet_groups","id",gid_val)
        await q.edit_message_text("✅ Eliminada.")
    except Exception as e: await q.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# HOY / START / CANCELAR / MENU
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(u:Update, ctx):
    if not is_ok(u): return
    await u.message.reply_text("👋 *BetLog*\nUsa los botones del menú.",
        parse_mode="Markdown", reply_markup=MENU_KB)

async def cmd_hoy(u:Update, ctx):
    if not is_ok(u): return
    await try_del(u.message)
    today = str(date.today())
    groups = await sb_get("bet_groups",f"date=eq.{today}&order=created_at.desc")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("📭 Sin apuestas hoy."); return
    tall = await sb_get("tickets","order=group_id.asc"); tmap = {}
    for t in (tall if isinstance(tall,list) else []): tmap.setdefault(t["group_id"],[]).append(t)
    lines = [f"📊 *{today}*\n"]; total=0; total_pnl=0
    for g in groups:
        ts = tmap.get(g["id"],[]); gs_s = sum(t["stake"] for t in ts); total+=gs_s
        icon = "⏳" if g["status"]=="pending" else "✅"
        pnl_str = ""
        if g["status"]=="settled":
            ret = sum((t.get("returned") or 0) for t in ts)
            p = round(ret-gs_s,2); total_pnl+=p
            pnl_str = f"  *{'+' if p>=0 else ''}{fmt(p)}*"
        cuota = round(sum(t["potencial"] for t in ts)/gs_s,3) if gs_s>0 else 0
        lines.append(f"{icon} *{g['descr'] or '(sin desc)'}*  {fmt(gs_s)} @{cuota}{pnl_str}")
    lines.append(f"\n💰 {fmt(total)}")
    if total_pnl!=0: lines.append(f"📈 P&L  *{'+' if total_pnl>=0 else ''}{fmt(total_pnl)}*")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_cancelar(u:Update, ctx):
    await clear(ctx, u.effective_chat.id, ctx.bot)
    rs(ctx); await u.message.reply_text("❌ Cancelado.", reply_markup=MENU_KB)
    return ConversationHandler.END

async def menu_handler(u:Update, ctx):
    txt = u.message.text
    if txt=="📝 Nueva apuesta": return await cmd_nueva(u, ctx)
    if txt=="⏳ Pendientes":    return await cmd_pendientes(u, ctx)
    if txt=="📊 Hoy":           await cmd_hoy(u, ctx)
    if txt=="❌ Cancelar":      return await cmd_cancelar(u, ctx)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    nueva_conv = ConversationHandler(
        entry_points=[CommandHandler("nueva",cmd_nueva),
                      MessageHandler(filters.Regex("^📝 Nueva apuesta$"),cmd_nueva)],
        states={
            S_TIPSTER: [CallbackQueryHandler(r_tipster, pattern="^(tip_|back)")],
            S_BOOKIE:  [CallbackQueryHandler(r_bookie,  pattern="^(bk_|back)")],
            S_TICKETS: [CallbackQueryHandler(r_bookie,  pattern="^back"),
                        MessageHandler(filters.TEXT&~filters.COMMAND, r_tickets)],
            S_DESC:    [CallbackQueryHandler(r_bookie,  pattern="^back"),
                        MessageHandler(filters.TEXT&~filters.COMMAND, r_desc)],
            S_CONFIRM: [CallbackQueryHandler(r_confirm, pattern="^(ok_|back)")],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        conversation_timeout=TIMEOUT,
        allow_reentry=True,
    )

    pending_conv = ConversationHandler(
        entry_points=[CommandHandler("pendientes",cmd_pendientes),
                      MessageHandler(filters.Regex("^⏳ Pendientes$"),cmd_pendientes)],
        states={
            P_LIST:       [CallbackQueryHandler(r_pd_select,      pattern="^pd_")],
            P_DETAIL:     [CallbackQueryHandler(r_pd_action,      pattern="^pr_")],
            P_EXACT:      [CallbackQueryHandler(r_exact_btn,      pattern="^ex_"),
                           MessageHandler(filters.TEXT&~filters.COMMAND, r_exact_txt)],
            P_CONFIRM_RES:[CallbackQueryHandler(r_result_confirm, pattern="^rs_")],
            P_DEL:        [CallbackQueryHandler(r_pd_del_confirm, pattern="^pdel_")],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        conversation_timeout=TIMEOUT,
        allow_reentry=True,
    )

    app.add_handler(nueva_conv)
    app.add_handler(pending_conv)
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CommandHandler("hoy",      cmd_hoy))
    app.add_handler(MessageHandler(filters.Regex("^(📊 Hoy|❌ Cancelar)$"), menu_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_photo_confirm, pattern="^photo_tip_"))
    app.add_handler(CallbackQueryHandler(handle_photo_bookie,  pattern="^photo_bk_"))
    app.add_handler(CallbackQueryHandler(handle_photo_confirm, pattern="^photo_cancel"))

    print("BetLog Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
