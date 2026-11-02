#!/usr/bin/env python3
"""
lovepawfect <- Zoodrop Sync  (Varianten gruppiert nach SUCHNUMMER)
  --mode full        : Gesamtfeed -> neue Produkte anlegen (inkl. Bilder) und alle bestehenden komplett pflegen:
                       Preis, Bestand, Status, Tags, Beschreibung (+ Futterangaben), Hersteller, Grundpreis
  --mode update      : Gesamtfeed -> nur Preis, Bestand und Verkaufbarkeit, nur Abweichungen (schnell);
                       danach Rechtstexte abgleichen (wie --mode rechtstexte)
  --mode gesperrt    : nur Produkte mit Tag lp-gesperrt neu prüfen (nach neuen GPSR-/Futterdaten)
  --mode rechtstexte : AGB, Widerrufsbelehrung, Datenschutz von der IT-Recht Kanzlei holen und in die Shopify-Rechtstexte
                       schreiben, Versandseite aus den Versandeinstellungen erzeugen (nur bei Änderung)
  --mode audit       : nur lesen: Shop-Einstellungen, Versand, Rechtstexte, Datenlage
  --mode retag       : nur Tags/Produkttyp
  --mode collections : Smart Collections (Welten + Kategorien) idempotent
  --mode wipe        : ALLE Produkte loeschen (Neuaufbau)
  --dry-run / --limit N / --local PATH

Gruppierung: alle Zeilen mit gleicher SUCHNUMMER = EIN Produkt; jede Zeile (EAN) = eine Variante.
Preisregel: Verkaufspreis = EK*FACTOR, gedeckelt auf UVP-0,01, Marge >= MIN_MARGIN, Charm x,99.
Betreiber ist Kleinunternehmer (§ 19 UStG): keine USt auf den Verkauf, aber auch kein Vorsteuerabzug.
Die Margenprüfung (Preis/(1+MwSt) - EK) entspricht Preis - EK brutto >= MIN_MARGIN*(1+MwSt), ist also vorsichtig genug.
Verkaufbar: Im Verkauf und (Bestand > 0 oder Fullfillment) und rentabel. Ausverkauft (Im Verkauf, Bestand 0)
bleibt aktiv mit Bestand 0; ausgelistet, unrentabel oder nicht mehr im Feed -> Bestand 0 und Entwurf.
Pflichtangaben: ohne belegte Herstellerangaben (GPSR, data/hersteller.json) oder ohne Futterangaben
(Beschreibung bzw. data/futter.json) bleibt ein Produkt Entwurf mit Tag lp-gesperrt.
"""
import os, sys, csv, io, re, json, time, base64, argparse, collections, html as htmlmod, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
csv.field_size_limit(10**8)

SHOP   = os.environ.get("SHOPIFY_SHOP", "lovepawfect.myshopify.com")
TOKEN  = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
APIV   = os.environ.get("SHOPIFY_API_VERSION", "2026-01")
FACTOR = float(os.environ.get("PRICE_FACTOR", "1.7"))
MIN_MARGIN = float(os.environ.get("MIN_MARGIN", "0.30"))
STATE_FILE = os.environ.get("STATE_FILE", "sync_state.json")
SKIP_NO_IMAGE = os.environ.get("SKIP_NO_IMAGE", "1") == "1"
FULL_URL   = "https://www.zoodrop.de/csv/download/zoodrop_utf8.csv"
DATA_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
SYNC_TAG, GESPERRT_TAG, AUS_TAG = "zoodrop", "lp-gesperrt", "lp-aus"
FOOD = {"Futter", "Trockenfutter", "Nassfutter", "Snacks", "Futterergänzung"}

def log(*a): print(*a, flush=True)

def load_data(name):
    p = os.path.join(DATA_DIR, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None
HERSTELLER = load_data("hersteller.json")   # Hersteller im Feed -> GPSR-Angaben ("__titel__": Titel -> Angaben)
FUTTER = load_data("futter.json")           # SUCHNUMMER -> Futterangaben

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
    if "halsbänder & leinen" in l1 or ("halsband" in l1 and "geschirr" in l1):
        if "geschirr" in l2: return ["Geschirre"]
        if "leine" in l2: return ["Leinen"]
        if "halsband" in l2: return ["Halsbänder"]
        return ["Leinen","Halsbänder","Geschirre"] if "geschirr" in l1 else ["Leinen","Halsbänder"]
    if l1 in L1_MAP: return list(L1_MAP[l1])
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

def load_rows(local):
    if local:
        return list(csv.DictReader(open(local, encoding="utf-8", errors="replace"), delimiter=";", quotechar='"'))
    log("Lade Feed:", FULL_URL)
    req=urllib.request.Request(FULL_URL, headers={"User-Agent":"lovepawfect-sync/1.0"})
    data=None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                data=r.read().decode("utf-8", errors="replace"); break
        except Exception as e:
            log("Feed-Download Fehler, retry", attempt+1, str(e)[:100]); time.sleep(5*(attempt+1))
    if data is None: raise RuntimeError("Feed-Download fehlgeschlagen")
    return list(csv.DictReader(io.StringIO(data), delimiter=";", quotechar='"'))

def gkey(r): return (r.get("SUCHNUMMER") or "").strip() or (r.get("ARTIKELNUMMER") or "").strip() or (r.get("EAN") or "").strip()

def group_rows(rows):
    groups={}; order=[]
    for r in rows:
        key=gkey(r)
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
    cap=uvp-0.01 if uvp and uvp>0 else None    # nie ÜBER UVP, aber nicht künstlich drücken
    if cap is not None and g>cap: g=cap
    if g<=ek: return None                       # niemals mit Verlust
    if (g/(1.0+vat/100.0))-ek < MIN_MARGIN: return None
    charm=float(int(g))+0.99
    if charm>ek and (cap is None or charm<=cap) and abs(charm-g)<=1.0: g=charm
    return round(g,2)

def row_price(r): return compute_gross(fnum(r.get("VERKAUFSPREIS")), fnum(r.get("UVP")), fnum(r.get("MWST") or "19"))

def variant_target(r):
    """-> (art, preis, bestand, policy); art: SELL verkaufbar, OOS ausverkauft (bleibt aktiv), DEAD weg"""
    if r is None: return ("DEAD", None, 0, "deny")
    g=row_price(r)
    if r.get("VERKAUFSSTATUS","").strip()!="Im Verkauf" or g is None: return ("DEAD", None, 0, "deny")
    qty=max(int(fnum(r.get("BESTAND"))),0)
    if r.get("LAGERSTATUS","").strip().lower()=="fullfillment": return ("SELL", g, qty, "continue")
    return ("SELL", g, qty, "deny") if qty>0 else ("OOS", g, 0, "deny")

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
    cats=["Einstreu" if (c=="Katzenklo" and "Katze" not in tiers) else c for c in cats]
    tags=[]
    for t in tiers+cats:
        if t and t not in tags: tags.append(t)
    vendor=vendor_of(row)
    if vendor and vendor not in tags: tags.append(vendor)
    return ptype, tags

# ---------- Pflichtangaben ----------
def hersteller_entry(row):
    if HERSTELLER is None: return None
    v=(row.get("HERSTELLER") or "").strip()
    if v: return HERSTELLER.get(v)
    return (HERSTELLER.get("__titel__") or {}).get((row.get("TITEL") or "").strip())

def vendor_of(row):
    v=(row.get("HERSTELLER") or "").strip()
    if v: return v
    e=hersteller_entry(row)
    return (e or {}).get("marke","") or ""

def gpsr_ok(row):
    if HERSTELLER is None: return True                 # Daten fehlen noch -> nichts sperren
    e=hersteller_entry(row)
    if not e or not e.get("sicher"): return False
    h=e.get("hersteller") or {}
    if not (h.get("name") and h.get("ort") and (h.get("email") or h.get("web"))): return False
    return bool(e.get("sitz_in_eu") or e.get("eu_verantwortlicher"))

def has_food_info(text):
    b=re.sub(r"<[^>]+>"," ",text or "").lower()
    return bool(re.search(r"zusammensetzung|zutaten", b)) and bool(re.search(r"analytische bestan?dteile|inhaltsstoffe|roh-?protein|roh-?fett|roh-?asche|roh-?faser", b))

def futter_entry(key):
    e=(FUTTER or {}).get(key)
    if e and e.get("status") in ("gefunden","teilweise") and (e.get("zusammensetzung") or "").strip() and (e.get("analytische_bestandteile") or "").strip(): return e
    return None

def food_ok(key, row, tags):
    if FUTTER is None or not (FOOD & set(tags)): return True
    if ((FUTTER or {}).get(key) or {}).get("status")=="kein_futter": return True
    return has_food_info(row.get("BESCHREIBUNG")) or futter_entry(key) is not None

def esc(s): return htmlmod.escape(s or "", quote=False)

def body_html(key, row):
    html=row.get("BESCHREIBUNG","") or ""
    e=None if has_food_info(html) else futter_entry(key)
    if e:
        parts=['<div class="lp-futterangaben">']
        if (e.get("futtermittelart") or "").strip(): parts.append(f'<p><strong>{esc(e["futtermittelart"].strip())}</strong></p>')
        for label,k in (("Zusammensetzung","zusammensetzung"),("Analytische Bestandteile","analytische_bestandteile"),
                        ("Zusatzstoffe","zusatzstoffe"),("Fütterungsempfehlung","fuetterungsempfehlung")):
            if (e.get(k) or "").strip(): parts.append(f'<h3>{label}</h3><p>{esc(e[k].strip())}</p>')
        parts.append('</div>'); html+="".join(parts)
    return html

def title_quantity(title, unit):
    """Menge aus dem Titel (z. B. '12 x 85 g', '1,6kg', '250 ml') in KG bzw. L"""
    t=title.lower().replace(",",".")
    m=re.search(r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(kg|g|ml|l)\b", t) or re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(kg|g|ml|ltr|l)\b", t)
    if not m: return None
    if len(m.groups())==3: n,v,u=float(m.group(1)),float(m.group(2)),m.group(3)
    else: n,v,u=1.0,float(m.group(1)),m.group(2)
    f={"kg":("KG",1),"g":("KG",0.001),"l":("L",1),"ltr":("L",1),"ml":("L",0.001)}[u]
    return n*v*f[1] if f[0]==unit else None

def unit_measure(row):
    """Füllmenge für den Grundpreis -> (Menge, 'KG'|'L') oder None; Zoodrops GRUNDPREIS bezieht sich auf die UVP"""
    u=(row.get("GRUNDPREIS_EINHEIT") or "").strip().lower().replace(" ","")
    m=fnum(row.get("GRUNDPREIS_MENGE")); gp=fnum(row.get("GRUNDPREIS")); uvp=fnum(row.get("UVP"))
    if u in ("kg","1kg"): unit,step="KG",1.0
    elif u in ("liter","l","1l"): unit,step="L",1.0
    elif u=="100g": unit,step="KG",0.1
    elif u=="100ml": unit,step="L",0.1
    else: return None
    if m<=0: return None
    q=m*step
    if gp>0 and uvp>0 and abs(gp*m-uvp)/uvp>0.05:      # Menge passt nicht zum Feed-Grundpreis -> Titel prüfen
        t=title_quantity(row.get("TITEL") or "", unit)
        if not t or abs(gp*(t/step)-uvp)/uvp>0.05: return None
        q=t
    return (round(q,4), unit)

def images(row):
    return [row[f"BILD{i}"].strip() for i in range(1,9) if row.get(f"BILD{i}","").strip()]

def group_images(rows):
    seen=set(); urls=[]
    for r in rows:
        for u in images(r):
            if u not in seen: seen.add(u); urls.append(u)
    return urls[:6]

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

# ---------- Shopify REST / GraphQL ----------
API_SEEN=[]
def api(method, path, body=None):
    url=f"https://{SHOP}/admin/api/{APIV}/{path}"
    data=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request(url, data=data, method=method, headers={"X-Shopify-Access-Token":TOKEN,"Content-Type":"application/json"})
    for attempt in range(7):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                if not API_SEEN: API_SEEN.append(r.headers.get("X-Shopify-API-Version")); log("API-Version ausgeliefert:", API_SEEN[0])
                used,cap=(int(x) for x in r.headers.get("X-Shopify-Shop-Api-Call-Limit","0/40").split("/"))
                if used>cap*0.7: time.sleep(0.6)
                txt=r.read().decode(); return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(2.0*(attempt+1)); continue
            if e.code>=500: time.sleep(1.5*(attempt+1)); continue
            log("HTTP",e.code,method,path,e.read().decode()[:300]); raise
        except Exception as e:
            if attempt>=6: log("NET",method,path,str(e)[:120]); raise
            time.sleep(2.0*(attempt+1)); continue
    raise RuntimeError(f"API failed: {method} {path}")

def gql(query, variables=None):
    url=f"https://{SHOP}/admin/api/{APIV}/graphql.json"
    body=json.dumps({"query":query,"variables":variables or {}}).encode()
    for attempt in range(7):
        req=urllib.request.Request(url, data=body, method="POST", headers={"X-Shopify-Access-Token":TOKEN,"Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d=json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code==429 or e.code>=500: time.sleep(2.0*(attempt+1)); continue
            log("GQL HTTP", e.code, e.read().decode()[:300]); raise
        except Exception as e:
            if attempt>=6: raise
            time.sleep(2.0*(attempt+1)); continue
        errs=d.get("errors") or []
        if any(((x.get("extensions") or {}).get("code")=="THROTTLED") for x in errs if isinstance(x,dict)): time.sleep(2.0*(attempt+1)); continue
        if errs: raise RuntimeError(json.dumps(errs, ensure_ascii=False)[:500])
        ts=((d.get("extensions") or {}).get("cost") or {}).get("throttleStatus") or {}
        if ts and ts.get("currentlyAvailable",1000)<150: time.sleep(1.5)
        return d.get("data") or {}
    raise RuntimeError("GraphQL failed")

UP_MUT="mutation($pid: ID!, $v: [ProductVariantsBulkInput!]!) { productVariantsBulkUpdate(productId: $pid, variants: $v) { userErrors { field message } } }"
def set_unit_prices(pid, items):
    """items: [(variant_id, (menge, einheit))] -> Shopify-Grundpreis (Bezug 1 kg bzw. 1 l)"""
    if not items: return 0
    v=[{"id":f"gid://shopify/ProductVariant/{vid}","showUnitPrice":True,
        "unitPriceMeasurement":{"quantityValue":q,"quantityUnit":u,"referenceValue":1,"referenceUnit":u}} for vid,(q,u) in items]
    d=gql(UP_MUT,{"pid":f"gid://shopify/Product/{pid}","v":v})
    errs=((d.get("productVariantsBulkUpdate") or {}).get("userErrors")) or []
    if errs: log("GRUNDPREIS FEHLER", pid, json.dumps(errs, ensure_ascii=False)[:300]); return 0
    return len(items)

def get_location_id():
    locs=api("GET","locations.json").get("locations",[])
    for l in locs:
        if l.get("active"): return l["id"]
    return locs[0]["id"] if locs else None

def load_state():
    try: return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception: return {}
def save_state(st): json.dump({k:{"p":v["p"],"v":v["v"],"i":v.get("i")} for k,v in st.items()}, open(STATE_FILE,"w",encoding="utf-8"), ensure_ascii=False)

def read_store():
    """-> state: EAN -> {p,v,i,price,qty,policy}; prods: id -> {status,tags,bcs}"""
    state={}; prods={}; since=0
    while True:
        data=api("GET", f"products.json?limit=250&since_id={since}&fields=id,status,tags,variants")
        page=data.get("products",[])
        if not page: break
        for p in page:
            since=max(since,p["id"])
            tags={t.strip() for t in (p.get("tags") or "").split(",") if t.strip()}
            prods[p["id"]]={"status":p.get("status"),"tags":tags,"bcs":[]}
            for v in p.get("variants",[]):
                bc=(v.get("barcode") or "").strip()
                if not bc or bc in state: continue
                state[bc]={"p":p["id"],"v":v["id"],"i":v.get("inventory_item_id"),"price":float(v.get("price") or 0),
                           "qty":int(v.get("inventory_quantity") or 0),"policy":v.get("inventory_policy")}
                prods[p["id"]]["bcs"].append(bc)
        log(f"  ...{len(prods)} vorhandene Produkte gelesen")
    log(f"Shop: {len(prods)} Produkte, {len(state)} Barcodes")
    return state, prods

# ---------- Abgleich ----------
def apply_variant(s, tgt, loc_id, st, dry):
    art,price,qty,policy=tgt
    body={}
    if price is not None and abs(price-s["price"])>0.005: body["price"]=f"{price:.2f}"
    if policy!=s["policy"]: body["inventory_policy"]=policy
    if body:
        st["preis_policy"]+=1
        if not dry: body["id"]=s["v"]; api("PUT",f"variants/{s['v']}.json",{"variant":body})
    if qty!=s["qty"] and loc_id and s.get("i"):
        st["bestand"]+=1
        if not dry: api("POST","inventory_levels/set.json",{"location_id":loc_id,"inventory_item_id":s["i"],"available":qty})

def managed(pinfo, known, feed):
    return SYNC_TAG in pinfo["tags"] or any(bc in known or bc in feed for bc in pinfo["bcs"])

def run_update(rows, dry):
    feed={(r.get("EAN") or "").strip(): r for r in rows if (r.get("EAN") or "").strip()}
    known=set(load_state().keys())
    loc_id=get_location_id(); state,prods=read_store()
    st=collections.Counter()
    for pid,pi in prods.items():
        if not managed(pi,known,feed): st["nicht_verwaltet"]+=1; continue
        arts=[]
        for bc in pi["bcs"]:
            tgt=variant_target(feed.get(bc)); arts.append(tgt[0])
            try: apply_variant(state[bc], tgt, loc_id, st, dry)
            except Exception as e: st["fehler"]+=1; log("ERR",bc,str(e)[:120])
        want=pi["status"]
        if arts and all(a=="DEAD" for a in arts): want="draft"
        elif pi["status"]=="draft" and any(a!="DEAD" for a in arts) and not ({GESPERRT_TAG,AUS_TAG} & pi["tags"]): want="active"
        if want!=pi["status"]:
            st["status_"+want]+=1
            if not dry: api("PUT",f"products/{pid}.json",{"product":{"id":pid,"status":want}})
    log("\n=== Update ==="); [log(f"{k:22}: {v}") for k,v in sorted(st.items())]

def product_tags(rep, compliant):
    ptype,tags=categories(rep)
    extra=[SYNC_TAG]+([] if compliant else [GESPERRT_TAG])
    return ptype, tags+[t for t in extra if t not in tags]

def create_grouped(key, group, compliant):
    q=[(r,row_price(r)) for r in group if is_available(r) and row_price(r) is not None][:100]
    if not q: return "drop", None, 0
    rows=[r for r,_ in q]
    imgs_urls=group_images(rows)
    if SKIP_NO_IMAGE and not imgs_urls: return "noimg", None, 0
    art0=((rows[0].get("ARTIKELNUMMER","") or "img").strip().replace(" ","-")) or "img"
    imgs=[]
    with ThreadPoolExecutor(max_workers=5) as ex:
        for idx,b in enumerate(ex.map(fetch_image, imgs_urls)):
            if b: imgs.append({"attachment":b,"filename":f"{art0}-{idx+1}.jpg"})
    if SKIP_NO_IMAGE and not imgs: return "noimg", None, 0
    rep=rows[0]; ptype,tags=product_tags(rep, compliant); multi=len(q)>1
    labels,ptitle=variant_labels([(r.get("TITEL") or "").strip() for r in rows])
    variants=[]
    for idx,(r,g) in enumerate(q):
        fulfill=r.get("LAGERSTATUS","").strip().lower()=="fullfillment"
        var={"price":f"{g:.2f}","sku":r.get("ARTIKELNUMMER","").strip(),"barcode":r.get("EAN","").strip(),
             "inventory_management":"shopify","inventory_policy":"continue" if fulfill else "deny","taxable":True,
             "inventory_quantity":max(int(fnum(r.get("BESTAND"))),0)}
        if multi: var["option1"]=labels[idx]
        variants.append(var)
    payload={"product":{"title":ptitle,"body_html":body_html(key,rep),"vendor":vendor_of(rep),
        "product_type":ptype,"tags":", ".join(tags),"status":"active" if compliant else "draft","images":imgs,"variants":variants}}
    if multi: payload["product"]["options"]=[{"name":"Variante"}]
    p=api("POST","products.json",payload)["product"]
    ids={}; units=[]
    by_bc={(r.get("EAN") or "").strip(): r for r in rows}
    for var in p["variants"]:
        bc=(var.get("barcode") or "").strip()
        ids[bc]={"p":p["id"],"v":var["id"],"i":var.get("inventory_item_id")}
        um=unit_measure(by_bc.get(bc) or {})
        if um: units.append((var["id"],um))
    return "created", ids, set_unit_prices(p["id"], units)

def run_full(rows, dry, limit, only_gated=False):
    """only_gated: nur Produkte mit Tag lp-gesperrt neu prüfen (nach neuen GPSR-/Futterdaten), nichts anlegen"""
    feed={(r.get("EAN") or "").strip(): r for r in rows if (r.get("EAN") or "").strip()}
    known=set(load_state().keys())
    loc_id=None if dry else get_location_id()
    state,prods=({},{}) if dry else read_store()
    st=collections.Counter(); seen_pids=set(); done=0
    groups=group_rows(rows); log(f"{len(groups)} Artikel-Gruppen (SUCHNUMMER).")
    for key,group in groups:
        pid=next((state[(r.get('EAN') or '').strip()]["p"] for r in group if (r.get('EAN') or '').strip() in state), None)
        if only_gated and not (pid and GESPERRT_TAG in prods[pid]["tags"]): continue
        live=[r for r in group if row_price(r) is not None and r.get("VERKAUFSSTATUS","").strip()=="Im Verkauf"]
        rep=(live or group)[0]
        _,tags0=categories(rep)
        ok_gpsr=gpsr_ok(rep); ok_food=food_ok(key,rep,tags0); compliant=ok_gpsr and ok_food
        try:
            if pid:
                seen_pids.add(pid); pi=prods[pid]; arts=[]; units=[]
                for bc in pi["bcs"]:
                    r=feed.get(bc); tgt=variant_target(r); arts.append(tgt[0])
                    if not dry: apply_variant(state[bc], tgt, loc_id, st, dry)
                    um=unit_measure(r) if r else None
                    if um: units.append((state[bc]["v"],um))
                alive=any(a!="DEAD" for a in arts)
                want="active" if (alive and compliant and AUS_TAG not in pi["tags"]) else "draft"
                ptype,tags=product_tags(rep, compliant)
                if AUS_TAG in pi["tags"]: tags.append(AUS_TAG)
                st["bestehend_"+want]+=1
                if not compliant: st["gesperrt_gpsr" if not ok_gpsr else "gesperrt_futter"]+=1
                if not dry:
                    api("PUT",f"products/{pid}.json",{"product":{"id":pid,"status":want,"tags":", ".join(tags),"product_type":ptype,
                        "body_html":body_html(key,rep),"vendor":vendor_of(rep) or None}})
                    st["grundpreis"]+=set_unit_prices(pid, units)
            else:
                if not any(is_available(r) and row_price(r) is not None for r in group): st["nicht_verkaufbar"]+=1; continue
                gate=None if compliant else ("neu_gesperrt_gpsr" if not ok_gpsr else "neu_gesperrt_futter")
                if dry:
                    st["neu"]+=1
                    if gate: st[gate]+=1
                    continue
                res,ids,nup=create_grouped(key, group, compliant)
                if res=="created":
                    for bc,vm in ids.items(): state[bc]=vm
                    st["neu"]+=1; st["neu_varianten"]+=len(ids); st["grundpreis"]+=nup
                    if gate: st[gate]+=1
                else: st[res]+=1
        except Exception as e: st["fehler"]+=1; log("ERR",key,str(e)[:160])
        done+=1
        if done%100==0:
            if not dry: save_state(state)
            log(f"... {done}/{len(groups)} Gruppen {dict(st)}")
        if limit and (st["neu"]+sum(v for k,v in st.items() if k.startswith("bestehend_")))>=limit: break
    # Produkte, deren Artikel ganz aus dem Feed verschwunden sind
    if not limit and not only_gated:
        for pid,pi in prods.items():
            if pid in seen_pids or not managed(pi,known,feed): continue
            for bc in pi["bcs"]:
                try: apply_variant(state[bc], variant_target(None), loc_id, st, dry)
                except Exception as e: st["fehler"]+=1; log("ERR",bc,str(e)[:120])
            if pi["status"]!="draft":
                st["verschwunden_entwurf"]+=1
                if not dry: api("PUT",f"products/{pid}.json",{"product":{"id":pid,"status":"draft"}})
    if not dry: save_state(state)
    log("\n=== Gesperrte neu geprüft ===" if only_gated else "\n=== Full ==="); [log(f"{k:22}: {v}") for k,v in sorted(st.items())]
    if HERSTELLER is None: log("HINWEIS: data/hersteller.json fehlt – GPSR-Sperre inaktiv")
    if FUTTER is None: log("HINWEIS: data/futter.json fehlt – Futter-Sperre inaktiv")

# ---------- Collections ----------
def find_collection(handle):
    sc=api("GET",f"smart_collections.json?handle={handle}").get("smart_collections",[])
    return sc[0] if sc else None
def upsert_collection(handle,title,conds,published=True):
    rules=[{"column":"tag","relation":"equals","condition":c} for c in conds]
    body={"smart_collection":{"title":title,"handle":handle,"disjunctive":False,"rules":rules,"published":published}}
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

def make_pages():
    """Shop-Seiten, die das Theme braucht (Widerrufsfunktion nach § 356a BGB)"""
    for pg in [{"handle":"widerruf","title":"Vertrag widerrufen","template_suffix":"widerruf","body_html":""}]:
        ex=api("GET",f"pages.json?handle={pg['handle']}").get("pages",[])
        body={"page":dict(pg, published=True)}
        if ex:
            body["page"]["id"]=ex[0]["id"]; api("PUT",f"pages/{ex[0]['id']}.json",body); log("Seite aktualisiert:",pg["handle"])
        else:
            api("POST","pages.json",body); log("Seite angelegt:",pg["handle"])

# ---------- Rechtstexte ----------
# Hosting-Links der IT-Recht Kanzlei liefern immer die aktuelle Fassung; Änderungen der Kanzlei kommen so mit dem nächsten Lauf in den Shop.
ITRK = {"TERMS_OF_SERVICE": "https://itrk.legal/1DXw.2Y.1dk7-iframe.html",   # AGB mit Kundeninformationen
        "REFUND_POLICY":    "https://itrk.legal/1DXw.2J.1dk7-iframe.html",   # Widerrufsbelehrung & Widerrufsformular
        "PRIVACY_POLICY":   "https://itrk.legal/1DXw.8U.1dk7-iframe.html"}   # Datenschutzerklärung
LAND = {"DE":"Deutschland","AT":"Österreich","BE":"Belgien","DK":"Dänemark","LU":"Luxemburg","NL":"Niederlande"}
# Lieferzeit ab Bestellung in Werktagen: (Lagerware, Ware aus dem Zoodrop-Fulfillment). DE wie im Theme (ship_days_min/max, +2/+3).
LIEFERZEIT = {"DE": ("2–4", "4–7"), "AUSLAND": ("3–6", "5–9")}
POLICY_Q = "{ shop { shopPolicies { type body url } } }"
POLICY_MUT = "mutation($p: ShopPolicyInput!) { shopPolicyUpdate(shopPolicy: $p) { shopPolicy { type url } userErrors { field message } } }"

def policy_plain(h):
    """Vergleichsform: nur der Text, ohne Abruf-Zeitstempel und Leerraum (Shopify formt das HTML beim Speichern um)"""
    t = htmlmod.unescape(re.sub(r"<[^>]+>", "", h or ""))
    t = re.sub(r"Stand:\s*\d{1,2}\.\d{1,2}\.\d{4},?\s*\d{1,2}:\d{2}(:\d{2})?", "", t)
    return re.sub(r"\s+", "", t)

def itrk_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "lovepawfect-sync (Rechtstexte)"})
    with urllib.request.urlopen(req, timeout=60) as r: page = r.read().decode("utf-8", "replace")
    m = re.search(r"(?is)<body[^>]*>(.*)</body>", page)
    body = re.sub(r"(?is)<(script|style|iframe)\b.*?</\1>", "", m.group(1) if m else page).strip()
    if len(policy_plain(body)) < 1000: raise RuntimeError(f"Rechtstext leer oder unvollständig: {url}")
    return body

def eur(a): return f"{a:.2f}".replace(".", ",") + " €"

def versand_html():
    """Versand & Zahlung aus den echten Versandeinstellungen (Standardprofil): Länder, Kosten, Gratisgrenze, Lieferzeit"""
    zonen = []
    for prof in gql(DELIVERY_Q)["deliveryProfiles"]["nodes"]:
        if not prof.get("default"): continue
        for g in prof.get("profileLocationGroups") or []:
            for z in g["locationGroupZones"]["nodes"]:
                cc = sorted((c["code"].get("countryCode") for c in z["zone"]["countries"] if c["code"].get("countryCode")), key=lambda c: LAND.get(c, c))
                preise, frei = [], None
                for m in z["methodDefinitions"]["nodes"]:
                    if not m.get("active"): continue
                    p = float((((m.get("rateProvider") or {}).get("price")) or {}).get("amount") or 0)
                    ab = [float((c.get("conditionCriteria") or {}).get("amount") or 0) for c in m.get("methodConditions") or []
                          if c.get("field") == "TOTAL_PRICE" and c.get("operator") == "GREATER_THAN_OR_EQUAL_TO"]
                    if p == 0 and ab: frei = min(ab + ([frei] if frei is not None else []))
                    else: preise.append(p)
                if cc and (preise or frei is not None): zonen.append((cc, min(preise) if preise else 0.0, frei))
    fremd = sorted({c for cc, _, _ in zonen for c in cc} - set(LAND))
    if fremd or not zonen: raise RuntimeError(f"Versandzonen nicht auswertbar (Länder ohne Namen: {fremd}) – LAND ergänzen")
    zonen.sort(key=lambda z: (z[0] != ["DE"], z[1]))
    zeilen = []
    for cc, preis, frei in zonen:
        kosten = eur(preis) if preis else "kostenlos"
        if frei is not None and preis: kosten += f", ab {eur(frei)} Bestellwert versandkostenfrei"
        lager, lieferant = LIEFERZEIT["DE" if cc == ["DE"] else "AUSLAND"]
        zeilen.append(f"<tr><td>{', '.join(LAND[c] for c in cc)}</td><td>{kosten}</td><td>{lager} Werktage<br>Lieferantenware: {lieferant} Werktage</td></tr>")
    alle = sorted({c for cc, _, _ in zonen for c in cc}, key=lambda c: (c != "DE", LAND[c]))
    return ("<h1>Versand &amp; Zahlung</h1>"
            "<h2>Liefergebiet</h2>"
            f"<p>Wir liefern in diese Länder: {', '.join(LAND[c] for c in alle)}. In andere Länder liefern wir nicht.</p>"
            "<h2>Versandkosten und Lieferzeit</h2>"
            "<table><thead><tr><th>Lieferland</th><th>Versandkosten</th><th>Lieferzeit ab Bestellung</th></tr></thead>"
            f"<tbody>{''.join(zeilen)}</tbody></table>"
            "<p>Die erste Lieferzeit gilt für Artikel, die bei uns auf Lager sind. Artikel, die wir erst bei unserem Lieferanten "
            "abrufen, sind auf der Produktseite mit „versandfertig in 3–5 Werktagen“ gekennzeichnet; für sie gilt die zweite Lieferzeit. "
            "Wir versenden mit DPD oder DHL.</p>"
            "<p>Alle Preise sind Endpreise zuzüglich der genannten Versandkosten. Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.</p>"
            "<h2>Zahlungsarten</h2>"
            "<p>Die akzeptierten Zahlungsarten siehst du unten auf jeder Seite und vor dem Bezahlen im Checkout.</p>")

def set_rechtstexte(dry=False):
    try:
        sc = {s["handle"] for s in gql("{ currentAppInstallation { accessScopes { handle } } }")["currentAppInstallation"]["accessScopes"]}
        if "write_legal_policies" not in sc: log("RECHTSTEXTE: der App fehlt das Recht write_legal_policies (vorhanden:", ", ".join(sorted(sc)), ")")
    except Exception as e: log("RECHTSTEXTE: Rechte nicht lesbar:", str(e)[:200])
    ist = {p["type"]: p for p in gql(POLICY_Q)["shop"]["shopPolicies"]}
    soll = {t: itrk_text(u) for t, u in ITRK.items()}
    soll["SHIPPING_POLICY"] = versand_html()
    for t, body in soll.items():
        if policy_plain(body) == policy_plain((ist.get(t) or {}).get("body")): log("RECHTSTEXTE unverändert:", t); continue
        if dry: log("RECHTSTEXTE [DRY] würde setzen:", t, len(body), "Zeichen"); continue
        r = gql(POLICY_MUT, {"p": {"type": t, "body": body}})["shopPolicyUpdate"]
        if r["userErrors"]: raise RuntimeError(f"{t}: {json.dumps(r['userErrors'], ensure_ascii=False)}")
        log("RECHTSTEXTE gesetzt:", t, len(body), "Zeichen ->", (r.get("shopPolicy") or {}).get("url"))
    for p in gql(POLICY_Q)["shop"]["shopPolicies"]: log("RECHTSTEXTE jetzt:", p["type"], len(p.get("body") or ""), "Zeichen", p.get("url"))

VERSAND_Q="""{ deliveryProfiles(first: 5) { nodes { id name default profileLocationGroups { locationGroup { id } locationGroupZones(first: 30) { nodes {
  zone { id name countries { code { countryCode restOfWorld } } }
  methodDefinitions(first: 30) { nodes { id name active rateProvider { ... on DeliveryRateDefinition { price { amount } } } } } } } } } } }"""
PROFILE_MUT="mutation($id: ID!, $p: DeliveryProfileInput!) { deliveryProfileUpdate(id: $id, profile: $p) { profile { id } userErrors { field message } } }"
NACHBARLAENDER=["AT","BE","LU","NL","DK"]   # Zoodrop liefert nur nach DE + diese Länder

def set_versand():
    """Versand nur in Zoodrop-Länder: DE bleibt (ohne Express), EU-/Weltzonen raus, neue Zone AT/BE/LU/NL/DK"""
    prof=next(p for p in gql(VERSAND_Q)["deliveryProfiles"]["nodes"] if p.get("default"))
    zones_del=[]; methods_del=[]; lg_id=None; price=None
    for g in prof["profileLocationGroups"]:
        lg_id=g["locationGroup"]["id"]
        for z in g["locationGroupZones"]["nodes"]:
            cc={(c["code"].get("countryCode") or "REST") for c in z["zone"]["countries"]}
            meths=z["methodDefinitions"]["nodes"]
            if "DE" in cc:
                if cc-{"DE"}: raise RuntimeError(f"DE-Zone enthält weitere Länder: {sorted(cc)} – bitte von Hand prüfen")
                for m in meths:
                    if "express" in (m.get("name") or "").lower(): methods_del.append(m["id"]); log("VERSAND entferne Methode", m["name"], "aus", z["zone"]["name"])
            else:
                if cc & set(NACHBARLAENDER) and meths and price is None:
                    price=((meths[0].get("rateProvider") or {}).get("price") or {}).get("amount")
                zones_del.append(z["zone"]["id"]); log("VERSAND entferne Zone", z["zone"]["name"], ",".join(sorted(cc)))
    if zones_del or methods_del:
        r=gql(PROFILE_MUT,{"id":prof["id"],"p":{"zonesToDelete":zones_del,"methodDefinitionsToDelete":methods_del}})["deliveryProfileUpdate"]
        if r["userErrors"]: raise RuntimeError(json.dumps(r["userErrors"], ensure_ascii=False))
    zone={"name":"Nachbarländer (Zoodrop)","countries":[{"code":c,"includeAllProvinces":True} for c in NACHBARLAENDER],
          "methodDefinitionsToCreate":[{"name":"Standard","active":True,"rateDefinition":{"price":{"amount":float(price or 13.99),"currencyCode":"EUR"}}}]}
    r=gql(PROFILE_MUT,{"id":prof["id"],"p":{"locationGroupsToUpdate":[{"id":lg_id,"zonesToCreate":[zone]}]}})["deliveryProfileUpdate"]
    if r["userErrors"]: raise RuntimeError(json.dumps(r["userErrors"], ensure_ascii=False))
    log("VERSAND neue Zone", ",".join(NACHBARLAENDER), "Standard", price or 13.99, "EUR")
    for g in gql(VERSAND_Q)["deliveryProfiles"]["nodes"]:
        for lg in g["profileLocationGroups"]:
            for z in lg["locationGroupZones"]["nodes"]:
                for m in z["methodDefinitions"]["nodes"]:
                    log("VERSAND jetzt", g["name"], "|", z["zone"]["name"], ",".join(sorted((c["code"].get("countryCode") or "REST") for c in z["zone"]["countries"])),
                        "|", m["name"], (m.get("rateProvider") or {}).get("price"))

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

# ---------- Audit (nur lesend) ----------
DELIVERY_Q="""{ deliveryProfiles(first: 5) { nodes { name default profileLocationGroups { locationGroupZones(first: 30) { nodes {
  zone { name countries { code { countryCode restOfWorld } } }
  methodDefinitions(first: 30) { nodes { name active
    rateProvider { ... on DeliveryRateDefinition { price { amount currencyCode } } ... on DeliveryParticipant { id } }
    methodConditions { field operator conditionCriteria { __typename ... on MoneyV2 { amount currencyCode } ... on Weight { value unit } } } } } } } } } } }"""
def audit(rows):
    shop=api("GET","shop.json").get("shop",{})
    keys=["name","domain","plan_name","plan_display_name","password_enabled","taxes_included","tax_shipping","county_taxes",
          "country_code","currency","primary_locale","setup_required","eligible_for_payments","email","customer_email"]
    log("AUDIT shop", json.dumps({k:shop.get(k) for k in keys}, ensure_ascii=False))
    try:
        d=gql(DELIVERY_Q)
        for prof in (d.get("deliveryProfiles") or {}).get("nodes",[]):
            for g in prof.get("profileLocationGroups") or []:
                for z in ((g.get("locationGroupZones") or {}).get("nodes") or []):
                    cc=",".join((c.get("code") or {}).get("countryCode") or "REST" for c in (z.get("zone") or {}).get("countries",[]))
                    for m in ((z.get("methodDefinitions") or {}).get("nodes") or []):
                        conds=[f'{c.get("field")} {c.get("operator")} {(c.get("conditionCriteria") or {}).get("amount") or (c.get("conditionCriteria") or {}).get("value")}' for c in m.get("methodConditions") or []]
                        log("AUDIT versand", prof.get("name"), "|", (z.get("zone") or {}).get("name"), cc, "|", m.get("name"), "aktiv" if m.get("active") else "inaktiv",
                            "|", json.dumps((m.get("rateProvider") or {}).get("price")), "|", "; ".join(conds))
    except Exception as e: log("AUDIT versand FEHLER", str(e)[:300])
    try:
        vid=next((v["v"] for v in read_store()[0].values() if v["qty"]>0), None)
        d=gql("""mutation($i: DraftOrderInput!) { draftOrderCalculate(input: $i) { calculatedDraftOrder {
                 totalTaxSet { shopMoney { amount } } taxesIncluded taxLines { title rate priceSet { shopMoney { amount } } } }
                 userErrors { field message } } }""",
              {"i":{"lineItems":[{"variantId":f"gid://shopify/ProductVariant/{vid}","quantity":1}],
                    "shippingAddress":{"address1":"Teststrasse 1","city":"Berlin","zip":"10115","countryCode":"DE"}}})
        log("AUDIT steuer", json.dumps(d.get("draftOrderCalculate"), ensure_ascii=False)[:600])
    except Exception as e: log("AUDIT steuer FEHLER", str(e)[:300])
    try:
        pol=api("GET","policies.json").get("policies",[])
        for p in pol: log("AUDIT rechtstext", p.get("title"), "| Zeichen:", len(p.get("body") or ""), "|", p.get("url"))
        if not pol: log("AUDIT rechtstexte KEINE")
    except Exception as e: log("AUDIT rechtstexte FEHLER", str(e)[:200])
    feed={(r.get("EAN") or "").strip(): r for r in rows if (r.get("EAN") or "").strip()}
    state,prods=read_store()
    st=collections.Counter()
    for pid,pi in prods.items():
        st["produkte_"+str(pi["status"])]+=1
        if GESPERRT_TAG in pi["tags"]: st["produkte_gesperrt"]+=1
        for bc in pi["bcs"]:
            s=state[bc]; t=variant_target(feed.get(bc)); kaufbar=s["qty"]>0 or s["policy"]=="continue"
            st["var_"+t[0]]+=1
            if t[0]=="DEAD" and kaufbar: st["var_DEAD_aber_kaufbar"]+=1
            if t[1] is not None and abs(t[1]-s["price"])>0.005: st["var_preis_abweichend"]+=1
            if t[2]!=s["qty"]: st["var_bestand_abweichend"]+=1
    for k in sorted(st): log("AUDIT produkte", k, st[k])

# ---------- Main ----------
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode",choices=["full","update","gesperrt","retag","collections","wipe","audit","seiten","versand","rechtstexte"],default="full")
    ap.add_argument("--dry-run",action="store_true"); ap.add_argument("--limit",type=int,default=0); ap.add_argument("--local",default="")
    args=ap.parse_args()
    if args.mode=="collections":
        if args.dry_run:
            for w,s in WORLD_SLUG.items():
                log(WORLD_HANDLE[w],"->",w)
                for c in WORLD_CATS.get(w,[]): log(f"  {s}-{CAT_HANDLE[c]} -> {w}+{c}")
            return
        make_collections(); return
    if args.mode=="seiten": make_pages(); return
    if args.mode=="versand": set_versand(); return
    if args.mode=="rechtstexte": set_rechtstexte(args.dry_run); return
    if args.mode=="wipe":
        if args.dry_run: log("[DRY-RUN] wuerde alle Produkte loeschen."); return
        wipe_all(); return
    rows=load_rows(args.local); log(f"{len(rows)} Zeilen geladen.")
    if args.mode=="audit": audit(rows); return
    if args.mode=="update":
        run_update(rows, args.dry_run)
        try: set_rechtstexte(args.dry_run)
        except Exception as e: log("RECHTSTEXTE FEHLER (Sync selbst ist durchgelaufen):", str(e)[:400])
        return
    if args.mode=="gesperrt": run_full(rows, args.dry_run, args.limit, only_gated=True); return
    if args.mode=="retag":
        state,prods=read_store(); st=collections.Counter()
        for key,group in group_rows(rows):
            pid=next((state[(r.get('EAN') or '').strip()]["p"] for r in group if (r.get('EAN') or '').strip() in state), None)
            if not pid: st["miss"]+=1; continue
            ptype,tags=categories(group[0])
            if args.dry_run: st["retagged"]+=1; continue
            try: api("PUT",f"products/{pid}.json",{"product":{"id":pid,"product_type":ptype,"tags":", ".join(tags)}}); st["retagged"]+=1
            except Exception as e: st["errors"]+=1; log("ERR",key,str(e)[:120])
        log("\n=== Retag ==="); [log(f"{k:9}: {v}") for k,v in st.items()]; return
    run_full(rows, args.dry_run, args.limit)

if __name__=="__main__":
    main()
