import os
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from datetime import date
import uuid

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
SUPA_URL     = os.environ.get("SUPA_URL", "")
SUPA_KEY     = os.environ.get("SUPA_KEY", "")
ALLOWED_IDS  = [int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()]
EUR_TO_SOLES = 4

(S_DESC, S_TIPSTER, S_BOOKIE, S_STAKE, S_CUOTA_RET,
 S_MORE_TICKETS, S_INV_STAKE, S_CONFIRM) = range(8)

(SR_PICK_GROUP, SR_PICK_TICKET, SR_SET_RETURN) = range(8, 11)
(SC_PICK_GROUP, SC_PICK_TICKET, SC_SET_RETURN) = range(11, 14)

MENU_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📝 Nueva apuesta"), KeyboardButton("⏳ Pendientes")],
    [KeyboardButton("📊 Hoy"),           KeyboardButton("🔧 Corregir")],
    [KeyboardButton("❌ Cancelar")]
], resize_keyboard=True)

def H():
    return {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
            "Content-Type": "application/json", "Prefer": "return=minimal"}

async def sb_get(table, params=""):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{SUPA_URL}/rest/v1/{table}?{params}", headers=H())
        return r.json()

async def sb_insert(table, data):
    async with httpx.AsyncClient() as c:
        return (await c.post(f"{SUPA_URL}/rest/v1/{table}", headers=H(), json=data)).status_code

async def sb_insert_many(table, rows):
    async with httpx.AsyncClient() as c:
        return (await c.post(f"{SUPA_URL}/rest/v1/{table}", headers=H(), json=rows)).status_code

async def sb_patch(table, col, val, data):
    h = H(); h["Prefer"] = "return=minimal"
    async with httpx.AsyncClient() as c:
        return (await c.patch(f"{SUPA_URL}/rest/v1/{table}?{col}=eq.{val}", headers=h, json=data)).status_code

async def sb_delete(table, col, val):
    async with httpx.AsyncClient() as c:
        return (await c.delete(f"{SUPA_URL}/rest/v1/{table}?{col}=eq.{val}", headers=H())).status_code

def gen_id(): return str(uuid.uuid4())[:12].replace("-", "")
def fmt(n):   return f"{n:,.2f}"
def is_ok(u): return not ALLOWED_IDS or u.effective_user.id in ALLOWED_IDS

async def try_delete(msg):
    try: await msg.delete()
    except: pass

async def edit_or_reply(src, text, kbd=None, md="Markdown"):
    markup = InlineKeyboardMarkup(kbd) if kbd else None
    if hasattr(src, 'edit_message_text'):
        try: await src.edit_message_text(text, reply_markup=markup, parse_mode=md); return
        except: pass
    msg = src.message if hasattr(src, 'message') else src
    await msg.reply_text(text, reply_markup=markup, parse_mode=md)

def gs(ctx):
    if "s" not in ctx.user_data:
        ctx.user_data["s"] = {"desc":"","date":str(date.today()),
            "tickets":[],"cur":{},"tipsters":[],"bookies":[],"investors":[],
            "inv_idx":0,"inv_stakes":{},"bot_msgs":[]}
    return ctx.user_data["s"]

def rs(ctx):
    ctx.user_data["s"] = {"desc":"","date":str(date.today()),
        "tickets":[],"cur":{},"tipsters":[],"bookies":[],"investors":[],
        "inv_idx":0,"inv_stakes":{},"bot_msgs":[]}

async def load(ctx):
    s = gs(ctx)
    tp = await sb_get("tipsters","order=name.asc")
    bk = await sb_get("bookies","order=name.asc")
    iv = await sb_get("investors","order=name.asc")
    s["tipsters"]  = [t["name"] for t in tp] if isinstance(tp,list) else []
    s["bookies"]   = [b["name"] for b in bk] if isinstance(bk,list) else []
    s["investors"] = iv if isinstance(iv,list) else []

async def track(ctx, msg):
    gs(ctx)["bot_msgs"].append(msg)

async def clear_msgs(ctx, chat_id, bot):
    for m in gs(ctx).get("bot_msgs",[]):
        try: await bot.delete_message(chat_id=chat_id, message_id=m.message_id)
        except: pass
    gs(ctx)["bot_msgs"] = []

def cc(tickets):
    ts = sum(t["stake"] for t in tickets)
    tp = sum(t["potencial"] for t in tickets)
    return round(tp/ts, 3) if ts > 0 else 0

def build_confirm(s):
    tix = s["tickets"]
    ts  = sum(t["stake"] for t in tix)
    tp  = sum(t["potencial"] for t in tix)
    cuota = cc(tix)
    lines = [f"*{s['desc']}*  _{s['date']}_"]
    for i,t in enumerate(tix):
        ss = f"{t['eur']}€={fmt(t['stake'])}" if t.get("eur") else fmt(t["stake"])
        gan = fmt(t["potencial"]-t["stake"])
        lines.append(f"#{i+1} {t['tipster']} · {t['bookie']}  {ss} @{t['cuota']} +{gan}")
    lines.append(f"──────────────")
    lines.append(f"@{cuota}  {fmt(ts)} → +{fmt(tp-ts)}")
    invs = {k:v for k,v in s["inv_stakes"].items() if v>0}
    if invs:
        for name,stake in invs.items():
            pot = round(stake*cuota,2)
            lines.append(f"💼 {name}: {fmt(stake)} → +{fmt(pot-stake)}")
    return "\n".join(lines)

def build_pendientes_line(g, tickets, inv_totals, inv_names):
    ts = sum(t["stake"] for t in tickets)
    tp = sum(t["potencial"] for t in tickets)
    cuota = round(tp/ts,3) if ts>0 else 0
    line = f"▸ *{g['descr'] or '(sin desc)'}*  {fmt(ts)} @{cuota} · pot {fmt(tp)}"
    if inv_totals:
        inv_parts = []
        for inv_id, stake in inv_totals.items():
            name = inv_names.get(inv_id,"?")
            pot_inv = round(stake*cuota,2)
            inv_parts.append(f"{name}: {fmt(stake)} → +{fmt(pot_inv-stake)}")
        line += f"\n  💼 {' · '.join(inv_parts)}"
    return line

async def delete_old_confirm(gid, bot):
    try:
        g = await sb_get("bet_groups", f"id=eq.{gid}&select=tg_chat_id,tg_msg_id")
        if isinstance(g,list) and g and g[0].get("tg_msg_id") and g[0].get("tg_chat_id"):
            await bot.delete_message(chat_id=g[0]["tg_chat_id"], message_id=g[0]["tg_msg_id"])
    except: pass

# ── BACK BUTTON helper ────────────────────────────────────────────────────────
BACK_BTN = [[InlineKeyboardButton("↩ Atrás", callback_data="go_back")]]

# ══════════════════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(u:Update, ctx):
    if not is_ok(u): return
    await u.message.reply_text("👋 *BetLog*\nUsa los botones del menú.",
        parse_mode="Markdown", reply_markup=MENU_KB)

# ══════════════════════════════════════════════════════════════════════════════
# Menu handler
# ══════════════════════════════════════════════════════════════════════════════
async def menu_handler(u:Update, ctx):
    txt = u.message.text
    if txt == "📝 Nueva apuesta":   return await cmd_nueva(u, ctx)
    if txt == "⏳ Pendientes":      await cmd_pendientes(u, ctx); return
    if txt == "📊 Hoy":             await cmd_hoy(u, ctx); return
    if txt == "🔧 Corregir":        return await cmd_corregir(u, ctx)
    if txt == "❌ Cancelar":        return await cmd_cancelar(u, ctx)

# ══════════════════════════════════════════════════════════════════════════════
# /nueva
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_nueva(u:Update, ctx):
    if not is_ok(u): return
    rs(ctx); await load(ctx)
    await try_delete(u.message)
    msg = await u.message.reply_text("✍️  *Nueva apuesta*\n\n¿Descripción?", parse_mode="Markdown")
    await track(ctx, msg)
    return S_DESC

async def r_desc(u:Update, ctx):
    s=gs(ctx); s["desc"]=u.message.text.strip(); s["date"]=str(date.today())
    await try_delete(u.message)
    return await ask_tipster(u, ctx)

async def ask_tipster(src, ctx):
    s=gs(ctx); s["cur"]={"num":len(s["tickets"])+1}
    tips=s["tipsters"]
    await clear_msgs(ctx, src.effective_chat.id, ctx.bot)
    if not tips:
        msg=await src.effective_message.reply_text("👤  *Tipster:*", parse_mode="Markdown")
        await track(ctx,msg); return S_TIPSTER
    rows=[[InlineKeyboardButton(t, callback_data=f"tip_{t}")] for t in tips]
    rows += BACK_BTN
    msg=await src.effective_message.reply_text("👤  *Tipster:*", reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track(ctx,msg); return S_TIPSTER

async def r_tip_cb(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="go_back":
        rs(ctx)
        await clear_msgs(ctx, u.effective_chat.id, ctx.bot)
        await q.edit_message_text("❌  Cancelado.")
        return ConversationHandler.END
    gs(ctx)["cur"]["tipster"]=q.data[4:]
    return await ask_bookie(u, ctx)

async def r_tip_txt(u:Update, ctx):
    await try_delete(u.message)
    gs(ctx)["cur"]["tipster"]=u.message.text.strip()
    return await ask_bookie(u, ctx)

async def ask_bookie(src, ctx):
    s=gs(ctx); bks=s["bookies"]
    await clear_msgs(ctx, src.effective_chat.id, ctx.bot)
    if not bks:
        msg=await src.effective_message.reply_text("🏠  *Bookie:*", parse_mode="Markdown")
        await track(ctx,msg); return S_BOOKIE
    rows=[]; row=[]
    for b in bks:
        row.append(InlineKeyboardButton(b, callback_data=f"bk_{b}"))
        if len(row)==2: rows.append(row); row=[]
    if row: rows.append(row)
    rows += BACK_BTN
    msg=await src.effective_message.reply_text("🏠  *Bookie:*", reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track(ctx,msg); return S_BOOKIE

async def r_bk_cb(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    if q.data=="go_back": return await ask_tipster(u, ctx)
    bk=q.data[3:]; s["cur"]["bookie"]=bk; s["cur"]["wnx"]="winamax" in bk.lower()
    return await ask_stake(u, ctx)

async def r_bk_txt(u:Update, ctx):
    await try_delete(u.message); s=gs(ctx)
    bk=u.message.text.strip(); s["cur"]["bookie"]=bk; s["cur"]["wnx"]="winamax" in bk.lower()
    return await ask_stake(u, ctx)

async def ask_stake(src, ctx):
    s=gs(ctx); n=s["cur"]["num"]
    await clear_msgs(ctx, src.effective_chat.id, ctx.bot)
    lbl = f"💶  *Stake #{n}* en euros (×4):" if s["cur"].get("wnx") else f"💰  *Stake #{n}*:"
    msg=await src.effective_message.reply_text(lbl,
        reply_markup=InlineKeyboardMarkup(BACK_BTN), parse_mode="Markdown")
    await track(ctx,msg); return S_STAKE

async def r_stake(u:Update, ctx):
    s=gs(ctx)
    try: raw=float(u.message.text.strip().replace(",",".")); assert raw>0
    except: await u.message.reply_text("⚠️  Número válido:"); return S_STAKE
    await try_delete(u.message)
    wnx=s["cur"].get("wnx")
    s["cur"]["raw"]=raw; s["cur"]["stake"]=round(raw*EUR_TO_SOLES,2) if wnx else raw
    if wnx: s["cur"]["eur"]=raw
    await clear_msgs(ctx, u.effective_chat.id, ctx.bot)
    extra = f"_{raw}€ = {fmt(s['cur']['stake'])}_\n\n" if wnx else ""
    msg=await u.message.reply_text(
        f"{extra}📊  `@1.90` → cuota  ·  `285` → retorno total",
        reply_markup=InlineKeyboardMarkup(BACK_BTN), parse_mode="Markdown")
    await track(ctx,msg); return S_CUOTA_RET

async def r_stake_back(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="go_back": return await ask_bookie(u, ctx)

async def r_cuota_ret(u:Update, ctx):
    s=gs(ctx); txt=u.message.text.strip()
    await try_delete(u.message)
    stake=s["cur"]["stake"]
    try:
        if txt.startswith("@"):
            c=float(txt[1:].replace(",",".")); assert c>1
            pot=round(stake*c,2)
            s["cur"]["cuota"]=c; s["cur"]["potencial"]=pot
        else:
            pot=float(txt.replace(",",".")); assert pot>stake
            c=round(pot/stake,3)
            s["cur"]["cuota"]=c; s["cur"]["potencial"]=pot
    except:
        msg=await u.message.reply_text("⚠️  `@1.90` cuota  ·  `285` retorno:", parse_mode="Markdown")
        await track(ctx,msg); return S_CUOTA_RET
    s["tickets"].append(dict(s["cur"]))
    n=len(s["tickets"])
    await clear_msgs(ctx, u.effective_chat.id, ctx.bot)
    gan=fmt(s["cur"]["potencial"]-stake)
    msg=await u.message.reply_text(
        f"Ticket #{n}  @{s['cur']['cuota']}  +{gan}\n\n¿Agregar otro?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕  Otro ticket", callback_data="more_yes"),
             InlineKeyboardButton("✅  Listo",        callback_data="more_no")],
            BACK_BTN[0]
        ]), parse_mode="Markdown")
    await track(ctx,msg); return S_MORE_TICKETS

async def r_more_cb(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    if q.data=="go_back":
        # Remove last ticket and go back to cuota
        if s["tickets"]: s["tickets"].pop()
        return await ask_stake(u, ctx)
    if q.data=="more_yes":
        s["cur"]={"num":len(s["tickets"])+1,"tipster":s["tickets"][0]["tipster"]}
        return await ask_bookie(u, ctx)
    return await ask_inv(u, ctx)

async def ask_inv(src, ctx):
    s=gs(ctx); s["inv_idx"]=0; s["inv_stakes"]={}
    return await ask_next_inv(src, ctx)

async def ask_next_inv(src, ctx):
    s=gs(ctx); invs=s["investors"]; idx=s["inv_idx"]
    if not invs or idx>=len(invs): return await show_confirm(src, ctx)
    inv=invs[idx]; wnx=any(t.get("wnx") for t in s["tickets"])
    ts=sum(t["stake"] for t in s["tickets"])
    await clear_msgs(ctx, src.effective_chat.id, ctx.bot)
    lbl = (f"💶  *{inv['name']}* en euros _(total {fmt(ts/EUR_TO_SOLES)}€)_\n_0 = no participa_"
           if wnx else
           f"💰  *{inv['name']}* _(total {fmt(ts)})_\n_0 = no participa_")
    msg=await src.effective_message.reply_text(lbl,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("0  No participa", callback_data=f"inv0_{inv['id']}"),
             InlineKeyboardButton("Todo",             callback_data=f"invf_{inv['id']}")],
            BACK_BTN[0]
        ]), parse_mode="Markdown")
    await track(ctx,msg); return S_INV_STAKE

async def r_inv_btn(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    if q.data=="go_back":
        if s["inv_idx"]>0: s["inv_idx"]-=1
        return await ask_next_inv(u, ctx)
    idx=s["inv_idx"]; inv=s["investors"][idx]
    if q.data.startswith("inv0_"): s["inv_stakes"][inv["name"]]=0
    elif q.data.startswith("invf_"): s["inv_stakes"][inv["name"]]=sum(t["stake"] for t in s["tickets"])
    s["inv_idx"]+=1; return await ask_next_inv(u, ctx)

async def r_inv_txt(u:Update, ctx):
    s=gs(ctx)
    try: raw=float(u.message.text.strip().replace(",",".")); assert raw>=0
    except: await u.message.reply_text("⚠️  Número válido:"); return S_INV_STAKE
    await try_delete(u.message)
    idx=s["inv_idx"]; inv=s["investors"][idx]
    wnx=any(t.get("wnx") for t in s["tickets"])
    soles=round(raw*EUR_TO_SOLES,2) if wnx and raw>0 else raw
    s["inv_stakes"][inv["name"]]=soles
    s["inv_idx"]+=1; return await ask_next_inv(u, ctx)

async def show_confirm(src, ctx):
    s=gs(ctx)
    await clear_msgs(ctx, src.effective_chat.id, ctx.bot)
    msg=await src.effective_message.reply_text(
        build_confirm(s),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅  Guardar",          callback_data="ok_yes"),
             InlineKeyboardButton("❌  Cancelar",         callback_data="ok_no")],
            [InlineKeyboardButton("⚡  Marcar resultado", callback_data="ok_res")],
            BACK_BTN[0]
        ]), parse_mode="Markdown")
    await track(ctx,msg); return S_CONFIRM

async def r_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    if q.data=="ok_no":
        await clear_msgs(ctx, u.effective_chat.id, ctx.bot)
        rs(ctx); await q.edit_message_text("❌  Cancelado."); return ConversationHandler.END
    if q.data=="go_back":
        return await ask_next_inv(u, ctx) if s["investors"] else await r_more_cb(u, ctx)
    wants_result = (q.data=="ok_res")
    try:
        gid=gen_id(); chat_id=u.effective_chat.id
        await sb_insert("bet_groups",{"id":gid,"date":s["date"],"descr":s["desc"],"status":"pending"})
        trows=[]
        for t in s["tickets"]:
            tid=gen_id(); t["_id"]=tid
            trows.append({"id":tid,"group_id":gid,"tipster":t["tipster"],"casa":t["bookie"],
                "stake":t["stake"],"cuota":t["cuota"],"potencial":t["potencial"],"status":"pending","returned":None})
        await sb_insert_many("tickets",trows)
        ts_total=sum(t["stake"] for t in s["tickets"])
        irows=[]
        for inv in s["investors"]:
            stake=s["inv_stakes"].get(inv["name"],0)
            if stake<=0: continue
            for t in s["tickets"]:
                prop=t["stake"]/ts_total if ts_total>0 else 1/len(s["tickets"])
                irows.append({"id":gen_id(),"ticket_id":t["_id"],"investor_id":inv["id"],"stake":round(stake*prop,2)})
        if irows: await sb_insert_many("ticket_investors",irows)
        await clear_msgs(ctx, chat_id, ctx.bot)
        conf_text = build_confirm(s)
        if wants_result:
            tickets_saved = await sb_get("tickets", f"group_id=eq.{gid}&order=id.asc")
            ctx.user_data["res_gid"] = gid
            ctx.user_data["res_tickets"] = tickets_saved if isinstance(tickets_saved,list) else []
            ctx.user_data["res_idx"] = 0
            ctx.user_data["res_returns"] = {}
            await q.edit_message_text(conf_text, parse_mode="Markdown")
            await sb_patch("bet_groups","id",gid,{"tg_chat_id":chat_id,"tg_msg_id":q.message.message_id})
            rs(ctx)
            return await ask_ticket_result(q, ctx)
        else:
            await q.edit_message_text(conf_text, parse_mode="Markdown")
            await sb_patch("bet_groups","id",gid,{"tg_chat_id":chat_id,"tg_msg_id":q.message.message_id})
            rs(ctx)
    except Exception as e:
        await q.edit_message_text(f"❌  Error: {e}")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# /pendientes
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_pendientes(u:Update, ctx):
    if not is_ok(u): return
    await try_delete(u.message)
    groups   = await sb_get("bet_groups","status=eq.pending&order=date.desc")
    tickets  = await sb_get("tickets","order=group_id.asc")
    inv_rows = await sb_get("ticket_investors","")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("✅  Sin pendientes."); return
    tmap={}
    for t in (tickets if isinstance(tickets,list) else []):
        tmap.setdefault(t["group_id"],[]).append(t)
    imap={}
    for ir in (inv_rows if isinstance(inv_rows,list) else []):
        imap.setdefault(ir["ticket_id"],[]).append(ir)
    investors = await sb_get("investors","order=name.asc")
    inv_names = {i["id"]:i["name"] for i in (investors if isinstance(investors,list) else [])}
    # Build single message with all apuestas as buttons
    lines = [f"⏳  *{len(groups)} pendiente(s)*\n"]
    rows = []
    for g in groups[:10]:
        ts=tmap.get(g["id"],[])
        inv_totals={}
        for t in ts:
            for ir in imap.get(t["id"],[]):
                inv_totals[ir["investor_id"]]=inv_totals.get(ir["investor_id"],0)+ir["stake"]
        lines.append(build_pendientes_line(g, ts, inv_totals, inv_names))
        rows.append([InlineKeyboardButton(f"▸ {g['descr'] or '(sin desc)'}",
            callback_data=f"psel_{g['id']}")])
    await u.message.reply_text("\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")

async def r_psel(u:Update, ctx):
    q=u.callback_query; await q.answer()
    gid=q.data[5:]
    grp=await sb_get("bet_groups",f"id=eq.{gid}&select=descr")
    desc=grp[0]["descr"] if isinstance(grp,list) and grp else gid
    await q.message.reply_text(
        f"*{desc}*",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Win",    callback_data=f"pq_win_{gid}"),
            InlineKeyboardButton("❌ Loss",   callback_data=f"pq_loss_{gid}"),
            InlineKeyboardButton("🔵 Void",   callback_data=f"pq_void_{gid}"),
            InlineKeyboardButton("✏️ Exacto", callback_data=f"pq_manual_{gid}"),
        ],[
            InlineKeyboardButton("🗑 Eliminar", callback_data=f"pq_del_{gid}"),
            InlineKeyboardButton("↩ Cancelar",  callback_data=f"pq_cancel_{gid}"),
        ]]),
        parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# /hoy
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_hoy(u:Update, ctx):
    if not is_ok(u): return
    await try_delete(u.message)
    today=str(date.today())
    groups=await sb_get("bet_groups",f"date=eq.{today}&order=created_at.desc")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text(f"📭  Sin apuestas hoy."); return
    tall=await sb_get("tickets","order=group_id.asc")
    tmap={}
    for t in (tall if isinstance(tall,list) else []): tmap.setdefault(t["group_id"],[]).append(t)
    lines=[f"📊  *{today}*\n"]; total=0; total_pnl=0
    for g in groups:
        ts=tmap.get(g["id"],[]); gs_s=sum(t["stake"] for t in ts); total+=gs_s
        icon="⏳" if g["status"]=="pending" else "✅"
        pnl_str=""
        if g["status"]=="settled":
            ret=sum((t.get("returned") or 0) for t in ts)
            p=round(ret-gs_s,2); total_pnl+=p
            pnl_str=f"  *{'+' if p>=0 else ''}{fmt(p)}*"
        cuota=round(sum(t["potencial"] for t in ts)/gs_s,3) if gs_s>0 else 0
        lines.append(f"{icon}  *{g['descr'] or '(sin desc)'}*  {fmt(gs_s)} @{cuota}{pnl_str}")
    lines.append(f"\n💰  {fmt(total)}")
    if total_pnl!=0: lines.append(f"📈  P&L  *{'+' if total_pnl>=0 else ''}{fmt(total_pnl)}*")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# /resultado
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_resultado(u:Update, ctx):
    if not is_ok(u): return
    await try_delete(u.message)
    groups=await sb_get("bet_groups","status=eq.pending&order=date.desc")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("✅  Sin pendientes."); return
    ctx.user_data["res_groups"]=groups
    rows=[[InlineKeyboardButton(f"▸  {g['descr'] or g['id'][:8]}  {fmt(sum(0 for _ in []))} ({g['date']})",callback_data=f"rg_{g['id']}")] for g in groups[:10]]
    rows.append([InlineKeyboardButton("🗑  Eliminar apuesta",callback_data="rg_delete_mode")])
    await u.message.reply_text("⚡  *¿Cuál liquidar?*",reply_markup=InlineKeyboardMarkup(rows),parse_mode="Markdown")
    return SR_PICK_GROUP

async def r_res_group(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="rg_delete_mode":
        groups=ctx.user_data.get("res_groups",[])
        rows=[[InlineKeyboardButton(f"🗑  {g['descr'] or g['id'][:8]}",callback_data=f"rd_{g['id']}")] for g in groups[:10]]
        rows.append([InlineKeyboardButton("↩  Cancelar",callback_data="rd_cancel")])
        await q.edit_message_text("🗑  *¿Cuál eliminar?*",reply_markup=InlineKeyboardMarkup(rows),parse_mode="Markdown")
        return SR_PICK_GROUP
    gid=q.data[3:]; ctx.user_data["res_gid"]=gid
    tickets=await sb_get("tickets",f"group_id=eq.{gid}&order=id.asc")
    if not isinstance(tickets,list) or not tickets:
        await q.edit_message_text("⚠️  Sin tickets."); return ConversationHandler.END
    ctx.user_data["res_tickets"]=tickets; ctx.user_data["res_idx"]=0; ctx.user_data["res_returns"]={}
    return await ask_ticket_result(q, ctx)

async def ask_ticket_result(src, ctx):
    tickets=ctx.user_data["res_tickets"]; idx=ctx.user_data["res_idx"]
    if idx>=len(tickets): return await finalize_resultado(src, ctx)
    t=tickets[idx]
    await edit_or_reply(src,
        f"🎯  *#{idx+1}/{len(tickets)}*  {t['tipster']} · {t['casa']}\n"
        f"{fmt(t['stake'])} @{t['cuota']} · pot {fmt(t['potencial'])}",[[
        InlineKeyboardButton("✅ Win",   callback_data="rr_win"),
        InlineKeyboardButton("❌ Loss",  callback_data="rr_loss"),
        InlineKeyboardButton("🔵 Void",  callback_data="rr_void"),
        InlineKeyboardButton("✏️ Exacto",callback_data="rr_manual"),
    ]]); return SR_PICK_TICKET

async def r_ticket_result(u:Update, ctx):
    q=u.callback_query; await q.answer()
    tickets=ctx.user_data["res_tickets"]; idx=ctx.user_data["res_idx"]
    t=tickets[idx]; stake=t["stake"]; pot=t["potencial"]
    a=q.data[3:]
    if a=="win":    ctx.user_data["res_returns"][t["id"]]=pot
    elif a=="loss": ctx.user_data["res_returns"][t["id"]]=0
    elif a=="void": ctx.user_data["res_returns"][t["id"]]=stake
    elif a=="manual":
        await q.edit_message_text(f"✏️  Ticket #{idx+1} — retorno exacto:"); return SR_SET_RETURN
    ctx.user_data["res_idx"]+=1; return await ask_ticket_result(q, ctx)

async def r_manual_return(u:Update, ctx):
    try: ret=float(u.message.text.strip().replace(",",".")); assert ret>=0
    except: await u.message.reply_text("⚠️  Número válido:"); return SR_SET_RETURN
    await try_delete(u.message)
    tickets=ctx.user_data["res_tickets"]; idx=ctx.user_data["res_idx"]
    ctx.user_data["res_returns"][tickets[idx]["id"]]=ret
    ctx.user_data["res_idx"]+=1; return await ask_ticket_result(u.message, ctx)

async def finalize_resultado(src, ctx):
    tickets=ctx.user_data["res_tickets"]; rets=ctx.user_data["res_returns"]
    tr=sum(rets.values()); ts=sum(t["stake"] for t in tickets); pnl=round(tr-ts,2)
    lines=[]
    for t in tickets:
        r=rets.get(t["id"],0); s=t["stake"]
        lbl="✅" if r>s else ("🔵" if abs(r-s)<0.01 else "❌")
        lines.append(f"{lbl}  {t['tipster']} · {t['casa']}  {fmt(r)}")
    lines.append(f"──────────────\nP&L  *{'+' if pnl>=0 else ''}{fmt(pnl)}*")
    await edit_or_reply(src,"\n".join(lines),[[
        InlineKeyboardButton("✅  Guardar",  callback_data="rf_save"),
        InlineKeyboardButton("❌  Cancelar", callback_data="rf_cancel")
    ]]); return SR_PICK_GROUP

async def r_res_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="rf_cancel": await q.edit_message_text("❌  Cancelado."); return ConversationHandler.END
    tickets=ctx.user_data["res_tickets"]; rets=ctx.user_data["res_returns"]; gid=ctx.user_data["res_gid"]
    try:
        grp=await sb_get("bet_groups",f"id=eq.{gid}&select=descr")
        grp_desc=(grp[0]["descr"] if isinstance(grp,list) and grp else "Apuesta")
        total_stake_g=sum(t["stake"] for t in tickets)
        total_ret_g=sum(rets.get(t["id"],0) for t in tickets)
        combined_c=total_ret_g/total_stake_g if total_stake_g>0 else 1
        for t in tickets:
            r=rets.get(t["id"],0)
            await sb_patch("tickets","id",t["id"],{"returned":r,"status":"settled"})
        inv_group={}
        for t in tickets:
            inv_rows=await sb_get("ticket_investors",f"ticket_id=eq.{t['id']}")
            if isinstance(inv_rows,list):
                for ir in inv_rows:
                    if ir["investor_id"] not in inv_group:
                        inv_group[ir["investor_id"]]={"stake":0,"ticket_id":t["id"]}
                    inv_group[ir["investor_id"]]["stake"]+=ir["stake"]
        for inv_id,data in inv_group.items():
            inv_stake=data["stake"]; inv_ret=round(inv_stake*combined_c,2)
            inv_pnl=round(inv_ret-inv_stake,2)
            if abs(inv_pnl)<0.01: continue
            await sb_insert("investor_movements",{"id":gen_id(),"investor_id":inv_id,
                "type":"bet_result","amount":inv_pnl,"note":grp_desc,
                "date":str(date.today()),"ticket_id":data["ticket_id"]})
        await sb_patch("bet_groups","id",gid,{"status":"settled"})
        tr=sum(rets.values()); ts=sum(t["stake"] for t in tickets); pnl=round(tr-ts,2)
        await delete_old_confirm(gid, ctx.bot)
        lines=[]
        for t in tickets:
            r=rets.get(t["id"],0); s=t["stake"]
            lbl="✅" if r>s else ("🔵" if abs(r-s)<0.01 else "❌")
            lines.append(f"{lbl}  {t['tipster']} · {t['casa']}  {fmt(r)}")
        lines.append(f"──────────────\n*{grp_desc}*  P&L  *{'+' if pnl>=0 else ''}{fmt(pnl)}*")
        new_msg=await ctx.bot.send_message(chat_id=q.message.chat_id,
            text="\n".join(lines), parse_mode="Markdown")
        await sb_patch("bet_groups","id",gid,{"tg_chat_id":q.message.chat_id,"tg_msg_id":new_msg.message_id})
        try: await q.message.delete()
        except: pass
    except Exception as e: await q.edit_message_text(f"❌  Error: {e}")
    return ConversationHandler.END

async def r_delete_group(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="rd_cancel": await q.edit_message_text("❌  Cancelado."); return ConversationHandler.END
    gid=q.data[3:]
    g=next((x for x in ctx.user_data.get("res_groups",[]) if x["id"]==gid),None)
    await q.edit_message_text(f"⚠️  ¿Eliminar *{g['descr'] if g else gid}*?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑  Sí",callback_data=f"rdc_{gid}"),
            InlineKeyboardButton("↩  No",callback_data="rd_cancel")
        ]]),parse_mode="Markdown"); return SR_PICK_GROUP

async def r_delete_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="rd_cancel": await q.edit_message_text("❌  Cancelado."); return ConversationHandler.END
    gid=q.data[4:]
    try:
        tickets=await sb_get("tickets",f"group_id=eq.{gid}")
        if isinstance(tickets,list):
            for t in tickets:
                mvs=await sb_get("investor_movements",f"ticket_id=eq.{t['id']}")
                if isinstance(mvs,list):
                    for mv in mvs: await sb_delete("investor_movements","id",mv["id"])
                await sb_delete("ticket_investors","ticket_id",t["id"])
                await sb_delete("tickets","id",t["id"])
        await sb_delete("bet_groups","id",gid)
        await q.edit_message_text("✅  Eliminada.")
    except Exception as e: await q.edit_message_text(f"❌  Error: {e}")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# /corregir
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_corregir(u:Update, ctx):
    if not is_ok(u): return
    await try_delete(u.message)
    groups=await sb_get("bet_groups","status=eq.settled&order=date.desc&limit=20")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("Sin liquidadas recientes."); return
    ctx.user_data["corr_groups"]=groups
    rows=[[InlineKeyboardButton(f"✎  {g['descr'] or g['id'][:8]}  ({g['date']})",callback_data=f"cg_{g['id']}")] for g in groups[:10]]
    await u.message.reply_text("✎  *¿Cuál corregir?*",reply_markup=InlineKeyboardMarkup(rows),parse_mode="Markdown")
    return SC_PICK_GROUP

async def r_corr_group(u:Update, ctx):
    q=u.callback_query; await q.answer()
    gid=q.data[3:]; ctx.user_data["corr_gid"]=gid
    tickets=await sb_get("tickets",f"group_id=eq.{gid}&order=id.asc")
    if not isinstance(tickets,list) or not tickets:
        await q.edit_message_text("⚠️  Sin tickets."); return ConversationHandler.END
    ctx.user_data["corr_tickets"]=tickets; ctx.user_data["corr_idx"]=0
    ctx.user_data["corr_returns"]={t["id"]:t.get("returned") or 0 for t in tickets}
    return await ask_corr_ticket(q, ctx)

async def ask_corr_ticket(src, ctx):
    tickets=ctx.user_data["corr_tickets"]; idx=ctx.user_data["corr_idx"]
    if idx>=len(tickets): return await finalize_correccion(src, ctx)
    t=tickets[idx]; cur=ctx.user_data["corr_returns"].get(t["id"],0)
    cur_lbl="Win" if cur>t["stake"] else ("Void" if abs(cur-t["stake"])<0.01 else "Loss")
    await edit_or_reply(src,
        f"✎  *#{idx+1}*  {t['tipster']} · {t['casa']}\n"
        f"{fmt(t['stake'])} @{t['cuota']}  actual: *{cur_lbl}* ({fmt(cur)})",[[
        InlineKeyboardButton("✅ Win",       callback_data="cr_win"),
        InlineKeyboardButton("❌ Loss",      callback_data="cr_loss"),
        InlineKeyboardButton("🔵 Void",      callback_data="cr_void"),
        InlineKeyboardButton("✏️ Exacto",    callback_data="cr_manual"),
        InlineKeyboardButton("↩ Sin cambio", callback_data="cr_skip"),
    ]]); return SC_PICK_TICKET

async def r_corr_ticket(u:Update, ctx):
    q=u.callback_query; await q.answer()
    tickets=ctx.user_data["corr_tickets"]; idx=ctx.user_data["corr_idx"]
    t=tickets[idx]; a=q.data[3:]
    if a=="win":    ctx.user_data["corr_returns"][t["id"]]=t["potencial"]
    elif a=="loss": ctx.user_data["corr_returns"][t["id"]]=0
    elif a=="void": ctx.user_data["corr_returns"][t["id"]]=t["stake"]
    elif a=="manual":
        await q.edit_message_text(f"✏️  Ticket #{idx+1} — retorno exacto:"); return SC_SET_RETURN
    ctx.user_data["corr_idx"]+=1; return await ask_corr_ticket(q, ctx)

async def r_corr_manual(u:Update, ctx):
    try: ret=float(u.message.text.strip().replace(",",".")); assert ret>=0
    except: await u.message.reply_text("⚠️  Número válido:"); return SC_SET_RETURN
    await try_delete(u.message)
    tickets=ctx.user_data["corr_tickets"]; idx=ctx.user_data["corr_idx"]
    ctx.user_data["corr_returns"][tickets[idx]["id"]]=ret
    ctx.user_data["corr_idx"]+=1; return await ask_corr_ticket(u.message, ctx)

async def finalize_correccion(src, ctx):
    tickets=ctx.user_data["corr_tickets"]; rets=ctx.user_data["corr_returns"]
    tr=sum(rets.values()); ts=sum(t["stake"] for t in tickets); pnl=round(tr-ts,2)
    lines=[]
    for t in tickets:
        r=rets.get(t["id"],0); s=t["stake"]
        lbl="✅" if r>s else ("🔵" if abs(r-s)<0.01 else "❌")
        lines.append(f"{lbl}  {t['tipster']} · {t['casa']}  {fmt(r)}")
    lines.append(f"──────────────\nP&L  *{'+' if pnl>=0 else ''}{fmt(pnl)}*")
    await edit_or_reply(src,"\n".join(lines),[[
        InlineKeyboardButton("✅  Guardar",  callback_data="cf_save"),
        InlineKeyboardButton("❌  Cancelar", callback_data="cf_cancel")
    ]]); return SC_PICK_GROUP

async def r_corr_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="cf_cancel": await q.edit_message_text("❌  Cancelado."); return ConversationHandler.END
    tickets=ctx.user_data["corr_tickets"]; rets=ctx.user_data["corr_returns"]; gid=ctx.user_data["corr_gid"]
    try:
        grp=await sb_get("bet_groups",f"id=eq.{gid}&select=descr")
        grp_desc=(grp[0]["descr"] if isinstance(grp,list) and grp else "Apuesta")
        total_stake_c=sum(t["stake"] for t in tickets)
        total_ret_c=sum(rets.get(t["id"],0) for t in tickets)
        combined_c=total_ret_c/total_stake_c if total_stake_c>0 else 1
        for t in tickets:
            r=rets.get(t["id"],0)
            await sb_patch("tickets","id",t["id"],{"returned":r,"status":"settled"})
            old_mvs=await sb_get("investor_movements",f"ticket_id=eq.{t['id']}&type=eq.bet_result")
            if isinstance(old_mvs,list):
                for mv in old_mvs: await sb_delete("investor_movements","id",mv["id"])
        inv_group_c={}
        for t in tickets:
            inv_rows=await sb_get("ticket_investors",f"ticket_id=eq.{t['id']}")
            if isinstance(inv_rows,list):
                for ir in inv_rows:
                    if ir["investor_id"] not in inv_group_c:
                        inv_group_c[ir["investor_id"]]={"stake":0,"ticket_id":t["id"]}
                    inv_group_c[ir["investor_id"]]["stake"]+=ir["stake"]
        for inv_id,data in inv_group_c.items():
            inv_stake=data["stake"]; inv_ret=round(inv_stake*combined_c,2)
            inv_pnl=round(inv_ret-inv_stake,2)
            if abs(inv_pnl)<0.01: continue
            await sb_insert("investor_movements",{"id":gen_id(),"investor_id":inv_id,
                "type":"bet_result","amount":inv_pnl,"note":f"Corrección: {grp_desc}",
                "date":str(date.today()),"ticket_id":data["ticket_id"]})
        tr=sum(rets.values()); ts=sum(t["stake"] for t in tickets); pnl=round(tr-ts,2)
        await delete_old_confirm(gid, ctx.bot)
        lines=[]
        for t in tickets:
            r=rets.get(t["id"],0); s=t["stake"]
            lbl="✅" if r>s else ("🔵" if abs(r-s)<0.01 else "❌")
            lines.append(f"{lbl}  {t['tipster']} · {t['casa']}  {fmt(r)}")
        lines.append(f"──────────────\n🔧 *{grp_desc}*  P&L  *{'+' if pnl>=0 else ''}{fmt(pnl)}*")
        new_msg=await ctx.bot.send_message(chat_id=q.message.chat_id,
            text="\n".join(lines), parse_mode="Markdown")
        await sb_patch("bet_groups","id",gid,{"tg_chat_id":q.message.chat_id,"tg_msg_id":new_msg.message_id})
        try: await q.message.delete()
        except: pass
    except Exception as e: await q.edit_message_text(f"❌  Error: {e}")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# /info
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_info(u:Update, ctx):
    if not is_ok(u): return
    await try_delete(u.message)
    groups = await sb_get("bet_groups","status=eq.pending&order=date.desc")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("✅  Sin apuestas pendientes."); return
    rows=[[InlineKeyboardButton(f"▸  {g['descr'] or g['id'][:8]}  ({g['date']})", callback_data=f"info_{g['id']}")] for g in groups[:10]]
    await u.message.reply_text("ℹ️  *¿Info de cuál?*", reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")

async def r_info_group(u:Update, ctx):
    q=u.callback_query; await q.answer()
    gid=q.data[5:]
    grp=await sb_get("bet_groups",f"id=eq.{gid}")
    tickets=await sb_get("tickets",f"group_id=eq.{gid}&order=id.asc")
    inv_rows_all=[]
    if isinstance(tickets,list):
        for t in tickets:
            tivs=await sb_get("ticket_investors",f"ticket_id=eq.{t['id']}")
            if isinstance(tivs,list): inv_rows_all+=tivs
    investors=await sb_get("investors","order=name.asc")
    inv_names={i["id"]:i["name"] for i in (investors if isinstance(investors,list) else [])}

    g=grp[0] if isinstance(grp,list) and grp else {}
    tix=tickets if isinstance(tickets,list) else []
    ts=sum(t["stake"] for t in tix)
    tp=sum(t["potencial"] for t in tix)
    cuota=round(tp/ts,3) if ts>0 else 0

    lines=[f"ℹ️  *{g.get('descr','—')}*  _{g.get('date','')}_",""]
    for i,t in enumerate(tix):
        ss=fmt(t["stake"]); c=t["cuota"]; pot=fmt(t["potencial"])
        lines.append(f"*#{i+1}* {t['tipster']} · {t['casa']}")
        lines.append(f"   {ss} @{c} · pot {pot}")
    lines.append("")
    lines.append(f"@{cuota}  {fmt(ts)} → pot {fmt(tp)}  +{fmt(tp-ts)}")

    inv_totals={}
    for ir in inv_rows_all:
        inv_totals[ir["investor_id"]]=inv_totals.get(ir["investor_id"],0)+ir["stake"]
    if inv_totals:
        lines.append("")
        for inv_id,stake in inv_totals.items():
            name=inv_names.get(inv_id,"?")
            pot_inv=round(stake*cuota,2)
            lines.append(f"💼 {name}: {fmt(stake)} → pot {fmt(pot_inv)}  +{fmt(pot_inv-stake)}")

    await q.edit_message_text("\n".join(lines), parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# QUICK RESULT from /pendientes
# ══════════════════════════════════════════════════════════════════════════════
async def r_pq_action(u:Update, ctx):
    q=u.callback_query; await q.answer()
    parts=q.data.split("_",2)  # pq_win_gid or pq_manual_gid or pq_cancel_gid
    action=parts[1]; gid=parts[2]
    tickets=await sb_get("tickets",f"group_id=eq.{gid}&order=id.asc")
    if not isinstance(tickets,list) or not tickets:
        await q.edit_message_text("⚠️  Sin tickets."); return
    ctx.user_data["res_gid"]=gid
    ctx.user_data["res_tickets"]=tickets
    ctx.user_data["res_returns"]={}
    ctx.user_data["res_idx"]=0
    if action in ("win","loss","void"):
        if len(tickets)==1:
            # Single ticket — liquidate immediately
            t=tickets[0]
            if action=="win":    ctx.user_data["res_returns"][t["id"]]=t["potencial"]
            elif action=="loss": ctx.user_data["res_returns"][t["id"]]=0
            elif action=="void": ctx.user_data["res_returns"][t["id"]]=t["stake"]
            return await save_quick_result(q, ctx)
        else:
            # Multiple tickets — apply same result to all then confirm
            for t in tickets:
                if action=="win":    ctx.user_data["res_returns"][t["id"]]=t["potencial"]
                elif action=="loss": ctx.user_data["res_returns"][t["id"]]=0
                elif action=="void": ctx.user_data["res_returns"][t["id"]]=t["stake"]
            return await finalize_resultado(q, ctx)
    elif action=="manual":
        return await ask_ticket_result(q, ctx)
    elif action=="cancel":
        try: await q.message.delete()
        except: pass
        return
    elif action=="del":
        grp=await sb_get("bet_groups",f"id=eq.{gid}&select=descr")
        desc=grp[0]["descr"] if isinstance(grp,list) and grp else gid
        await q.edit_message_text(f"⚠️  ¿Eliminar *{desc}*?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Sí", callback_data=f"pqdel_yes_{gid}"),
                InlineKeyboardButton("↩ No",  callback_data=f"pqdel_no_{gid}")
            ]]),parse_mode="Markdown")

async def r_pq_del_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer()
    parts=q.data.split("_",2); action=parts[1]; gid=parts[2]
    if action=="no":
        await q.edit_message_text("❌  Cancelado."); return
    try:
        tickets=await sb_get("tickets",f"group_id=eq.{gid}")
        if isinstance(tickets,list):
            for t in tickets:
                mvs=await sb_get("investor_movements",f"ticket_id=eq.{t['id']}")
                if isinstance(mvs,list):
                    for mv in mvs: await sb_delete("investor_movements","id",mv["id"])
                await sb_delete("ticket_investors","ticket_id",t["id"])
                await sb_delete("tickets","id",t["id"])
        await sb_delete("bet_groups","id",gid)
        await q.edit_message_text("✅  Eliminada.")
    except Exception as e: await q.edit_message_text(f"❌  Error: {e}")

async def save_quick_result(src, ctx):
    tickets=ctx.user_data["res_tickets"]
    rets=ctx.user_data["res_returns"]
    gid=ctx.user_data["res_gid"]
    try:
        grp=await sb_get("bet_groups",f"id=eq.{gid}&select=descr")
        grp_desc=(grp[0]["descr"] if isinstance(grp,list) and grp else "Apuesta")
        total_stake_g=sum(t["stake"] for t in tickets)
        total_ret_g=sum(rets.get(t["id"],0) for t in tickets)
        combined_c=total_ret_g/total_stake_g if total_stake_g>0 else 1
        for t in tickets:
            r=rets.get(t["id"],0)
            await sb_patch("tickets","id",t["id"],{"returned":r,"status":"settled"})
        inv_group={}
        for t in tickets:
            inv_rows=await sb_get("ticket_investors",f"ticket_id=eq.{t['id']}")
            if isinstance(inv_rows,list):
                for ir in inv_rows:
                    if ir["investor_id"] not in inv_group:
                        inv_group[ir["investor_id"]]={"stake":0,"ticket_id":t["id"]}
                    inv_group[ir["investor_id"]]["stake"]+=ir["stake"]
        for inv_id,data in inv_group.items():
            inv_stake=data["stake"]; inv_ret=round(inv_stake*combined_c,2)
            inv_pnl=round(inv_ret-inv_stake,2)
            if abs(inv_pnl)<0.01: continue
            await sb_insert("investor_movements",{"id":gen_id(),"investor_id":inv_id,
                "type":"bet_result","amount":inv_pnl,"note":grp_desc,
                "date":str(date.today()),"ticket_id":data["ticket_id"]})
        await sb_patch("bet_groups","id",gid,{"status":"settled"})
        tr=sum(rets.values()); ts=sum(t["stake"] for t in tickets); pnl=round(tr-ts,2)
        await delete_old_confirm(gid, ctx.bot)
        lines=[]
        for t in tickets:
            r=rets.get(t["id"],0); s=t["stake"]
            lbl="✅" if r>s else ("🔵" if abs(r-s)<0.01 else "❌")
            lines.append(f"{lbl}  {t['tipster']} · {t['casa']}  {fmt(r)}")
        lines.append(f"──────────────\n*{grp_desc}*  P&L  *{'+' if pnl>=0 else ''}{fmt(pnl)}*")
        chat_id = src.message.chat_id if hasattr(src,'message') else src.effective_chat.id
        new_msg=await ctx.bot.send_message(chat_id=chat_id,
            text="\n".join(lines), parse_mode="Markdown")
        await sb_patch("bet_groups","id",gid,{"tg_chat_id":chat_id,"tg_msg_id":new_msg.message_id})
        try:
            if hasattr(src,'edit_message_text'): await src.message.delete()
            elif hasattr(src,'delete'): await src.delete()
        except: pass
    except Exception as e:
        if hasattr(src,'edit_message_text'): await src.edit_message_text(f"❌  Error: {e}")

async def cmd_cancelar(u:Update, ctx):
    await clear_msgs(ctx, u.effective_chat.id, ctx.bot)
    rs(ctx); await u.message.reply_text("❌  Cancelado.", reply_markup=MENU_KB)
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    app=Application.builder().token(BOT_TOKEN).build()

    nueva_conv=ConversationHandler(
        entry_points=[
            CommandHandler("nueva",cmd_nueva),
            MessageHandler(filters.Regex("^📝 Nueva apuesta$"),cmd_nueva)
        ],
        states={
            S_DESC:        [MessageHandler(filters.TEXT&~filters.COMMAND,r_desc)],
            S_TIPSTER:     [CallbackQueryHandler(r_tip_cb,pattern="^(tip_|go_back)"),
                            MessageHandler(filters.TEXT&~filters.COMMAND,r_tip_txt)],
            S_BOOKIE:      [CallbackQueryHandler(r_bk_cb,pattern="^(bk_|go_back)"),
                            MessageHandler(filters.TEXT&~filters.COMMAND,r_bk_txt)],
            S_STAKE:       [CallbackQueryHandler(r_stake_back,pattern="^go_back"),
                            MessageHandler(filters.TEXT&~filters.COMMAND,r_stake)],
            S_CUOTA_RET:   [CallbackQueryHandler(r_stake_back,pattern="^go_back"),
                            MessageHandler(filters.TEXT&~filters.COMMAND,r_cuota_ret)],
            S_MORE_TICKETS:[CallbackQueryHandler(r_more_cb,pattern="^(more_|go_back)")],
            S_INV_STAKE:   [CallbackQueryHandler(r_inv_btn,pattern="^(inv(0|f)_|go_back)"),
                            MessageHandler(filters.TEXT&~filters.COMMAND,r_inv_txt)],
            S_CONFIRM:     [CallbackQueryHandler(r_confirm,pattern="^(ok_|go_back)")],
            SR_PICK_TICKET:[CallbackQueryHandler(r_ticket_result,pattern="^rr_")],
            SR_SET_RETURN: [MessageHandler(filters.TEXT&~filters.COMMAND,r_manual_return)],
            SR_PICK_GROUP: [CallbackQueryHandler(r_res_confirm,pattern="^rf_")],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        allow_reentry=True,
    )

    res_conv=ConversationHandler(
        entry_points=[
            CommandHandler("resultado",cmd_resultado),
            MessageHandler(filters.Regex("^✅ Resultado$"),cmd_resultado),
        ],
        states={
            SR_PICK_GROUP: [CallbackQueryHandler(r_res_group,   pattern="^rg_"),
                            CallbackQueryHandler(r_delete_group, pattern="^rd_"),
                            CallbackQueryHandler(r_delete_confirm,pattern="^rdc_"),
                            CallbackQueryHandler(r_res_confirm,  pattern="^rf_")],
            SR_PICK_TICKET:[CallbackQueryHandler(r_ticket_result,pattern="^rr_")],
            SR_SET_RETURN: [MessageHandler(filters.TEXT&~filters.COMMAND,r_manual_return)],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        allow_reentry=True,
    )

    corr_conv=ConversationHandler(
        entry_points=[
            CommandHandler("corregir",cmd_corregir),
            MessageHandler(filters.Regex("^🔧 Corregir$"),cmd_corregir),
        ],
        states={
            SC_PICK_GROUP: [CallbackQueryHandler(r_corr_group,  pattern="^cg_"),
                            CallbackQueryHandler(r_corr_confirm,pattern="^cf_")],
            SC_PICK_TICKET:[CallbackQueryHandler(r_corr_ticket, pattern="^cr_")],
            SC_SET_RETURN: [MessageHandler(filters.TEXT&~filters.COMMAND,r_corr_manual)],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        allow_reentry=True,
    )

    app.add_handler(nueva_conv)
    app.add_handler(res_conv)
    app.add_handler(corr_conv)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("cancelar",cmd_cancelar))
    app.add_handler(MessageHandler(
        filters.Regex("^(⏳ Pendientes|📊 Hoy|❌ Cancelar|🔧 Corregir)$"),menu_handler))
    app.add_handler(CommandHandler("pendientes",cmd_pendientes))
    app.add_handler(CommandHandler("hoy",cmd_hoy))
    app.add_handler(CommandHandler("info",cmd_info))
    app.add_handler(CallbackQueryHandler(r_info_group,    pattern="^info_"))
    app.add_handler(CallbackQueryHandler(r_psel,          pattern="^psel_"))
    app.add_handler(CallbackQueryHandler(r_pq_action,     pattern="^pq_"))
    app.add_handler(CallbackQueryHandler(r_pq_del_confirm,pattern="^pqdel_"))

    print("BetLog Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
