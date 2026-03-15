import os, httpx, uuid, re
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters)

BOT_TOKEN   = os.environ.get("BOT_TOKEN","")
SUPA_URL    = os.environ.get("SUPA_URL","")
SUPA_KEY    = os.environ.get("SUPA_KEY","")
ALLOWED_IDS = [int(x) for x in os.environ.get("ALLOWED_USER_IDS","").split(",") if x.strip()]
EUR         = 4
TIMEOUT     = 900  # 15 min

# ── States ────────────────────────────────────────────────────────────────────
(S_DESC, S_TIPSTER, S_N_BOOKIES, S_BOOKIE, S_TICKETS,
 S_INV, S_CONFIRM) = range(7)
(P_LIST, P_DETAIL, P_RESULT, P_EXACT, P_CONFIRM_RES, P_DEL) = range(10, 16)

MENU_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📝 Nueva apuesta"), KeyboardButton("⏳ Pendientes")],
    [KeyboardButton("📊 Hoy"),           KeyboardButton("❌ Cancelar")]
], resize_keyboard=True)

# ── Supabase helpers ──────────────────────────────────────────────────────────
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
    h=H(); h["Prefer"]="return=minimal"
    async with httpx.AsyncClient() as c:
        return (await c.patch(f"{SUPA_URL}/rest/v1/{table}?{col}=eq.{val}", headers=h, json=data)).status_code

async def sb_delete(table, col, val):
    async with httpx.AsyncClient() as c:
        return (await c.delete(f"{SUPA_URL}/rest/v1/{table}?{col}=eq.{val}", headers=H())).status_code

def gid(): return str(uuid.uuid4())[:12].replace("-","")
def fmt(n):
    n = round(float(n), 2)
    return f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"
def is_ok(u): return not ALLOWED_IDS or u.effective_user.id in ALLOWED_IDS

# ── Message helpers ───────────────────────────────────────────────────────────
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

async def edit_or_new(src, text, kbd=None, md="Markdown"):
    markup = InlineKeyboardMarkup(kbd) if kbd else None
    if hasattr(src,"edit_message_text"):
        try: await src.edit_message_text(text, reply_markup=markup, parse_mode=md); return
        except: pass
    m = src.message if hasattr(src,"message") else src
    await m.reply_text(text, reply_markup=markup, parse_mode=md)

async def delete_old_confirm(gid_val, bot):
    try:
        g = await sb_get("bet_groups", f"id=eq.{gid_val}&select=tg_chat_id,tg_msg_id")
        if isinstance(g,list) and g and g[0].get("tg_msg_id"):
            await bot.delete_message(chat_id=g[0]["tg_chat_id"], message_id=g[0]["tg_msg_id"])
    except: pass

BACK = [[InlineKeyboardButton("↩ Atrás", callback_data="back")]]

# ── State helpers ─────────────────────────────────────────────────────────────
def gs(ctx):
    ctx.user_data.setdefault("s",{
        "desc":"","date":str(date.today()),
        "tipsters":[],"bookies":[],"investors":[],
        "n_bookies":1,"bookie_idx":0,"cur_bookie":"","wnx":False,
        "tickets":[],"inv_stakes":{},"_msgs":[]
    })
    return ctx.user_data["s"]

def rs(ctx):
    ctx.user_data["s"] = {
        "desc":"","date":str(date.today()),
        "tipsters":[],"bookies":[],"investors":[],
        "n_bookies":1,"bookie_idx":0,"cur_bookie":"","wnx":False,
        "tickets":[],"inv_stakes":{}
    }
    ctx.user_data["_msgs"] = []

async def load_db(ctx):
    s = gs(ctx)
    tp = await sb_get("tipsters","order=name.asc")
    bk = await sb_get("bookies","order=name.asc")
    iv = await sb_get("investors","order=name.asc")
    s["tipsters"]  = [t["name"] for t in tp] if isinstance(tp,list) else []
    s["bookies"]   = [b["name"] for b in bk] if isinstance(bk,list) else []
    s["investors"] = iv if isinstance(iv,list) else []

# ── Ticket parser ─────────────────────────────────────────────────────────────
def parse_tickets(text, stake_mult=1):
    """Parse multiline ticket message. Returns (tickets, errors)"""
    tickets, errors = [], []
    for i, line in enumerate(text.strip().splitlines(), 1):
        line = line.strip()
        if not line: continue
        # format: "500 @1.90" or "500 1.90" (no @) or "500 285" (retorno)
        m = re.match(r'^([\d.,]+)\s+@?([\d.,]+)$', line)
        if not m:
            errors.append(f"Línea {i}: `{line}` — formato incorrecto")
            continue
        try:
            stake = float(m.group(1).replace(",",".")) * stake_mult
            val   = float(m.group(2).replace(",","."))
        except:
            errors.append(f"Línea {i}: número inválido"); continue
        if stake <= 0:
            errors.append(f"Línea {i}: stake debe ser > 0"); continue
        # Detect cuota vs retorno: if original had @, it's cuota; else if val > stake it's retorno, else cuota
        orig = line
        if '@' in orig:
            if val <= 1: errors.append(f"Línea {i}: cuota debe ser > 1"); continue
            cuota = round(val, 3); pot = round(stake * cuota, 2)
        else:
            if val > stake:  # retorno
                pot = round(val * stake_mult if stake_mult > 1 else val, 2)
                cuota = round(pot / stake, 3)
            else:  # treat as cuota
                if val <= 1: errors.append(f"Línea {i}: cuota debe ser > 1"); continue
                cuota = round(val, 3); pot = round(stake * cuota, 2)
        tickets.append({"stake": stake, "cuota": cuota, "potencial": pot})
    return tickets, errors

# ── Build messages ────────────────────────────────────────────────────────────
def cc(tickets):
    ts = sum(t["stake"] for t in tickets)
    tp = sum(t["potencial"] for t in tickets)
    return round(tp/ts, 3) if ts > 0 else 0

def build_confirm(s):
    tix = s["tickets"]; ts = sum(t["stake"] for t in tix)
    tp  = sum(t["potencial"] for t in tix); cuota = cc(tix)
    lines = [f"*{s['desc'].upper()}*", f"_{s['date']}_", ""]
    for i,t in enumerate(tix,1):
        bk=t.get("bookie",""); tip=t.get("tipster","")
        eur=f" ({t['eur']}€)" if t.get("eur") else ""
        gan=fmt(t["potencial"]-t["stake"])
        lines.append(f"#{i}  {tip} · {bk}")
        lines.append(f"     {fmt(t['stake'])}{eur}  @{t['cuota']}  +{gan}")
    lines.append("")
    lines.append(f"──────────")
    lines.append(f"@{cuota}   {fmt(ts)} apostado   +{fmt(tp-ts)} ganancia")
    invs = {k:v for k,v in s["inv_stakes"].items() if v>0}
    if invs:
        lines.append("")
        for name,stake in invs.items():
            pot=round(stake*cuota,2)
            lines.append(f"💼 {name}   {fmt(stake)} → +{fmt(pot-stake)}")
    return "\n".join(lines)

def build_result_msg(desc, tickets, rets):
    ts = sum(t["stake"] for t in tickets)
    tr = sum(rets.values()); pnl = round(tr-ts,2)
    sign = "+" if pnl>=0 else ""
    lines = [f"*{desc.upper()}*", ""]
    for t in tickets:
        r=rets.get(t["id"],0); s=t["stake"]
        lbl="✅" if r>s else ("🔵" if abs(r-s)<0.01 else "❌")
        lines.append(f"{lbl}  {t.get('tipster','')} · {t.get('casa','')}   ret {fmt(r)}")
    lines.append("")
    lines.append(f"──────────")
    lines.append(f"P&L   *{sign}{fmt(pnl)}*")
    return "\n".join(lines)

def pending_line(g, tickets, inv_totals, inv_names, cuota):
    ts = sum(t["stake"] for t in tickets)
    tp = sum(t["potencial"] for t in tickets)
    line = f"▸ *{g['descr'] or '(sin desc)'}*   {fmt(ts)} @{cuota}  →  {fmt(tp)}"
    if inv_totals:
        parts = [f"{inv_names.get(iid,'?')} {fmt(s)}" for iid,s in inv_totals.items()]
        line += f"\n   💼 {' · '.join(parts)}"
    return line

# ══════════════════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(u:Update, ctx):
    if not is_ok(u): return
    await u.message.reply_text("*BETLOG*\nUsa los botones del menú.",
        parse_mode="Markdown", reply_markup=MENU_KB)

# ══════════════════════════════════════════════════════════════════════════════
# /nueva
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_nueva(u:Update, ctx):
    if not is_ok(u): return
    rs(ctx); await load_db(ctx)
    await try_del(u.message)
    msg = await u.message.reply_text("✍️ *NUEVA APUESTA*\n\nDescripción del evento:", parse_mode="Markdown")
    await track(ctx, msg); return S_DESC

async def r_desc(u:Update, ctx):
    s=gs(ctx); s["desc"]=u.message.text.strip(); s["date"]=str(date.today())
    await try_del(u.message)
    return await ask_tipster(u, ctx)

async def ask_tipster(src, ctx):
    s=gs(ctx)
    await clear(ctx, src.effective_chat.id, ctx.bot)
    rows = [[InlineKeyboardButton(t, callback_data=f"tip_{t}")] for t in s["tipsters"]]
    rows += BACK
    msg = await src.effective_message.reply_text("👤 Tipster:",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track(ctx, msg); return S_TIPSTER

async def r_tipster(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="back": return await cmd_nueva(u, ctx)
    gs(ctx)["tipster"]=q.data[4:]
    return await ask_n_bookies(u, ctx)

async def r_tipster_txt(u:Update, ctx):
    await try_del(u.message); gs(ctx)["tipster"]=u.message.text.strip()
    return await ask_n_bookies(u, ctx)

async def ask_n_bookies(src, ctx):
    await clear(ctx, src.effective_chat.id, ctx.bot)
    rows = [[InlineKeyboardButton(str(n), callback_data=f"nb_{n}") for n in range(1,6)]] + BACK
    msg = await src.effective_message.reply_text("🏠 ¿Cuántas bookies?",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track(ctx, msg); return S_N_BOOKIES

async def r_n_bookies(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="back": return await ask_tipster(u, ctx)
    gs(ctx)["n_bookies"]=int(q.data[3:]); gs(ctx)["bookie_idx"]=0
    return await ask_bookie(u, ctx)

async def r_n_bookies_txt(u:Update, ctx):
    try: n=int(u.message.text.strip()); assert 1<=n<=10
    except: await u.message.reply_text("⚠️  Entre 1 y 10:"); return S_N_BOOKIES
    await try_del(u.message)
    gs(ctx)["n_bookies"]=n; gs(ctx)["bookie_idx"]=0
    return await ask_bookie(u, ctx)

async def ask_bookie(src, ctx):
    s=gs(ctx); idx=s["bookie_idx"]; n=s["n_bookies"]
    await clear(ctx, src.effective_chat.id, ctx.bot)
    rows=[]; row=[]
    for b in s["bookies"]:
        row.append(InlineKeyboardButton(b, callback_data=f"bk_{b}"))
        if len(row)==2: rows.append(row); row=[]
    if row: rows.append(row)
    rows += BACK
    lbl = f"Bookie {idx+1}/{n}" if n>1 else "Bookie"
    msg = await src.effective_message.reply_text(f"🏠 {lbl}",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track(ctx, msg); return S_BOOKIE

async def r_bookie(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="back":
        if gs(ctx)["bookie_idx"]==0: return await ask_n_bookies(u, ctx)
        gs(ctx)["bookie_idx"]-=1
        gs(ctx)["tickets"]=[t for t in gs(ctx)["tickets"] if t.get("bookie")!=gs(ctx).get("_last_bookie","")]
        return await ask_bookie(u, ctx)
    s=gs(ctx); bk=q.data[3:]
    s["cur_bookie"]=bk; s["wnx"]="winamax" in bk.lower()
    return await ask_tickets(u, ctx)

async def r_bookie_txt(u:Update, ctx):
    await try_del(u.message); s=gs(ctx)
    bk=u.message.text.strip(); s["cur_bookie"]=bk; s["wnx"]="winamax" in bk.lower()
    return await ask_tickets(u, ctx)

async def ask_tickets(src, ctx):
    s=gs(ctx)
    await clear(ctx, src.effective_chat.id, ctx.bot)
    wnx_note = f"\n_Winamax: ingresa en euros (×{EUR} = soles)_" if s["wnx"] else ""
    msg = await src.effective_message.reply_text(
        f"📋 *{s['cur_bookie']}*{wnx_note}\n"
        f"`500 @1.90` cuota  ·  `500 285` retorno",
        reply_markup=InlineKeyboardMarkup(BACK), parse_mode="Markdown")
    await track(ctx, msg); return S_TICKETS

async def r_tickets(u:Update, ctx):
    s=gs(ctx); wnx=s["wnx"]
    await try_del(u.message)
    raw_tickets, errors = parse_tickets(u.message.text, stake_mult=EUR if wnx else 1)
    if errors:
        err_msg = "⚠️ Corrige y reenvía:\n" + "\n".join(errors)
        msg = await u.message.reply_text(err_msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(BACK))
        await track(ctx, msg); return S_TICKETS
    # Add bookie/tipster/eur to each ticket
    for t in raw_tickets:
        t["bookie"]=s["cur_bookie"]; t["tipster"]=s.get("tipster","")
        if wnx: t["eur"]=round(t["stake"]/EUR, 2)
        s["tickets"].append(t)
    s["_last_bookie"]=s["cur_bookie"]
    s["bookie_idx"]+=1
    if s["bookie_idx"] < s["n_bookies"]:
        return await ask_bookie(u, ctx)
    return await ask_inv(u, ctx)

async def ask_inv(src, ctx):
    s=gs(ctx)
    await clear(ctx, src.effective_chat.id, ctx.bot)
    ts = sum(t["stake"] for t in s["tickets"])
    invs = s["investors"]
    names = [i["name"] for i in invs]
    rows = []
    if len(names)>=1: rows.append([InlineKeyboardButton(f"Solo {names[0]}", callback_data=f"inv_solo0")])
    if len(names)>=2:
        rows.append([InlineKeyboardButton(f"Solo {names[1]}", callback_data=f"inv_solo1")])
        rows.append([InlineKeyboardButton("50/50", callback_data="inv_5050"),
                     InlineKeyboardButton("Sin inversores", callback_data="inv_none")])
    else:
        rows.append([InlineKeyboardButton("Sin inversores", callback_data="inv_none")])
    rows += BACK
    wnx = any(t.get("wnx") for t in s["tickets"])
    note = f"Total: {fmt(ts)} soles" + (f" = {fmt(ts/EUR)}€" if wnx else "")
    msg = await src.effective_message.reply_text(
        f"💼 *INVERSORES*   _{note}_\nO escribe: `Alonso 200 RV 50`",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track(ctx, msg); return S_INV

async def r_inv_btn(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    if q.data=="back": return await ask_tickets(u, ctx)
    ts = sum(t["stake"] for t in s["tickets"])
    invs = s["investors"]; names=[i["name"] for i in invs]
    if q.data=="inv_none":
        s["inv_stakes"]={}
    elif q.data=="inv_solo0" and names:
        s["inv_stakes"]={names[0]:ts}
    elif q.data=="inv_solo1" and len(names)>1:
        s["inv_stakes"]={names[1]:ts}
    elif q.data=="inv_5050" and len(names)>=2:
        half=round(ts/2,2); s["inv_stakes"]={names[0]:half,names[1]:half}
    return await show_confirm(u, ctx)

async def r_inv_txt(u:Update, ctx):
    s=gs(ctx); txt=u.message.text.strip()
    await try_del(u.message)
    invs=s["investors"]; inv_stakes={}
    # Parse "Alonso 200 RV 50" or "RV 50"
    parts=txt.split()
    i=0
    while i<len(parts)-1:
        name_part=parts[i].lower()
        try: amount=float(parts[i+1].replace(",",".")); assert amount>=0
        except: i+=1; continue
        matched=None
        for inv in invs:
            if inv["name"].lower().startswith(name_part) or name_part in inv["name"].lower():
                matched=inv["name"]; break
        if matched: inv_stakes[matched]=amount
        i+=2
    if not inv_stakes:
        msg=await u.message.reply_text("⚠️  No entendí\nEj: `Alonso 200 RV 50`",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(BACK))
        await track(ctx,msg); return S_INV
    s["inv_stakes"]=inv_stakes
    return await show_confirm(u, ctx)

async def show_confirm(src, ctx):
    s=gs(ctx)
    await clear(ctx, src.effective_chat.id, ctx.bot)
    msg=await src.effective_message.reply_text(
        build_confirm(s),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Guardar", callback_data="ok_yes"),
             InlineKeyboardButton("❌ Cancelar", callback_data="ok_no")],
            BACK[0]
        ]), parse_mode="Markdown")
    await track(ctx, msg); return S_CONFIRM

async def r_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    if q.data=="back": return await ask_inv(u, ctx)
    if q.data=="ok_no":
        await clear(ctx, u.effective_chat.id, ctx.bot)
        rs(ctx); await q.edit_message_text("Cancelado."); return ConversationHandler.END
    try:
        group_id=gid(); chat_id=u.effective_chat.id
        await sb_post("bet_groups",{"id":group_id,"date":s["date"],"descr":s["desc"],"status":"pending"})
        trows=[]
        for t in s["tickets"]:
            tid=gid(); t["_id"]=tid
            trows.append({"id":tid,"group_id":group_id,"tipster":t["tipster"],"casa":t["bookie"],
                "stake":t["stake"],"cuota":t["cuota"],"potencial":t["potencial"],"status":"pending","returned":None})
        await sb_post("tickets",trows)
        ts_total=sum(t["stake"] for t in s["tickets"])
        irows=[]
        for inv in s["investors"]:
            stake=s["inv_stakes"].get(inv["name"],0)
            if stake<=0: continue
            for t in s["tickets"]:
                prop=t["stake"]/ts_total if ts_total>0 else 1/len(s["tickets"])
                irows.append({"id":gid(),"ticket_id":t["_id"],"investor_id":inv["id"],"stake":round(stake*prop,2)})
        if irows: await sb_post("ticket_investors",irows)
        await clear(ctx, chat_id, ctx.bot)
        conf_text=build_confirm(s)
        await q.edit_message_text(conf_text, parse_mode="Markdown")
        await sb_patch("bet_groups","id",group_id,{"tg_chat_id":chat_id,"tg_msg_id":q.message.message_id})
        rs(ctx)
    except Exception as e:
        await q.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# /pendientes
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_pendientes(u:Update, ctx):
    if not is_ok(u): return
    await try_del(u.message)
    return await show_pending_list(u.message, ctx)

async def show_pending_list(src, ctx):
    groups  = await sb_get("bet_groups","status=eq.pending&order=date.desc")
    tickets = await sb_get("tickets","order=group_id.asc")
    inv_rows= await sb_get("ticket_investors","")
    investors=await sb_get("investors","order=name.asc")
    if not isinstance(groups,list) or not groups:
        await src.reply_text("Sin pendientes ✅"); return P_LIST
    tmap={}
    for t in (tickets if isinstance(tickets,list) else []):
        tmap.setdefault(t["group_id"],[]).append(t)
    imap={}
    for ir in (inv_rows if isinstance(inv_rows,list) else []):
        imap.setdefault(ir["ticket_id"],[]).append(ir)
    inv_names={i["id"]:i["name"] for i in (investors if isinstance(investors,list) else [])}
    ctx.user_data["pd_groups"]=groups; ctx.user_data["pd_tmap"]=tmap
    ctx.user_data["pd_imap"]=imap; ctx.user_data["pd_inv_names"]=inv_names
    rows=[]
    for g in groups[:10]:
        ts=tmap.get(g["id"],[]); total_s=sum(t["stake"] for t in ts); total_p=sum(t["potencial"] for t in ts)
        cuota=round(total_p/total_s,3) if total_s>0 else 0
        rows.append([InlineKeyboardButton(
            f"▸ {g['descr'] or '(sin desc)'}  {fmt(total_s)} @{cuota}",
            callback_data=f"pd_{g['id']}")])
    msg=await src.reply_text("⏳ *PENDIENTES*",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    ctx.user_data["pd_list_msg"]=msg.message_id
    return P_LIST

async def r_pd_select(u:Update, ctx):
    q=u.callback_query; await q.answer()
    gid_val=q.data[3:]
    groups=ctx.user_data.get("pd_groups",[])
    g=next((x for x in groups if x["id"]==gid_val),None)
    if not g: await q.edit_message_text("⚠️ No encontrado."); return ConversationHandler.END
    tmap=ctx.user_data.get("pd_tmap",{}); imap=ctx.user_data.get("pd_imap",{})
    inv_names=ctx.user_data.get("pd_inv_names",{})
    ts=tmap.get(gid_val,[]); total_s=sum(t["stake"] for t in ts); total_p=sum(t["potencial"] for t in ts)
    cuota=round(total_p/total_s,3) if total_s>0 else 0
    inv_totals={}
    for t in ts:
        for ir in imap.get(t["id"],[]):
            inv_totals[ir["investor_id"]]=inv_totals.get(ir["investor_id"],0)+ir["stake"]
    lines=[f"*{g['descr'].upper()}*   _{g['date']}_", ""]
    for i,t in enumerate(ts,1):
        lines.append(f"#{i}  {t['tipster']} · {t['casa']}")
        lines.append(f"     {fmt(t['stake'])}  @{t['cuota']}  →  {fmt(t['potencial'])}")
    lines.append("")
    lines.append(f"──────────")
    lines.append(f"@{cuota}   {fmt(total_s)} apostado   pot {fmt(total_p)}")
    if inv_totals:
        lines.append("")
        for iid,stake in inv_totals.items():
            pot=round(stake*cuota,2)
            lines.append(f"💼 {inv_names.get(iid,'?')}   {fmt(stake)} → +{fmt(pot-stake)}")
    ctx.user_data["pd_gid"]=gid_val; ctx.user_data["pd_tickets"]=ts
    ctx.user_data["pd_cuota"]=cuota
    await q.edit_message_text("\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Win",   callback_data="pr_win"),
             InlineKeyboardButton("❌ Loss",  callback_data="pr_loss"),
             InlineKeyboardButton("🔵 Void",  callback_data="pr_void"),
             InlineKeyboardButton("✏️ Exacto",callback_data="pr_exact")],
            [InlineKeyboardButton("🗑 Eliminar",callback_data="pr_del"),
             InlineKeyboardButton("↩ Lista",   callback_data="pr_back")]
        ]), parse_mode="Markdown")
    return P_DETAIL

async def r_pd_action(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="pr_back":
        return await show_pending_list(q.message, ctx)
    if q.data=="pr_del":
        g=next((x for x in ctx.user_data.get("pd_groups",[]) if x["id"]==ctx.user_data.get("pd_gid","")),{})
        await q.edit_message_text(f"⚠️ ¿Eliminar *{g.get('descr','')}*?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Sí",callback_data="pdel_yes"),
                InlineKeyboardButton("↩ No", callback_data="pdel_no")
            ]]),parse_mode="Markdown")
        return P_DEL
    tickets=ctx.user_data.get("pd_tickets",[]); action=q.data[3:]
    ctx.user_data["pd_returns"]={}
    ctx.user_data["pd_idx"]=0
    if len(tickets)==1 and action in ("win","loss","void"):
        t=tickets[0]; s=t["stake"]
        if action=="win":    ctx.user_data["pd_returns"][t["id"]]=t["potencial"]
        elif action=="loss": ctx.user_data["pd_returns"][t["id"]]=0
        elif action=="void": ctx.user_data["pd_returns"][t["id"]]=s
        return await save_result(q, ctx)
    elif action in ("win","loss","void"):
        for t in tickets:
            if action=="win":    ctx.user_data["pd_returns"][t["id"]]=t["potencial"]
            elif action=="loss": ctx.user_data["pd_returns"][t["id"]]=0
            elif action=="void": ctx.user_data["pd_returns"][t["id"]]=t["stake"]
        return await show_result_summary(q, ctx)
    elif action=="exact":
        return await ask_exact_ticket(q, ctx)

async def ask_exact_ticket(src, ctx):
    tickets=ctx.user_data.get("pd_tickets",[]); idx=ctx.user_data.get("pd_idx",0)
    if idx>=len(tickets): return await show_result_summary(src, ctx)
    t=tickets[idx]
    await edit_or_new(src,
        f"✏️ #{idx+1}/{len(tickets)}   {t['tipster']} · {t['casa']}\n"
        f"{fmt(t['stake'])}  @{t['cuota']}  →  {fmt(t['potencial'])}\n\nMonto retornado:",[[
        InlineKeyboardButton("✅ Win",  callback_data="ex_win"),
        InlineKeyboardButton("❌ Loss", callback_data="ex_loss"),
        InlineKeyboardButton("🔵 Void", callback_data="ex_void"),
    ]])
    return P_EXACT

async def r_exact_btn(u:Update, ctx):
    q=u.callback_query; await q.answer()
    tickets=ctx.user_data.get("pd_tickets",[]); idx=ctx.user_data.get("pd_idx",0)
    t=tickets[idx]
    if q.data=="ex_win":    ctx.user_data["pd_returns"][t["id"]]=t["potencial"]
    elif q.data=="ex_loss": ctx.user_data["pd_returns"][t["id"]]=0
    elif q.data=="ex_void": ctx.user_data["pd_returns"][t["id"]]=t["stake"]
    ctx.user_data["pd_idx"]+=1
    return await ask_exact_ticket(q, ctx)

async def r_exact_txt(u:Update, ctx):
    try: ret=float(u.message.text.strip().replace(",",".")); assert ret>=0
    except: await u.message.reply_text("⚠️  Número válido:"); return P_EXACT
    await try_del(u.message)
    tickets=ctx.user_data.get("pd_tickets",[]); idx=ctx.user_data.get("pd_idx",0)
    ctx.user_data["pd_returns"][tickets[idx]["id"]]=ret
    ctx.user_data["pd_idx"]+=1
    return await ask_exact_ticket(u.message, ctx)

async def show_result_summary(src, ctx):
    tickets=ctx.user_data.get("pd_tickets",[]); rets=ctx.user_data.get("pd_returns",{})
    tr=sum(rets.values()); ts=sum(t["stake"] for t in tickets); pnl=round(tr-ts,2)
    sign="+" if pnl>=0 else ""
    lines=["*RESULTADO*", ""]
    for t in tickets:
        r=rets.get(t["id"],0); s=t["stake"]
        lbl="✅" if r>s else ("🔵" if abs(r-s)<0.01 else "❌")
        lines.append(f"{lbl}  {t['tipster']} · {t['casa']}   {fmt(r)}")
    lines.append("")
    lines.append(f"──────────")
    lines.append(f"P&L   *{sign}{fmt(pnl)}*")
    await edit_or_new(src,"\n".join(lines),[[
        InlineKeyboardButton("✅ Guardar",  callback_data="rs_yes"),
        InlineKeyboardButton("❌ Cancelar", callback_data="rs_no")
    ]])
    return P_CONFIRM_RES

async def r_result_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="rs_no": await q.edit_message_text("Cancelado."); return ConversationHandler.END
    return await save_result(q, ctx)

async def save_result(src, ctx):
    tickets=ctx.user_data.get("pd_tickets",[]); rets=ctx.user_data.get("pd_returns",{})
    gid_val=ctx.user_data.get("pd_gid","")
    try:
        grp=await sb_get("bet_groups",f"id=eq.{gid_val}&select=descr")
        desc=(grp[0]["descr"] if isinstance(grp,list) and grp else "Apuesta")
        ts_total=sum(t["stake"] for t in tickets)
        tr_total=sum(rets.get(t["id"],0) for t in tickets)
        comb_c=tr_total/ts_total if ts_total>0 else 1
        for t in tickets:
            await sb_patch("tickets","id",t["id"],{"returned":rets.get(t["id"],0),"status":"settled"})
        inv_group={}
        for t in tickets:
            rows=await sb_get("ticket_investors",f"ticket_id=eq.{t['id']}")
            if isinstance(rows,list):
                for ir in rows:
                    if ir["investor_id"] not in inv_group:
                        inv_group[ir["investor_id"]]={"stake":0,"ticket_id":t["id"]}
                    inv_group[ir["investor_id"]]["stake"]+=ir["stake"]
        for iid,data in inv_group.items():
            inv_stake=data["stake"]; inv_ret=round(inv_stake*comb_c,2)
            inv_pnl=round(inv_ret-inv_stake,2)
            if abs(inv_pnl)<0.01: continue
            await sb_post("investor_movements",{"id":gid(),"investor_id":iid,
                "type":"bet_result","amount":inv_pnl,"note":desc,
                "date":str(date.today()),"ticket_id":data["ticket_id"]})
        await sb_patch("bet_groups","id",gid_val,{"status":"settled"})
        pnl=round(tr_total-ts_total,2)
        await delete_old_confirm(gid_val, ctx.bot)
        chat_id=src.message.chat_id if hasattr(src,"message") else src.effective_chat.id
        result_text=build_result_msg(desc, tickets, rets)
        new_msg=await ctx.bot.send_message(chat_id=chat_id, text=result_text, parse_mode="Markdown")
        await sb_patch("bet_groups","id",gid_val,{"tg_chat_id":chat_id,"tg_msg_id":new_msg.message_id})
        try:
            if hasattr(src,"message"): await src.message.delete()
        except: pass
    except Exception as e:
        if hasattr(src,"edit_message_text"): await src.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def r_pd_del_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="pdel_no":
        return await r_pd_select_by_id(q, ctx, ctx.user_data.get("pd_gid",""))
    gid_val=ctx.user_data.get("pd_gid","")
    try:
        tix=await sb_get("tickets",f"group_id=eq.{gid_val}")
        if isinstance(tix,list):
            for t in tix:
                mvs=await sb_get("investor_movements",f"ticket_id=eq.{t['id']}")
                if isinstance(mvs,list):
                    for mv in mvs: await sb_delete("investor_movements","id",mv["id"])
                await sb_delete("ticket_investors","ticket_id",t["id"])
                await sb_delete("tickets","id",t["id"])
        await sb_delete("bet_groups","id",gid_val)
        await q.edit_message_text("Eliminada.")
    except Exception as e: await q.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def r_pd_select_by_id(src, ctx, gid_val):
    ctx.user_data["pd_gid"]=gid_val
    groups=ctx.user_data.get("pd_groups",[])
    g=next((x for x in groups if x["id"]==gid_val),{})
    ctx.user_data["pd_tickets"]=ctx.user_data.get("pd_tmap",{}).get(gid_val,[])
    return await r_pd_select(type("FakeUpdate",(),{"callback_query":src})(), ctx)

# ══════════════════════════════════════════════════════════════════════════════
# /hoy
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_hoy(u:Update, ctx):
    if not is_ok(u): return
    await try_del(u.message)
    today=str(date.today())
    groups=await sb_get("bet_groups",f"date=eq.{today}&order=created_at.desc")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text(f"Sin apuestas hoy 📭"); return
    tall=await sb_get("tickets","order=group_id.asc"); tmap={}
    for t in (tall if isinstance(tall,list) else []): tmap.setdefault(t["group_id"],[]).append(t)
    lines=[f"📊 *{today}*\n"]; total=0; total_pnl=0
    for g in groups:
        ts=tmap.get(g["id"],[]); gs_s=sum(t["stake"] for t in ts); total+=gs_s
        icon="⏳" if g["status"]=="pending" else "✅"
        pnl_str=""
        if g["status"]=="settled":
            ret=sum((t.get("returned") or 0) for t in ts)
            p=round(ret-gs_s,2); total_pnl+=p
            pnl_str=f"  *{'+' if p>=0 else ''}{fmt(p)}*"
        cuota=round(sum(t["potencial"] for t in ts)/gs_s,3) if gs_s>0 else 0
        lines.append(f"{icon}  *{(g['descr'] or 'sin desc').upper()}*")
        lines.append(f"     {fmt(gs_s)}  @{cuota}{pnl_str}")
    lines.append(f"\n──────────")
    lines.append(f"Total   {fmt(total)}")
    if total_pnl!=0: lines.append(f"P&L     *{'+' if total_pnl>=0 else ''}{fmt(total_pnl)}*")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# Cancel / menu
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_cancelar(u:Update, ctx):
    await clear(ctx, u.effective_chat.id, ctx.bot)
    rs(ctx); await u.message.reply_text("Cancelado.", reply_markup=MENU_KB)
    return ConversationHandler.END

async def menu_handler(u:Update, ctx):
    txt=u.message.text
    if txt=="📝 Nueva apuesta": return await cmd_nueva(u, ctx)
    if txt=="⏳ Pendientes":    return await cmd_pendientes(u, ctx)
    if txt=="📊 Hoy":           await cmd_hoy(u, ctx)
    if txt=="❌ Cancelar":      return await cmd_cancelar(u, ctx)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    app=Application.builder().token(BOT_TOKEN).build()

    nueva_conv=ConversationHandler(
        entry_points=[CommandHandler("nueva",cmd_nueva),
                      MessageHandler(filters.Regex("^📝 Nueva apuesta$"),cmd_nueva)],
        states={
            S_DESC:    [MessageHandler(filters.TEXT&~filters.COMMAND,r_desc)],
            S_TIPSTER: [CallbackQueryHandler(r_tipster,pattern="^(tip_|back)"),
                        MessageHandler(filters.TEXT&~filters.COMMAND,r_tipster_txt)],
            S_N_BOOKIES:[CallbackQueryHandler(r_n_bookies,pattern="^(nb_|back)"),
                         MessageHandler(filters.TEXT&~filters.COMMAND,r_n_bookies_txt)],
            S_BOOKIE:  [CallbackQueryHandler(r_bookie,pattern="^(bk_|back)"),
                        MessageHandler(filters.TEXT&~filters.COMMAND,r_bookie_txt)],
            S_TICKETS: [CallbackQueryHandler(r_bookie,pattern="^back"),
                        MessageHandler(filters.TEXT&~filters.COMMAND,r_tickets)],
            S_INV:     [CallbackQueryHandler(r_inv_btn,pattern="^(inv_|back)"),
                        MessageHandler(filters.TEXT&~filters.COMMAND,r_inv_txt)],
            S_CONFIRM: [CallbackQueryHandler(r_confirm,pattern="^(ok_|back)")],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        conversation_timeout=TIMEOUT,
        allow_reentry=True,
    )

    pending_conv=ConversationHandler(
        entry_points=[CommandHandler("pendientes",cmd_pendientes),
                      MessageHandler(filters.Regex("^⏳ Pendientes$"),cmd_pendientes)],
        states={
            P_LIST:       [CallbackQueryHandler(r_pd_select,pattern="^pd_")],
            P_DETAIL:     [CallbackQueryHandler(r_pd_action,pattern="^pr_")],
            P_EXACT:      [CallbackQueryHandler(r_exact_btn,pattern="^ex_"),
                           MessageHandler(filters.TEXT&~filters.COMMAND,r_exact_txt)],
            P_CONFIRM_RES:[CallbackQueryHandler(r_result_confirm,pattern="^rs_")],
            P_DEL:        [CallbackQueryHandler(r_pd_del_confirm,pattern="^pdel_")],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        conversation_timeout=TIMEOUT,
        allow_reentry=True,
    )

    app.add_handler(nueva_conv)
    app.add_handler(pending_conv)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("cancelar",cmd_cancelar))
    app.add_handler(CommandHandler("hoy",cmd_hoy))
    app.add_handler(MessageHandler(filters.Regex("^(📊 Hoy|❌ Cancelar)$"),menu_handler))

    print("BetLog Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
