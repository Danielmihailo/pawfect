# pawfect – Zoodrop → Shopify Sync

Vollautomatischer Produkt-Sync von **Zoodrop** in den Shopify-Store **lovepawfect** – läuft komplett über GitHub Actions.

## Was es tut
- **Voll-Sync** (`--mode full`, montags + manuell): legt Produkte an / aktualisiert sie aus `zoodrop_utf8.csv`
  (Titel, Beschreibung, Hersteller→Vendor, Kategorie→Produkttyp/Tags, Bilder, **EAN→Barcode/GTIN**, **Bruttopreis**, Bestand).
  Nicht mehr verfügbare oder unrentable Artikel werden auf *Entwurf* gesetzt.
- **Update-Sync** (`--mode update`, täglich): aktualisiert nur **Preis & Bestand & Status** aus `update_utf8.csv` (schnell).

## Preisregel
`Brutto = Netto-EK × 1,7`, gedeckelt auf `UVP − 1 €`, nur wenn **Netto-Marge ≥ 2 €**, Charm-Pricing `x,99`.
Verfügbar = `Im Verkauf` **und** (Bestand > 0 **oder** Lagerstatus „Fullfillment"). Artikel ohne Bild werden übersprungen.

Anpassbar über Env: `PRICE_FACTOR`, `MIN_MARGIN`, `SKIP_NO_IMAGE`.

## Setup (einmalig)
1. **Secret setzen** (Repo → Settings → Secrets and variables → Actions):
   - `SHOPIFY_ACCESS_TOKEN` = der Admin-API-Token des Stores
2. **Erster Lauf**: Actions-Tab → „Zoodrop Sync" → *Run workflow* → Modus `full`.
3. Danach läuft es automatisch: täglich Update, montags Voll-Sync.

Der Stand (Zuordnung EAN → Shopify-IDs) wird in `sync_state.json` gespeichert und nach jedem Lauf zurück ins Repo committet.

## Lokal testen
```bash
SHOPIFY_SHOP=lovepawfect.myshopify.com \
SHOPIFY_ACCESS_TOKEN=*** \
python sync/sync.py --mode full --dry-run        # nur Statistik, kein Schreiben
python sync/sync.py --mode full --limit 5        # 5 Artikel live
```

Datenquelle: Zoodrop CSV-Feeds (öffentlich). Doku: https://www.zoodrop.de/csv/dokumentation/ZooDrop-CSV.pdf
