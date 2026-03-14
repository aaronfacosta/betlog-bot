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

# ── MAIN MENU KEYBOARD ───────────────────────────────────────────────────────
MENU_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📝 Nueva apuesta"), KeyboardButton("⏳ Pendientes")],
    [KeyboardButton("✅ Resultado"),      KeyboardButton("📊 Hoy")],
    [KeyboardButton("🔧 Corregir resultado")]
], resize_keyboard=True, persistent=True)

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
        try:
            await src.edit_message_text(text, reply_markup=markup, parse_mode=md)
            return
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

async def track_msg(ctx, msg):
    gs(ctx)["bot_msgs"].append(msg)

async def clear_bot_msgs(ctx, chat_id, bot):
    s = gs(ctx)
    for m in s.get("bot_msgs", []):
        try: await bot.delete_message(chat_id=chat_id, message_id=m.message_id)
        except: pass
    s["bot_msgs"] = []

def cc(tickets):
    ts = sum(t["stake"] for t in tickets)
    tp = sum(t["potencial"] for t in tickets)
    return round(tp/ts, 3) if ts > 0 else 0

def build_confirm(s):
    tix = s["tickets"]
    ts  = sum(t["stake"] for t in tix)
    tp  = sum(t["potencial"] for t in tix)
    cuota = cc(tix)
    lines = [
        f"╔══ 📋 *{s['desc']}*",
        f"║  📅 {s['date']}",
        f"╠══ TICKETS",
    ]
    for i,t in enumerate(tix):
        ss = f"{t['eur']}€ = {fmt(t['stake'])} S" if t.get("eur") else fmt(t["stake"])
        lines.append(f"║  #{i+1} {t['tipster']} · {t['bookie']}")
        lines.append(f"║     {ss} · @{t['cuota']} · pot {fmt(t['potencial'])}")
    lines += [
        f"╠══ RESUMEN",
        f"║  Cuota combinada  @{cuota}",
        f"║  Total apostado   {fmt(ts)} soles",
        f"║  Potencial        {fmt(tp)} soles",
        f"║  Ganancia neta    +{fmt(tp-ts)} soles",
    ]
    invs = {k:v for k,v in s["inv_stakes"].items() if v>0}
    if invs:
        lines.append(f"╠══ INVERSORES")
        for name,stake in invs.items():
            pot = round(stake*cuota,2); gan = round(pot-stake,2)
            lines.append(f"║  {name}: {fmt(stake)} S · pot {fmt(pot)} · +{fmt(gan)}")
    lines.append("╚══ ✅ *Apuesta registrada*")
    return "\n".join(lines)

def build_pendientes(groups, tmap, imap, inv_names):
    lines = [f"⏳  *PENDIENTES  ({len(groups)})*\n"]
    for g in groups[:10]:
        ts  = tmap.get(g["id"],[])
        total_s = sum(t["stake"] for t in ts)
        total_p = sum(t["potencial"] for t in ts)
        cuota   = round(total_p/total_s,3) if total_s>0 else 0
        lines.append(f"▸ *{g['descr'] or '(sin desc)'}*  _{g['date']}_")
        for t in ts:
            lines.append(f"  └ {t['tipster']} · {t['casa']}  @{t['cuota']}  {fmt(t['stake'])}S")
        inv_totals={}
        for t in ts:
            for ir in imap.get(t["id"],[]):
                n=inv_names.get(ir["investor_id"],"?")
                inv_totals[n]=inv_totals.get(n,0)+ir["stake"]
        if inv_totals:
            for name,stake in inv_totals.items():
                pot=round(stake*cuota,2); gan=round(pot-stake,2)
                lines.append(f"  💼 {name}  {fmt(stake)}S → pot {fmt(pot)}  +{fmt(gan)}")
        lines.append(f"  📊 @{cuota}  {fmt(total_s)}S  →  {fmt(total_p)}S\n")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(u:Update, ctx):
    if not is_ok(u): return
    await u.message.reply_text(
        "👋 *BetLog*\n\nUsa los botones del menú para navegar.",
        parse_mode="Markdown", reply_markup=MENU_KB)

# ══════════════════════════════════════════════════════════════════════════════
# Menu button handler
# ══════════════════════════════════════════════════════════════════════════════
async def menu_handler(u:Update, ctx):
    txt = u.message.text
    if txt == "📝 Nueva apuesta": return await cmd_nueva(u, ctx)
    if txt == "⏳ Pendientes":    await cmd_pendientes(u, ctx); return
    if txt == "✅ Resultado":     return await cmd_resultado(u, ctx)
    if txt == "📊 Hoy":           await cmd_hoy(u, ctx); return
    if txt == "🔧 Corregir resultado": return await cmd_corregir(u, ctx)

# ══════════════════════════════════════════════════════════════════════════════
# /nueva
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_nueva(u:Update, ctx):
    if not is_ok(u): return
    rs(ctx); await load(ctx)
    await try_delete(u.message)
    msg = await u.message.reply_text("✍️  *Nueva apuesta*\n\n¿Descripción?", parse_mode="Markdown")
    await track_msg(ctx, msg)
    return S_DESC

async def r_desc(u:Update, ctx):
    s=gs(ctx); s["desc"]=u.message.text.strip(); s["date"]=str(date.today())
    await try_delete(u.message)
    return await ask_tipster(u, ctx)

async def ask_tipster(src, ctx):
    s=gs(ctx); s["cur"]={"num":len(s["tickets"])+1}
    tips=s["tipsters"]
    if not tips:
        msg=await src.message.reply_text("👤  *Tipster:*", parse_mode="Markdown")
        await track_msg(ctx, msg); return S_TIPSTER
    rows=[[InlineKeyboardButton(t, callback_data=f"tip_{t}")] for t in tips]
    await clear_bot_msgs(ctx, src.message.chat_id, src.message.get_bot() if hasattr(src.message,'get_bot') else ctx.bot)
    msg=await src.message.reply_text("👤  *Tipster:*", reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track_msg(ctx, msg); return S_TIPSTER

async def r_tip_cb(u:Update, ctx):
    q=u.callback_query; await q.answer()
    gs(ctx)["cur"]["tipster"]=q.data[4:]
    return await ask_bookie(u, ctx)

async def r_tip_txt(u:Update, ctx):
    await try_delete(u.message)
    gs(ctx)["cur"]["tipster"]=u.message.text.strip()
    return await ask_bookie(u, ctx)

async def ask_bookie(src, ctx):
    s=gs(ctx); bks=s["bookies"]
    await clear_bot_msgs(ctx, src.effective_chat.id, ctx.bot)
    if not bks:
        msg=await src.effective_message.reply_text("🏠  *Bookie:*", parse_mode="Markdown")
        await track_msg(ctx, msg); return S_BOOKIE
    rows=[]; row=[]
    for b in bks:
        row.append(InlineKeyboardButton(b, callback_data=f"bk_{b}"))
        if len(row)==2: rows.append(row); row=[]
    if row: rows.append(row)
    msg=await src.effective_message.reply_text("🏠  *Bookie:*", reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track_msg(ctx, msg); return S_BOOKIE

async def r_bk_cb(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    bk=q.data[3:]; s["cur"]["bookie"]=bk; s["cur"]["wnx"]="winamax" in bk.lower()
    return await ask_stake(u, ctx)

async def r_bk_txt(u:Update, ctx):
    await try_delete(u.message); s=gs(ctx)
    bk=u.message.text.strip(); s["cur"]["bookie"]=bk; s["cur"]["wnx"]="winamax" in bk.lower()
    return await ask_stake(u, ctx)

async def ask_stake(src, ctx):
    s=gs(ctx); n=s["cur"]["num"]
    await clear_bot_msgs(ctx, src.effective_chat.id, ctx.bot)
    lbl = f"💶  *Stake #{n}* en euros (×4 soles):" if s["cur"].get("wnx") else f"💰  *Stake #{n}* en soles:"
    msg=await src.effective_message.reply_text(lbl, parse_mode="Markdown")
    await track_msg(ctx, msg); return S_STAKE

async def r_stake(u:Update, ctx):
    s=gs(ctx)
    try: raw=float(u.message.text.strip().replace(",",".")); assert raw>0
    except: await u.message.reply_text("⚠️  Número válido:"); return S_STAKE
    await try_delete(u.message)
    wnx=s["cur"].get("wnx")
    s["cur"]["raw"]=raw; s["cur"]["stake"]=round(raw*EUR_TO_SOLES,2) if wnx else raw
    if wnx: s["cur"]["eur"]=raw
    await clear_bot_msgs(ctx, u.effective_chat.id, ctx.bot)
    extra = f"_{raw}€ = {fmt(s['cur']['stake'])} soles_\n\n" if wnx else ""
    msg=await u.message.reply_text(
        f"{extra}📊  *Cuota o retorno:*\n\n`@1.90` → cuota\n`285` → retorno total",
        parse_mode="Markdown")
    await track_msg(ctx, msg); return S_CUOTA_RET

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
        msg=await u.message.reply_text("⚠️  Usa `@1.90` para cuota o `285` para retorno:", parse_mode="Markdown")
        await track_msg(ctx, msg); return S_CUOTA_RET
    s["tickets"].append(dict(s["cur"]))
    n=len(s["tickets"])
    await clear_bot_msgs(ctx, u.effective_chat.id, ctx.bot)
    msg=await u.message.reply_text(
        f"✅  Ticket #{n}:  @{s['cur']['cuota']}  ·  pot *{fmt(s['cur']['potencial'])}*  ·  +*{fmt(s['cur']['potencial']-stake)}*\n\n¿Otro ticket?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕  Otro ticket", callback_data="more_yes"),
            InlineKeyboardButton("✅  Listo",        callback_data="more_no")
        ]]), parse_mode="Markdown")
    await track_msg(ctx, msg); return S_MORE_TICKETS

async def r_more_cb(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
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
    await clear_bot_msgs(ctx, src.effective_chat.id, ctx.bot)
    lbl = (f"💶  *{inv['name']}* — inversión en euros\n_Total apuesta: {fmt(ts/EUR_TO_SOLES)}€_\n_(0 = no participa)_"
           if wnx else
           f"💰  *{inv['name']}* — inversión en soles\n_Total apuesta: {fmt(ts)} soles_\n_(0 = no participa)_")
    msg=await src.effective_message.reply_text(lbl,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("0  No participa", callback_data=f"inv0_{inv['id']}"),
            InlineKeyboardButton("Todo",             callback_data=f"invf_{inv['id']}")
        ]]), parse_mode="Markdown")
    await track_msg(ctx, msg); return S_INV_STAKE

async def r_inv_btn(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
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
    await clear_bot_msgs(ctx, src.effective_chat.id, ctx.bot)
    msg=await src.effective_message.reply_text(
        build_confirm(s)+"\n\n_¿Confirmar?_",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅  Guardar", callback_data="ok_yes"),
            InlineKeyboardButton("❌  Cancelar", callback_data="ok_no")
        ]]), parse_mode="Markdown")
    await track_msg(ctx, msg); return S_CONFIRM

async def r_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    if q.data=="ok_no":
        await clear_bot_msgs(ctx, u.effective_chat.id, ctx.bot)
        rs(ctx); await q.edit_message_text("❌  Cancelado."); return ConversationHandler.END
    try:
        gid=gen_id()
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
        # Clear intermediate messages, show only final confirmation
        await clear_bot_msgs(ctx, u.effective_chat.id, ctx.bot)
        await q.edit_message_text(build_confirm(s), parse_mode="Markdown")
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
    tickets  = await sb_get("tickets","status=eq.pending&order=group_id.asc")
    inv_rows = await sb_get("ticket_investors","")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("✅  Sin apuestas pendientes."); return
    tmap={}
    for t in (tickets if isinstance(tickets,list) else []):
        tmap.setdefault(t["group_id"],[]).append(t)
    imap={}
    for ir in (inv_rows if isinstance(inv_rows,list) else []):
        imap.setdefault(ir["ticket_id"],[]).append(ir)
    investors = await sb_get("investors","order=name.asc")
    inv_names = {i["id"]:i["name"] for i in (investors if isinstance(investors,list) else [])}
    await u.message.reply_text(build_pendientes(groups,tmap,imap,inv_names), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# /hoy
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_hoy(u:Update, ctx):
    if not is_ok(u): return
    await try_delete(u.message)
    today=str(date.today())
    groups=await sb_get("bet_groups",f"date=eq.{today}&order=created_at.desc")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text(f"📭  Sin apuestas hoy ({today})."); return
    tall=await sb_get("tickets","order=group_id.asc")
    tmap={}
    for t in (tall if isinstance(tall,list) else []): tmap.setdefault(t["group_id"],[]).append(t)
    lines=[f"📊  *HOY  {today}*\n"]
    total_s=0; total_pnl=0
    for g in groups:
        ts=tmap.get(g["id"],[]); gs_s=sum(t["stake"] for t in ts); total_s+=gs_s
        icon="⏳" if g["status"]=="pending" else "✅"
        pnl_str=""
        if g["status"]=="settled":
            ret=sum((t.get("returned") or 0) for t in ts)
            p=round(ret-gs_s,2); total_pnl+=p
            pnl_str=f"  *{'+' if p>=0 else ''}{fmt(p)}*"
        lines.append(f"{icon}  *{g['descr'] or '(sin desc)'}*{pnl_str}")
        for t in ts:
            lbl=""
            if t.get("returned") is not None:
                r=t["returned"]; st=t["stake"]
                lbl = "  ✅" if r>st else ("  🔵" if abs(r-st)<0.01 else "  ❌")
            lines.append(f"   └ {t['tipster']} · {t['casa']}  @{t['cuota']}{lbl}")
    lines.append(f"\n💰  Apostado:  *{fmt(total_s)} soles*")
    if total_pnl!=0: lines.append(f"📈  P&L:  *{'+' if total_pnl>=0 else ''}{fmt(total_pnl)} soles*")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# /resultado
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_resultado(u:Update, ctx):
    if not is_ok(u): return
    await try_delete(u.message)
    groups=await sb_get("bet_groups","status=eq.pending&order=date.desc")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("✅  Sin apuestas pendientes."); return
    ctx.user_data["res_groups"]=groups
    rows=[[InlineKeyboardButton(f"▸  {g['descr'] or g['id'][:8]}  ({g['date']})",callback_data=f"rg_{g['id']}")] for g in groups[:10]]
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
        f"🎯  *Ticket #{idx+1} / {len(tickets)}*\n"
        f"╔  {t['tipster']}  ·  {t['casa']}\n"
        f"║  Stake:  {fmt(t['stake'])}  ·  @{t['cuota']}\n"
        f"╚  Pot:  {fmt(t['potencial'])}\n\n"
        f"¿Resultado?",[[
        InlineKeyboardButton("✅ Win",  callback_data="rr_win"),
        InlineKeyboardButton("❌ Loss", callback_data="rr_loss"),
        InlineKeyboardButton("🔵 Void", callback_data="rr_void"),
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
        await q.edit_message_text(f"✏️  Ticket #{idx+1} — monto retornado exacto:"); return SR_SET_RETURN
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
    lines=["📊  *RESULTADO*\n"]
    for t in tickets:
        r=rets.get(t["id"],0); s=t["stake"]
        lbl="✅ Win" if r>s else ("🔵 Void" if abs(r-s)<0.01 else "❌ Loss")
        lines.append(f"{lbl}  {t['tipster']}  ·  {t['casa']}  →  {fmt(r)}S")
    lines.append(f"\nP&L  *{'+' if pnl>=0 else ''}{fmt(pnl)} soles*")
    await edit_or_reply(src,"\n".join(lines),[[
        InlineKeyboardButton("✅  Guardar",  callback_data="rf_save"),
        InlineKeyboardButton("❌  Cancelar", callback_data="rf_cancel")
    ]]); return SR_PICK_GROUP

async def r_res_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="rf_cancel": await q.edit_message_text("❌  Cancelado."); return ConversationHandler.END
    tickets=ctx.user_data["res_tickets"]; rets=ctx.user_data["res_returns"]; gid=ctx.user_data["res_gid"]
    try:
        for t in tickets:
            r=rets.get(t["id"],0); s=t["stake"]
            await sb_patch("tickets","id",t["id"],{"returned":r,"status":"settled"})
            inv_rows=await sb_get("ticket_investors",f"ticket_id=eq.{t['id']}")
            if isinstance(inv_rows,list):
                for ir in inv_rows:
                    prop=ir["stake"]/s if s>0 else 0
                    inv_pnl=round(r*prop-ir["stake"],2)
                    if inv_pnl!=0:
                        await sb_insert("investor_movements",{"id":gen_id(),
                            "investor_id":ir["investor_id"],"type":"bet_result",
                            "amount":inv_pnl,"note":"Resultado apuesta",
                            "date":str(date.today()),"ticket_id":t["id"]})
        await sb_patch("bet_groups","id",gid,{"status":"settled"})
        tr=sum(rets.values()); ts=sum(t["stake"] for t in tickets); pnl=round(tr-ts,2)
        await q.edit_message_text(
            f"✅  *Resultado guardado*\n\nP&L:  *{'+' if pnl>=0 else ''}{fmt(pnl)} soles*",
            parse_mode="Markdown")
    except Exception as e: await q.edit_message_text(f"❌  Error: {e}")
    return ConversationHandler.END

async def r_delete_group(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="rd_cancel": await q.edit_message_text("❌  Cancelado."); return ConversationHandler.END
    gid=q.data[3:]
    g=next((x for x in ctx.user_data.get("res_groups",[]) if x["id"]==gid),None)
    await q.edit_message_text(f"⚠️  ¿Eliminar *{g['descr'] if g else gid}*?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑  Sí",  callback_data=f"rdc_{gid}"),
            InlineKeyboardButton("↩  No",  callback_data="rd_cancel")
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
        await q.edit_message_text("✅  *Apuesta eliminada.*",parse_mode="Markdown")
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
        await u.message.reply_text("Sin apuestas liquidadas recientes."); return
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
        f"✎  *Ticket #{idx+1}*  —  {t['tipster']}  ·  {t['casa']}\n"
        f"Stake {fmt(t['stake'])}  @{t['cuota']}\n"
        f"Actual:  *{cur_lbl}*  ({fmt(cur)}S)\n\nNuevo resultado:",[[
        InlineKeyboardButton("✅ Win",  callback_data="cr_win"),
        InlineKeyboardButton("❌ Loss", callback_data="cr_loss"),
        InlineKeyboardButton("🔵 Void", callback_data="cr_void"),
        InlineKeyboardButton("✏️ Exacto",callback_data="cr_manual"),
        InlineKeyboardButton("↩ Sin cambio",callback_data="cr_skip"),
    ]]); return SC_PICK_TICKET

async def r_corr_ticket(u:Update, ctx):
    q=u.callback_query; await q.answer()
    tickets=ctx.user_data["corr_tickets"]; idx=ctx.user_data["corr_idx"]
    t=tickets[idx]; a=q.data[3:]
    if a=="win":    ctx.user_data["corr_returns"][t["id"]]=t["potencial"]
    elif a=="loss": ctx.user_data["corr_returns"][t["id"]]=0
    elif a=="void": ctx.user_data["corr_returns"][t["id"]]=t["stake"]
    elif a=="manual":
        await q.edit_message_text(f"✏️  Ticket #{idx+1} — monto exacto:"); return SC_SET_RETURN
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
    lines=["📊  *CORRECCIÓN*\n"]
    for t in tickets:
        r=rets.get(t["id"],0); s=t["stake"]
        lbl="✅ Win" if r>s else ("🔵 Void" if abs(r-s)<0.01 else "❌ Loss")
        lines.append(f"{lbl}  {t['tipster']}  ·  {t['casa']}  →  {fmt(r)}S")
    lines.append(f"\nNuevo P&L  *{'+' if pnl>=0 else ''}{fmt(pnl)} soles*")
    await edit_or_reply(src,"\n".join(lines),[[
        InlineKeyboardButton("✅  Guardar corrección", callback_data="cf_save"),
        InlineKeyboardButton("❌  Cancelar",           callback_data="cf_cancel")
    ]]); return SC_PICK_GROUP

async def r_corr_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="cf_cancel": await q.edit_message_text("❌  Cancelado."); return ConversationHandler.END
    tickets=ctx.user_data["corr_tickets"]; rets=ctx.user_data["corr_returns"]; gid=ctx.user_data["corr_gid"]
    try:
        for t in tickets:
            r=rets.get(t["id"],0); s=t["stake"]
            await sb_patch("tickets","id",t["id"],{"returned":r,"status":"settled"})
            old_mvs=await sb_get("investor_movements",f"ticket_id=eq.{t['id']}&type=eq.bet_result")
            if isinstance(old_mvs,list):
                for mv in old_mvs: await sb_delete("investor_movements","id",mv["id"])
            inv_rows=await sb_get("ticket_investors",f"ticket_id=eq.{t['id']}")
            if isinstance(inv_rows,list):
                for ir in inv_rows:
                    prop=ir["stake"]/s if s>0 else 0
                    inv_pnl=round(r*prop-ir["stake"],2)
                    if inv_pnl!=0:
                        await sb_insert("investor_movements",{"id":gen_id(),
                            "investor_id":ir["investor_id"],"type":"bet_result",
                            "amount":inv_pnl,"note":"Corrección resultado",
                            "date":str(date.today()),"ticket_id":t["id"]})
        tr=sum(rets.values()); ts=sum(t["stake"] for t in tickets); pnl=round(tr-ts,2)
        await q.edit_message_text(
            f"✅  *Corrección guardada*\n\nNuevo P&L:  *{'+' if pnl>=0 else ''}{fmt(pnl)} soles*",
            parse_mode="Markdown")
    except Exception as e: await q.edit_message_text(f"❌  Error: {e}")
    return ConversationHandler.END

async def cmd_cancelar(u:Update, ctx):
    await clear_bot_msgs(ctx, u.effective_chat.id, ctx.bot)
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
            S_TIPSTER:     [CallbackQueryHandler(r_tip_cb,pattern="^tip_"),
                            MessageHandler(filters.TEXT&~filters.COMMAND,r_tip_txt)],
            S_BOOKIE:      [CallbackQueryHandler(r_bk_cb,pattern="^bk_"),
                            MessageHandler(filters.TEXT&~filters.COMMAND,r_bk_txt)],
            S_STAKE:       [MessageHandler(filters.TEXT&~filters.COMMAND,r_stake)],
            S_CUOTA_RET:   [MessageHandler(filters.TEXT&~filters.COMMAND,r_cuota_ret)],
            S_MORE_TICKETS:[CallbackQueryHandler(r_more_cb,pattern="^more_")],
            S_INV_STAKE:   [CallbackQueryHandler(r_inv_btn,pattern="^inv(0|f)_"),
                            MessageHandler(filters.TEXT&~filters.COMMAND,r_inv_txt)],
            S_CONFIRM:     [CallbackQueryHandler(r_confirm,pattern="^ok_")],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        allow_reentry=True,
    )

    res_conv=ConversationHandler(
        entry_points=[
            CommandHandler("resultado",cmd_resultado),
            MessageHandler(filters.Regex("^✅ Resultado$"),cmd_resultado)
        ],
        states={
            SR_PICK_GROUP: [CallbackQueryHandler(r_res_group,  pattern="^rg_"),
                            CallbackQueryHandler(r_delete_group,pattern="^rd_"),
                            CallbackQueryHandler(r_delete_confirm,pattern="^rdc_"),
                            CallbackQueryHandler(r_res_confirm, pattern="^rf_")],
            SR_PICK_TICKET:[CallbackQueryHandler(r_ticket_result,pattern="^rr_")],
            SR_SET_RETURN: [MessageHandler(filters.TEXT&~filters.COMMAND,r_manual_return)],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        allow_reentry=True,
    )

    corr_conv=ConversationHandler(
        entry_points=[
            CommandHandler("corregir",cmd_corregir),
            MessageHandler(filters.Regex("^🔧 Corregir resultado$"),cmd_corregir)
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
    app.add_handler(MessageHandler(filters.Regex("^(⏳ Pendientes|📊 Hoy)$"),menu_handler))
    app.add_handler(CommandHandler("pendientes",cmd_pendientes))
    app.add_handler(CommandHandler("hoy",cmd_hoy))

    print("BetLog Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
