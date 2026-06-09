#!/usr/bin/env python3
"""
lovepawfect <- Zoodrop Sync  (Varianten gruppiert nach SUCHNUMMER)
  --mode full        : zoodrop_utf8.csv -> Produkte (mit Varianten) anlegen/aktualisieren, inkl. Bilder
  --mode update      : update_utf8.csv  -> nur Preis/Bestand/Policy je Variante (schnell, taeglich)
  --mode retag       : zoodrop_utf8.csv -> nur Tags/Produkttyp je Produkt
  --mode collections : Smart Collections (Welten + Kategorien) idempotent
  --mode wipe        : ALLE Produkte loeschen (Neuaufbau)
  --dry-run / --limit N / --local PATH

Gruppierung: alle Zeilen mit gleicher SUCHNUMMER = EIN Produkt; jede Zeile (EAN) = eine Variante.
Variantenname = Teil des TITEL nach dem gemeinsamen Praefix. Einzel-Artikel = 1 Variante ohne Optionen.
Preisregel: Brutto = EK*FACTOR, gedeckelt UVP-1, Marge>=MIN_MARGIN, Charm x,99.
Tags: Tier + Bedarf + Marke. Bildlose Gruppen uebersprungen.
"""
import os, sys, csv, io, json, time, base64, argparse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

SHOP   = os.environ.get("SHOPIFY_SHOP", "lovepawfect.myshopify.com")
TOKEN  = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
APIV   = os.environ.get("SHOPIFY_API_VERSION", "2025-01")
FACTOR = float(os.environ.get("PRICE_FACTOR", "1.7"))
MIN_MARGIN = float(os.environ.get("MIN_MARGIN", "0.30"))
STATE_FILE = os.environ.get("STATE_FILE", "sync_state.json")
SKIP_NO_IMAGE = os.environ.get("SKIP_NO_IMAGE", "1") == "1"
FULL_URL   = "https://www.zoodrop.de/csv/download/zoodrop_utf8.csv"
UPDATE_URL = "https://www.zoodrop.de/csv/download/update_utf8.csv"

def log(*a): print(*a, flush=True)

# ---------- Taxonomie ----------
TIER_MAP = {"Hundewelt":"Hund","Katzenwelt":"Katze","Kleintierwelt":"Kleintier","Vogelwelt":"Vogel",
            "Aquarienwelt":"Aquaristik","Terrarienwelt":"Reptil","Gartenwelt":"Garten"}
# Präzises Mapping der 2. Kategorie-Ebene (Bedarf) – exakt statt Substring über den ganzen Pfad
L1_MAP = {
    "hundespielzeug & sport":["Spielzeug"], "katzenspielzeug":["Spielzeug"],
    "betten & körbe":["Schlafen"], "katzenbetten & katzenkörbe":["Schlafen"],
    "pflege & gesundheit":["Pflege"], "hygiene & reinigung":["Pflege"],
    "katzen nassfutter":["Nassfutter","Futter"], "hunde-nassfutter":["Nassfutter","Futter"], "hundefutter barf":["Nassfutter","Futter"],
    "hundetrockenfutter":["Trockenfutter","Futter"], "katzen trockenfutter":["Trockenfutter","Futter"],
    "hundesnacks":["Snacks"], "katzensnacks":["Snacks"], "kleintierfutter & snacks":["Futter","Snacks"],
    "kratzbäume & -möbel":["Kratzbäume"],
    "hundenäpfe & tränken":["Näpfe"], "katzennäpfe & tränken":["Näpfe"],
    "reise & transport":["Transport"], "katzentransport & reise":["Transport"], "transport":["Transport"], "unterwegs":["Transport"],
    "toiletten & katzenstreu":["Katzenklo"],
    "futterergänzung":["Futterergänzung"], "futterergänzung für katzen":["Futterergänzung"], "picksteine & mineralien":["Futterergänzung"],
    "erziehung":["Erziehung"], "hundebekleidung":["Bekleidung"],
    "gartenvogelfutter":["Futter"], "vogelfutter":["Futter"], "zierfischfutter":["Futter"], "teichfischfutter":["Futter"],
    "koifutter":["Futter"], "futtertabletten":["Futter"], "garnelenfutter":["Futter"], "schildkrötenfutter":["Futter"],
    "wasserpflege":["Wasserpflege"], "pflanzenpflege":["Wasserpflege"], "teichpflanzenpflege":["Wasserpflege"],
    "filtertechnik":["Technik"], "aquarium technik":["Technik"], "luftpumpen":["Technik"], "aquarienheizer":["Technik"],
    "futterhäuser & nistkästen":["Käfige"], "freigehege für kleintiere":["Käfige"], "außenställe":["Käfige"],
    "arzneimittel":["Gesundheit"], "bachblüten globuli":["Gesundheit"],
    "ausstattung & zubehör":["Zubehör"], "aquarium zubehör":["Zubehör"], "haus & hof":["Zubehör"], "balkon & garten":["Zubehör"],
    "futterautomaten":["Näpfe"], "futterspender & tränken":["Näpfe"], "trinkbrunnen":["Näpfe"],
}
def map_category(lvl1, lvl2):
    l1=lvl1.strip().lower(); l2=(lvl2 or "").strip().lower()
    # Leinen/Halsbänder/Geschirre über die 3. Ebene aufschlüsseln
    if "halsbänder & leinen" in l1 or ("halsband" in l1 and "geschirr" in l1):
        if "geschirr" in l2: return ["Geschirre"]
        if "leine" in l2: return ["Leinen"]
        if "halsband" in l2: return ["Halsbänder"]
        return ["Leinen","Halsbänder","Geschirre"] if "geschirr" in l1 else ["Leinen","Halsbänder"]
    if l1 in L1_MAP: return list(L1_MAP[l1])
    # konservativer Fallback NUR auf der Kategorie-Ebene (nicht ganzer Pfad)
    if "napf" in l1 or "näpfe" in l1 or "tränke" in l1 or "automat" in l1 or "spender" in l1 or "brunnen" in l1: return ["Näpfe"]
    if "trockenfutter" in l1: return ["Trockenfutter","Futter"]
    if "nassfutter" in l1: return ["Nassfutter","Futter"]
    if "snack" in l1: return ["Snacks"]
    if "ergänz" in l1: return ["Futterergänzung"]
    if "futter" in l1: return ["Futter"]
    if "spielzeug" in l1: return ["Spielzeug"]
    if "kratz" in l1: return ["Kratzbäume"]
    if "bett" in l1 or "körb" in l1 or "schlaf" in l1: return ["Schlafen"]
    if "streu" in l1 or "toilette" in l1: return ["Katzenklo"]
    if "leine" in l1: return ["Leinen"]
    if "halsband" in l1: return ["Halsbänder"]
    if "geschirr" in l1: return ["Geschirre"]
    if "transport" in l1 or "reise" in l1: return ["Transport"]
    if "pflege" in l1 or "hygiene" in l1: return ["Pflege"]
    if "gesundheit" in l1 or "arznei" in l1: return ["Gesundheit"]
    if "bekleidung" in l1: return ["Bekleidung"]
    if "käfig" in l1 or "gehege" in l1 or "stall" in l1 or "nistkast" in l1: return ["Käfige"]
    if "technik" in l1 or "filter" in l1 or "pumpe" in l1 or "heizer" in l1: return ["Technik"]
    if "wasserpflege" in l1: return ["Wasserpflege"]
    if "heu" in l1 or "einstreu" in l1: return ["Einstreu"]
    if "erziehung" in l1 or "training" in l1: return ["Erziehung"]
    return ["Zubehör"]
WORLD_HANDLE={"Hund":"hundewelt","Katze":"katzenwelt","Kleintier":"kleintierwelt","Vogel":"vogelwelt","Aquaristik":"aquarienwelt","Reptil":"terrarienwelt","Garten":"gartenwelt"}
WORLD_SLUG={"Hund":"hund","Katze":"katze","Kleintier":"kleintier","Vogel":"vogel","Aquaristik":"aquaristik","Reptil":"reptil","Garten":"garten"}
WORLD_TITLE={"Hund":"Hundewelt","Katze":"Katzenwelt","Kleintier":"Kleintierwelt","Vogel":"Vogelwelt","Aquaristik":"Aquaristik","Reptil":"Terraristik","Garten":"Garten & Outdoor"}
CAT_HANDLE={"Futter":"futter","Trockenfutter":"trockenfutter","Nassfutter":"nassfutter","Futterergänzung":"futterergaenzung","Snacks":"snacks","Spielzeug":"spielzeug","Leinen":"leinen","Halsbänder":"halsbaender","Geschirre":"geschirre","Schlafen":"schlafen","Näpfe":"naepfe","Pflege":"pflege","Transport":"transport","Erziehung":"erziehung","Bekleidung":"bekleidung","Gesundheit":"gesundheit","Kratzbäume":"kratzbaeume","Katzenklo":"katzenklo","Einstreu":"einstreu","Käfige":"kaefige","Zubehör":"zubehoer","Technik":"technik","Wasserpflege":"wasserpflege"}
WORLD_CATS={
    "Hund":["Futter","Trockenfutter","Nassfutter","Futterergänzung","Snacks","Spielzeug","Leinen","Halsbänder","Geschirre","Schlafen","Näpfe","Pflege","Transport","Erziehung","Bekleidung","Gesundheit"],
    "Katze":["Futter","Trockenfutter","Nassfutter","Futterergänzung","Snacks","Spielzeug","Kratzbäume","Schlafen","Katzenklo","Näpfe","Pflege","Transport","Geschirre","Gesundheit"],
    "Kleintier":["Futter","Einstreu","Käfige","Zubehör","Transport","Pflege"],
    "Vogel":["Futter","Käfige","Zubehör"],"Aquaristik":["Futter","Wasserpflege","Technik","Zubehör"],
    "Reptil":["Zubehör","Technik"],"Garten":[],
}

# ---------- CSV / Mapping ----------
def fnum(s):
    s=(s or "").strip().replace(",",".")
    try: return float(s)
    except ValueError: return 0.0

def load_rows(mode, local):
    if local:
        return list(csv.DictReader(open(local, encoding="utf-8", errors="replace"), delimiter=";", quotechar='"'))
    url = UPDATE_URL if mode=="update" else FULL_URL
    log("Lade Feed:", url)
    req=urllib.request.Request(url, headers={"User-Agent":"lovepawfect-sync/1.0"})
    data=None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                data=r.read().decode("utf-8", errors="replace"); break
        except Exception as e:
            log("Feed-Download Fehler, retry", attempt+1, str(e)[:100]); time.sleep(5*(attempt+1))
    if data is None: raise RuntimeError("Feed-Download fehlgeschlagen")
    return list(csv.DictReader(io.StringIO(data), delimiter=";", quotechar='"'))

def group_rows(rows):
    groups={}; order=[]
    for r in rows:
        key=(r.get("SUCHNUMMER") or "").strip() or (r.get("ARTIKELNUMMER") or "").strip() or (r.get("EAN") or "").strip()
        if not key: continue
        if key not in groups: groups[key]=[]; order.append(key)
        groups[key].append(r)
    return [(k, groups[k]) for k in order]

def is_available(row):
    if row.get("VERKAUFSSTATUS","").strip()!="Im Verkauf": return False
    return fnum(row.get("BESTAND"))>0 or row.get("LAGERSTATUS","").strip().lower()=="fullfillment"

def compute_gross(ek, uvp, vat):
    if ek<=0: return None
    g=ek*FACTOR
    cap=uvp-0.01 if uvp and uvp>0 else None    # nie ÜBER UVP, aber nicht künstlich auf UVP-1 drücken
    if cap is not None and g>cap: g=cap         # nur kappen, wenn wir drüber liegen
    if g<=ek: return None                       # niemals mit Verlust
    if (g/(1.0+vat/100.0))-ek < MIN_MARGIN: return None
    charm=float(int(g))+0.99                    # x,99 wenn es passt (Marge + unter UVP)
    if charm>ek and (cap is None or charm<=cap) and abs(charm-g)<=1.0: g=charm
    return round(g,2)

def categories(row):
    raw=row.get("KATEGORIE","") or ""
    tiers=[]; ptype=""; cats=[]
    for p in [x for x in raw.split("|") if x.strip()]:
        L=[l.strip() for l in p.split(">") if l.strip()]
        if not L: continue
        if not ptype: ptype=L[0]
        t=TIER_MAP.get(L[0])
        if t and t not in tiers: tiers.append(t)
        if len(L)>=2:
            lvl2=L[2] if len(L)>=3 else ""
            for c in map_category(L[1], lvl2):
                if c not in cats: cats.append(c)
    # Kleintier-Streu ist Einstreu, nicht Katzenklo
    cats=["Einstreu" if (c=="Katzenklo" and "Katze" not in tiers) else c for c in cats]
    tags=[]
    for t in tiers+cats:
        if t and t not in tags: tags.append(t)
    vendor=row.get("HERSTELLER","").strip()
    if vendor and vendor not in tags: tags.append(vendor)
    return ptype, tags

def images(row):
    return [row[f"BILD{i}"].strip() for i in range(1,9) if row.get(f"BILD{i}","").strip()]

def group_images(rows):
    seen=set(); urls=[]
    for r in rows:
        for u in images(r):
            if u not in seen: seen.add(u); urls.append(u)
    return urls[:6]

def body_html(row):
    html=row.get("BESCHREIBUNG","") or ""
    gp,gpe=fnum(row.get("GRUNDPREIS")),(row.get("GRUNDPREIS_EINHEIT") or "").strip()
    if gp>0 and gpe: html+=f'<p class="grundpreis"><small>Grundpreis: {gp:.2f} € / {gpe}</small></p>'
    return html

def fetch_image(url):
    try:
        req=urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 (lovepawfect-sync)"})
        with urllib.request.urlopen(req, timeout=45) as r:
            return base64.b64encode(r.read()).decode()
    except Exception: return None

def variant_labels(titles):
    """gemeinsamer Praefix entfernen -> Varianten-Labels + Produkttitel"""
    if len(titles)==1: return [""], titles[0]
    s1=min(titles); s2=max(titles); i=0
    while i<len(s1) and i<len(s2) and s1[i]==s2[i]: i+=1
    seps=" -–—/|,·"
    ptitle=titles[0][:i].strip(seps) or titles[0]
    raw=[t[i:].strip(seps) if t[:i]==titles[0][:i] else t for t in titles]
    out=[]; seen={}
    for idx,lab in enumerate(raw):
        if not lab: lab="Variante "+str(idx+1)
        if lab in seen: seen[lab]+=1; lab=f"{lab} ({seen[lab]})"
        else: seen[lab]=1
        out.append(lab[:80])
    return out, ptitle

# ---------- Shopify REST ----------
def api(method, path, body=None):
    url=f"https://{SHOP}/admin/api/{APIV}/{path}"
    data=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request(url, data=data, method=method, headers={"X-Shopify-Access-Token":TOKEN,"Content-Type":"application/json"})
    for attempt in range(7):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                used,cap=(int(x) for x in r.headers.get("X-Shopify-Shop-Api-Call-Limit","0/40").split("/"))
                if used>cap*0.7: time.sleep(0.6)
                txt=r.read().decode(); return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(2.0*(attempt+1)); continue
            if e.code>=500: time.sleep(1.5*(attempt+1)); continue
            log("HTTP",e.code,method,path,e.read().decode()[:300]); raise
        except Exception as e:                       # Timeout/Verbindungsabbruch -> retry mit Backoff
            if attempt>=6: log("NET",method,path,str(e)[:120]); raise
            time.sleep(2.0*(attempt+1)); continue
    raise RuntimeError(f"API failed: {method} {path}")

def get_location_id():
    locs=api("GET","locations.json").get("locations",[])
    for l in locs:
        if l.get("active"): return l["id"]
    return locs[0]["id"] if locs else None

def load_state():
    try: return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception: return {}
def save_state(st): json.dump(st, open(STATE_FILE,"w",encoding="utf-8"), ensure_ascii=False)

def build_state_from_store(state):
    """barcode(EAN) -> {p:product_id, v:variant_id, i:inventory_item_id}"""
    since,total=0,0
    while True:
        data=api("GET", f"products.json?limit=250&since_id={since}&fields=id,variants")
        prods=data.get("products",[])
        if not prods: break
        for p in prods:
            since=max(since,p["id"])
            for v in p.get("variants",[]):
                bc=(v.get("barcode") or "").strip()
                if bc: state.setdefault(bc,{"p":p["id"],"v":v["id"],"i":v.get("inventory_item_id")})
            total+=1
        log(f"  ...{total} vorhandene Produkte gelesen")
    log(f"State aus Store: {total} Produkte, {len(state)} Barcodes")

# ---------- Create (gruppiert) ----------
def qualifying(group):
    q=[]
    for r in group:
        if not is_available(r): continue
        g=compute_gross(fnum(r.get("VERKAUFSPREIS")), fnum(r.get("UVP")), fnum(r.get("MWST") or "19"))
        if g is None: continue
        q.append((r,g))
    return q

def create_grouped(group, loc_id):
    q=qualifying(group)
    if not q: return "drop", None
    if len(q)>100: q=q[:100]                 # Shopify-Varianten-Limit
    rows=[r for r,_ in q]
    imgs_urls=group_images(rows)
    if SKIP_NO_IMAGE and not imgs_urls: return "noimg", None
    art0=((rows[0].get("ARTIKELNUMMER","") or "img").strip().replace(" ","-")) or "img"
    imgs=[]
    with ThreadPoolExecutor(max_workers=5) as ex:           # Bilder parallel laden (Speed)
        for idx,b in enumerate(ex.map(fetch_image, imgs_urls)):
            if b: imgs.append({"attachment":b,"filename":f"{art0}-{idx+1}.jpg"})
    if SKIP_NO_IMAGE and not imgs: return "noimg", None
    rep=rows[0]; ptype,tags=categories(rep); multi=len(q)>1
    labels,ptitle=variant_labels([(r.get("TITEL") or "").strip() for r in rows])
    variants=[]
    for idx,(r,g) in enumerate(q):
        fulfill=r.get("LAGERSTATUS","").strip().lower()=="fullfillment"
        var={"price":f"{g:.2f}","sku":r.get("ARTIKELNUMMER","").strip(),"barcode":r.get("EAN","").strip(),
             "inventory_management":"shopify","inventory_policy":"continue" if fulfill else "deny","taxable":True,
             "inventory_quantity":max(int(fnum(r.get("BESTAND"))),0)}   # Bestand direkt beim Create (spart Inventar-Calls)
        if multi: var["option1"]=labels[idx]
        variants.append(var)
    payload={"product":{"title":ptitle,"body_html":body_html(rep),"vendor":rep.get("HERSTELLER","").strip(),
        "product_type":ptype,"tags":", ".join(tags),"status":"active","images":imgs,"variants":variants}}
    if multi: payload["product"]["options"]=[{"name":"Variante"}]
    p=api("POST","products.json",payload)["product"]
    vmap={}
    for var in p["variants"]:
        bc=(var.get("barcode") or "").strip()
        vmap[bc]={"v":var["id"],"i":var.get("inventory_item_id")}
    return "created", {"p":p["id"],"variants":vmap}

def update_grouped(group, state, loc_id, full):
    pid=None
    for r in group:
        bc=r.get("EAN","").strip()
        if bc in state: pid=state[bc]["p"]; break
    if not pid: return "miss"
    for r,g in qualifying(group):
        bc=r.get("EAN","").strip(); fulfill=r.get("LAGERSTATUS","").strip().lower()=="fullfillment"
        if bc in state:
            api("PUT",f"variants/{state[bc]['v']}.json",{"variant":{"id":state[bc]['v'],"price":f"{g:.2f}","inventory_policy":"continue" if fulfill else "deny"}})
            if loc_id and state[bc].get("i"):
                api("POST","inventory_levels/set.json",{"location_id":loc_id,"inventory_item_id":state[bc]["i"],"available":max(int(fnum(r.get('BESTAND'))),0)})
    if full:
        ptype,tags=categories(group[0])
        api("PUT",f"products/{pid}.json",{"product":{"id":pid,"status":"active","product_type":ptype,"tags":", ".join(tags)}})
    return "updated"

# ---------- Collections ----------
def find_collection(handle):
    sc=api("GET",f"smart_collections.json?handle={handle}").get("smart_collections",[])
    return sc[0] if sc else None
def upsert_collection(handle,title,conds):
    rules=[{"column":"tag","relation":"equals","condition":c} for c in conds]
    body={"smart_collection":{"title":title,"handle":handle,"disjunctive":False,"rules":rules,"published":True}}
    ex=find_collection(handle)
    if ex:
        body["smart_collection"]["id"]=ex["id"]; api("PUT",f"smart_collections/{ex['id']}.json",body); return "updated"
    api("POST","smart_collections.json",body); return "created"
def make_collections():
    n={"created":0,"updated":0}
    for world,slug in WORLD_SLUG.items():
        n[upsert_collection(WORLD_HANDLE[world],WORLD_TITLE[world],[world])]+=1
        for cat in WORLD_CATS.get(world,[]):
            n[upsert_collection(f"{slug}-{CAT_HANDLE[cat]}",f"{world} · {cat}",[world,cat])]+=1
    log(f"Collections: {n['created']} neu, {n['updated']} aktualisiert.")

def wipe_all():
    total=0
    while True:
        prods=api("GET","products.json?limit=250&fields=id").get("products",[])
        if not prods: break
        for p in prods: api("DELETE",f"products/{p['id']}.json"); total+=1
        log(f"  ...{total} geloescht")
    try: os.remove(STATE_FILE)
    except OSError: pass
    log(f"Wipe fertig: {total} Produkte geloescht, State geleert.")

# ---------- Main ----------
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode",choices=["full","update","retag","collections","wipe"],default="full")
    ap.add_argument("--dry-run",action="store_true"); ap.add_argument("--limit",type=int,default=0); ap.add_argument("--local",default="")
    args=ap.parse_args()

    if args.mode=="collections":
        if args.dry_run:
            for w,s in WORLD_SLUG.items():
                log(WORLD_HANDLE[w],"->",w)
                for c in WORLD_CATS.get(w,[]): log(f"  {s}-{CAT_HANDLE[c]} -> {w}+{c}")
            return
        make_collections(); return
    if args.mode=="wipe":
        if args.dry_run: log("[DRY-RUN] wuerde alle Produkte loeschen."); return
        wipe_all(); return

    rows=load_rows(args.mode,args.local); log(f"{len(rows)} Zeilen geladen.")
    state={}   # immer frisch aus dem Store (verhindert veraltete Zuordnungen nach Wipe)
    loc_id=None if args.dry_run else get_location_id()
    if not args.dry_run: log("Location:",loc_id); build_state_from_store(state)

    if args.mode=="update":
        st={"updated":0,"miss":0,"errors":0}
        for r in rows:
            bc=r.get("EAN","").strip()
            if bc not in state: st["miss"]+=1; continue
            if not is_available(r): continue
            g=compute_gross(fnum(r.get("VERKAUFSPREIS")),fnum(r.get("UVP")),fnum(r.get("MWST") or "19"))
            if g is None: continue
            if args.dry_run: st["updated"]+=1; continue
            try:
                fulfill=r.get("LAGERSTATUS","").strip().lower()=="fullfillment"
                api("PUT",f"variants/{state[bc]['v']}.json",{"variant":{"id":state[bc]['v'],"price":f"{g:.2f}","inventory_policy":"continue" if fulfill else "deny"}})
                if loc_id and state[bc].get("i"):
                    api("POST","inventory_levels/set.json",{"location_id":loc_id,"inventory_item_id":state[bc]["i"],"available":max(int(fnum(r.get('BESTAND'))),0)})
                st["updated"]+=1
            except Exception as e: st["errors"]+=1; log("ERR",bc,str(e)[:120])
        log("\n=== Update ==="); [log(f"{k:9}: {v}") for k,v in st.items()]; return

    groups=group_rows(rows); log(f"{len(groups)} Artikel-Gruppen (SUCHNUMMER).")

    if args.mode=="retag":
        st={"retagged":0,"miss":0,"errors":0}
        for key,group in groups:
            pid=None
            for r in group:
                if r.get("EAN","").strip() in state: pid=state[r.get("EAN","").strip()]["p"]; break
            if not pid: st["miss"]+=1; continue
            ptype,tags=categories(group[0])
            if args.dry_run: st["retagged"]+=1; continue
            try: api("PUT",f"products/{pid}.json",{"product":{"id":pid,"product_type":ptype,"tags":", ".join(tags)}}); st["retagged"]+=1
            except Exception as e: st["errors"]+=1; log("ERR",key,str(e)[:120])
            if st["retagged"]%100==0: log(f"... {st['retagged']} getaggt")
            if args.limit and st["retagged"]>=args.limit: break
        log("\n=== Retag ==="); [log(f"{k:9}: {v}") for k,v in st.items()]; return

    # full
    st={"created":0,"updated":0,"variants":0,"noimg":0,"drop":0,"errors":0}
    done=0
    for key,group in groups:
        exists=any(r.get("EAN","").strip() in state for r in group)
        q=qualifying(group)
        if not q:
            st["drop"]+=1; continue
        if args.dry_run:
            st["created" if not exists else "updated"]+=1; st["variants"]+=len(q); continue
        try:
            if exists:
                update_grouped(group,state,loc_id,True); st["updated"]+=1
            else:
                res,ids=create_grouped(group,loc_id)
                if res=="created":
                    for bc,vm in ids["variants"].items(): state[bc]={"p":ids["p"],"v":vm["v"],"i":vm["i"]}
                    st["created"]+=1; st["variants"]+=len(ids["variants"])
                else: st[res]+=1
        except Exception as e: st["errors"]+=1; log("ERR",key,str(e)[:140])
        done+=1
        if done%50==0: save_state(state); log(f"... {done}/{len(groups)} Gruppen (created {st['created']}, updated {st['updated']}, err {st['errors']})")
        if args.limit and (st["created"]+st["updated"])>=args.limit: break
    save_state(state)
    log("\n=== Full ==="); [log(f"{k:9}: {v}") for k,v in st.items()]

if __name__=="__main__":
    main()
