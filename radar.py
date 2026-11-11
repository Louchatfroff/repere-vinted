"""Repère: local Vinted monitoring with optional RAM-specific profiles."""
import argparse
import copy
import csv
import io
import json
import math
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from connection import RemoteProvider, Connection, ProviderError

ROOT = Path(__file__).resolve().parent
DEFAULT_PROFILE = {
    "id": "ddr5", "name": "SO-DIMM DDR5", "enabled": True,
    "kind": "ram", "required_all": [], "required_any": [],
    "catalog_ids": [], "brand_ids": [], "size_ids": [], "status_ids": [],
    "queries": ["sodimm ddr5", "so dimm ddr5", "ram portable ddr5"],
    "generations": [5], "require_sodimm": True, "allow_unknown_capacity": True,
    "capacities": [8, 16, 24, 32, 48, 64, 96], "min_price": 1, "max_price": 100,
    "max_per_gb": 8, "min_speed": 0,
    "exclude": ["hs", "defectueux", "pour pieces", "adaptateur", "ddr3", "rdimm", "udimm"],
    "brands": [], "min_rating": 0, "min_reviews": 0,
    "shipping": 5, "buyer_fee_fixed": 0.7, "buyer_fee_percent": 5,
    "conversion_cost": 0, "resale_target": 250, "resale_capacity": 16,
    "selling_cost": 0, "min_margin": 100, "margin_only": False,
}
GENERIC_PROFILE = dict(copy.deepcopy(DEFAULT_PROFILE), id="general", name="Nouvelle recherche",
    kind="general", enabled=False, queries=[""], generations=[], require_sodimm=False,
    capacities=[], max_per_gb=0, max_price=200, exclude=["defectueux", "pour pieces"],
    resale_target=0, resale_capacity=0, min_margin=0)
DEFAULT_APPEARANCE = {"theme": "light", "accent": "copper", "density": "comfortable", "layout": "list", "show_margin": True, "show_score": False}
DEFAULT_CONFIG = {"interval_seconds": 60, "per_page": 48, "pages": 1, "notify_initial": False, "appearance": DEFAULT_APPEARANCE, "profiles": [DEFAULT_PROFILE,
    dict(copy.deepcopy(DEFAULT_PROFILE), id="ddr4", name="SO-DIMM DDR4", enabled=False,
         generations=[4], queries=["sodimm ddr4", "ram portable ddr4"], max_price=40,
         max_per_gb=3, resale_target=0, min_margin=0)]}


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFKD", str(text)).lower()
                   if not unicodedata.combining(c)).replace("×", "x")


def number(value, default=None):
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        result = float(str(value).replace(",", "."))
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def specs(title, description=""):
    text = normalize(title + " " + description)
    generations = set(int(x) for x in re.findall(r"\b(?:ddr|pc)\s*([345])\b", text))
    kits = set((int(a), int(b)) for a, b in re.findall(r"\b(\d{1,2})\s*x\s*(\d{1,3})\s*g[bo]\b", text))
    kits.update((int(a), int(b)) for a, b in re.findall(r"\b(\d{1,2})\s*barrettes?\s*(?:de\s*)?(\d{1,3})\s*g[bo]\b", text))
    kits.update((int(b), int(a)) for a, b in re.findall(r"\b(\d{1,3})\s*g[bo]\s*x\s*(\d{1,2})\b", text))
    kit = next(iter(kits)) if len(kits) == 1 else None
    capacities = set(int(x) for x in re.findall(r"\b(\d{1,3})\s*g[bo]\b", text))
    count = kit[0] if kit else None
    capacity = kit[0] * kit[1] if kit else (next(iter(capacities)) if len(capacities) == 1 else None)
    ambiguous_capacity = len(kits) > 1 or (bool(kit) and bool(capacities - {kit[1], capacity})) or (not kit and len(capacities) > 1)
    speed = re.search(r"\b(\d{4,5})\s*(?:mhz|mt/?s)\b|\bddr[345][ -]+(\d{4,5})\b", text)
    return {"generation": next(iter(generations)) if len(generations) == 1 else None,
            "sodimm": bool(re.search(r"\bso[\s-]?dimm\b|\bsodim\b|\bram\s+(?:de\s+)?portable\b|\blaptop\s+(?:ram|memory)\b", text)),
            "capacity": capacity, "count": count,
            "speed": int(speed[1] or speed[2]) if speed else None,
            "ambiguous": len(generations) > 1 or ambiguous_capacity}


def canonical_item(raw):
    if not isinstance(raw, dict):
        raise ValueError("Chaque annonce doit être un objet JSON.")
    item_id = str(raw.get("id", ""))
    title = str(raw.get("title", "")).strip()[:500]
    price = number(raw.get("price"))
    if not re.fullmatch(r"[\w-]{1,100}", item_id) or not title or price is None or price < 0:
        raise ValueError("Annonce invalide : id, title et price positif requis.")
    currency = raw.get("currency") or (raw.get("price", {}).get("currency_code") if isinstance(raw.get("price"), dict) else None) or "EUR"
    url = urllib.parse.urljoin("https://www.vinted.fr", str(raw.get("url") or "/items/" + item_id))
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"vinted.fr", "www.vinted.fr"} or parsed.username:
        raise ValueError("Lien d'annonce : HTTPS sur vinted.fr uniquement.")
    seller = raw.get("user") or {}
    if not isinstance(seller, dict):
        seller = {}
    box = raw.get("item_box") if isinstance(raw.get("item_box"), dict) else {}
    brand = raw.get("brand_title") or raw.get("brand") or ""
    if not brand and isinstance(box.get("first_line"), str) and box["first_line"] != title:
        brand = box["first_line"]
    photo = raw.get("photo") if isinstance(raw.get("photo"), dict) else {}
    image = str(photo.get("url") or raw.get("image") or "")
    image_url = urllib.parse.urlparse(image)
    if image_url.scheme != "https" or not (image_url.hostname or "").endswith(".vinted.net") or image_url.username:
        image = ""
    return {"id": item_id, "title": title, "description": str(raw.get("description", ""))[:15000],
            "profile_ids": [str(x) for x in raw.get("profile_ids", [])][:20] if isinstance(raw.get("profile_ids", []), list) else [],
            "price": round(price, 2), "currency": currency, "url": url,
            "brand": str(brand), "image": image,
            "rating": number(raw.get("rating")), "reviews": number(raw.get("reviews")),
            "seller": str(seller.get("login") or raw.get("seller") or ""),
            "sold": bool(raw.get("sold") or raw.get("is_sold") or raw.get("is_reserved"))}


def evaluate(item, profile):
    profile = dict(DEFAULT_PROFILE, **profile)
    ram = profile["kind"] == "ram"
    s = specs(item["title"], item["description"])
    text = normalize(item["title"] + " " + item["description"])
    reasons = []
    if item.get("profile_ids") and profile["id"] not in item["profile_ids"]: reasons.append("Issue d'une autre recherche")
    if item["sold"]: reasons.append("Vendue ou réservée")
    if item["currency"] != "EUR": reasons.append("Devise différente de l'euro")
    if not profile["min_price"] <= item["price"] <= profile["max_price"]: reasons.append("Prix hors limite")
    if ram and profile["generations"] and s["generation"] not in profile["generations"]: reasons.append("Génération absente ou différente")
    if ram and profile["require_sodimm"] and not s["sodimm"]: reasons.append("Format SO-DIMM non confirmé")
    contains = lambda word: bool(re.search(r"(?<!\w)" + re.escape(normalize(word)) + r"(?!\w)", text))
    if not ram and not item.get("profile_ids"):
        clauses = [re.findall(r"\w+", normalize(query)) for query in profile["queries"]]
        if not any(words and all(contains(word) for word in words) for words in clauses):
            reasons.append("Le texte importé ne correspond pas aux recherches")
    if any(not contains(word) for word in profile["required_all"]): reasons.append("Un mot obligatoire est absent")
    if profile["required_any"] and not any(contains(word) for word in profile["required_any"]): reasons.append("Aucun des mots souhaités")
    for word in profile["exclude"]:
        if re.search(r"(?<!\w)" + re.escape(normalize(word)) + r"(?!\w)", text):
            reasons.append("Exclusion : " + word)
    if ram and s["ambiguous"]: reasons.append("Caractéristiques ambiguës")
    if ram and s["capacity"] is None and not profile["allow_unknown_capacity"]: reasons.append("Capacité inconnue")
    if ram and s["capacity"] is not None and profile["capacities"] and s["capacity"] not in profile["capacities"]: reasons.append("Capacité hors sélection")
    per_gb = round(item["price"] / s["capacity"], 2) if ram and s["capacity"] else None
    if per_gb is not None and profile["max_per_gb"] and per_gb > profile["max_per_gb"]: reasons.append("Prix par Go trop élevé")
    if ram and profile["min_speed"] and (s["speed"] or 0) < profile["min_speed"]: reasons.append("Fréquence insuffisante ou inconnue")
    if profile["brands"] and not any(normalize(b) in normalize(item["brand"] + " " + text) for b in profile["brands"]): reasons.append("Marque hors sélection")
    for key, field in [("min_rating", "rating"), ("min_reviews", "reviews")]:
        if profile[key] and (item[field] is None or item[field] < profile[key]): reasons.append("Informations vendeur insuffisantes : " + field)
    landed = round(item["price"] * (1 + profile["buyer_fee_percent"] / 100) + profile["buyer_fee_fixed"] + profile["shipping"] + profile["conversion_cost"], 2)
    landed_per_gb = round(landed / s["capacity"], 2) if ram and s["capacity"] else None
    margin = round(profile["resale_target"] - landed - profile["selling_cost"], 2) if profile["resale_target"] > 0 and (not ram or s["capacity"] == profile["resale_capacity"]) else None
    if profile["margin_only"] and (margin is None or margin < profile["min_margin"]): reasons.append("Marge cible non atteinte ou inconnue")
    score = min(100, round(35 + (15 if ram and s["capacity"] else 0) + (10 if ram and s["speed"] else 0) +
                  (max(0, 25 * (1 - landed_per_gb / profile["max_per_gb"])) if per_gb is not None and profile["max_per_gb"] else 0) +
                  (15 if margin is not None and margin >= profile["min_margin"] else 0)))
    if not ram:
        score = round(50 + max(0, 35 * (1 - item["price"] / max(1, profile["max_price"]))) + (15 if margin is not None and margin >= profile["min_margin"] else 0))
    return {"matched": not reasons, "reasons": reasons, "kind": profile["kind"], "specs": s if ram else {}, "per_gb": per_gb, "landed": landed,
            "landed_per_gb": landed_per_gb, "margin": margin, "score": score, "profile_id": profile["id"], "profile_name": profile["name"]}


def validate_config(config):
    if not isinstance(config, dict): raise ValueError("Configuration invalide.")
    interval = number(config.get("interval_seconds"))
    if interval is None or not 60 <= interval <= 86400: raise ValueError("Intervalle : 60 à 86400 secondes.")
    profiles = config.get("profiles")
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= 20: raise ValueError("Il faut entre 1 et 20 profils.")
    result, ids = [], set()
    for raw in profiles:
        if not isinstance(raw, dict): raise ValueError("Profil invalide.")
        base = GENERIC_PROFILE if raw.get("kind") == "general" else DEFAULT_PROFILE
        p = dict(copy.deepcopy(base), **{k: v for k, v in raw.items() if k in DEFAULT_PROFILE})
        if p["kind"] not in ["general", "ram"]: raise ValueError("Type de recherche inconnu.")
        if not isinstance(p["id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", p["id"]) or p["id"] in ids: raise ValueError("Identifiant de profil invalide ou dupliqué.")
        ids.add(p["id"])
        if not isinstance(p["name"], str) or not 1 <= len(p["name"]) <= 100: raise ValueError("Nom de profil invalide.")
        for k in ["queries", "exclude", "brands", "required_all", "required_any"]:
            if not isinstance(p[k], list) or len(p[k]) > 20 or any(not isinstance(x, str) or not 1 <= len(x.strip()) <= 150 for x in p[k]): raise ValueError("Liste invalide : " + k)
        if not p["queries"]: raise ValueError("Au moins une recherche par profil.")
        for k in ["generations", "capacities", "catalog_ids", "brand_ids", "size_ids", "status_ids"]:
            if not isinstance(p[k], list) or len(p[k]) > 30 or any(type(x) is not int or x < 1 or x > 10000000 for x in p[k]): raise ValueError("Valeurs invalides : " + k)
        if any(x not in [3, 4, 5] for x in p["generations"]): raise ValueError("Générations possibles : 3, 4, 5.")
        for k in ["enabled", "require_sodimm", "allow_unknown_capacity", "margin_only"]:
            if type(p[k]) is not bool: raise ValueError("Booléen attendu : " + k)
        for k in ["min_price", "max_price", "max_per_gb", "min_speed", "min_rating", "min_reviews", "shipping", "buyer_fee_fixed", "buyer_fee_percent", "conversion_cost", "resale_target", "resale_capacity", "selling_cost", "min_margin"]:
            n = number(p[k])
            if n is None or n < 0 or n > 100000: raise ValueError("Valeur numérique invalide : " + k)
            p[k] = n
        if p["min_price"] > p["max_price"]: raise ValueError("Prix minimum supérieur au maximum.")
        if p["min_rating"] > 5 or p["buyer_fee_percent"] > 100: raise ValueError("Note maximum 5 ; frais maximum 100 %.")
        result.append(p)
    appearance = dict(DEFAULT_APPEARANCE, **config.get("appearance", {})) if isinstance(config.get("appearance", {}), dict) else {}
    for key, choices in {"theme":["light","dark","system"], "accent":["copper","blue","green"], "density":["comfortable","compact"], "layout":["list","grid"]}.items():
        if appearance.get(key) not in choices: raise ValueError("Apparence invalide : " + key)
    for key in ["show_margin", "show_score"]:
        if type(appearance.get(key)) is not bool: raise ValueError("Option d'apparence invalide.")
    pages, per_page = config.get("pages", 1), config.get("per_page", 48)
    if type(pages) is not int or not 1 <= pages <= 5: raise ValueError("Pages par recherche : 1 à 5.")
    if type(per_page) is not int or not 1 <= per_page <= 96: raise ValueError("Résultats par page : 1 à 96.")
    notify_initial = config.get("notify_initial", False)
    if type(notify_initial) is not bool: raise ValueError("Option d'alertes invalide.")
    return {"interval_seconds": int(interval), "pages": pages, "per_page": per_page, "notify_initial": notify_initial,
            "appearance": {k: appearance[k] for k in DEFAULT_APPEARANCE}, "profiles": result}


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, data TEXT NOT NULL, first_seen TEXT, last_seen TEXT, source TEXT, favorite INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS prices (id INTEGER PRIMARY KEY, item_id TEXT, price REAL, at TEXT);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, item_id TEXT, kind TEXT, message TEXT, at TEXT);
            CREATE INDEX IF NOT EXISTS prices_by_item ON prices(item_id, id);
            CREATE INDEX IF NOT EXISTS items_by_seen ON items(last_seen DESC);
        ''')
        self.set("config", validate_config(self.get("config") or DEFAULT_CONFIG))

    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def ingest(self, items, source, notify=True, profile_id=None):
        # Validate the complete batch before making any changes.
        items = [canonical_item(x) for x in items]
        count = 0
        with self.lock, self.db:
            profiles = [p for p in self.get("config")["profiles"] if p["enabled"]]
            for item in items:
                previous = self.db.execute("SELECT data FROM items WHERE id=?", (item["id"],)).fetchone()
                old = json.loads(previous[0]) if previous else None
                if profile_id:
                    item["profile_ids"] = sorted(set((old or {}).get("profile_ids", []) + [profile_id]))
                at = now()
                self.db.execute("INSERT INTO items(id,data,first_seen,last_seen,source) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,last_seen=excluded.last_seen,source=excluded.source", (item["id"], json.dumps(item), at, at, source))
                changed = old is None or old["price"] != item["price"]
                if changed:
                    self.db.execute("INSERT INTO prices(item_id,price,at) VALUES (?,?,?)", (item["id"], item["price"], at))
                matches = [evaluate(item, p) for p in profiles]
                if notify and any(m["matched"] for m in matches) and (old is None or item["price"] < old["price"]):
                    kind = "new" if old is None else "price_drop"
                    message = ("Nouvelle annonce" if old is None else f"Baisse : {old['price']:.2f} € → {item['price']:.2f} €") + " · " + item["title"]
                    self.db.execute("INSERT INTO events(item_id,kind,message,at) VALUES (?,?,?,?)", (item["id"], kind, message, at))
                    count += 1
            self.db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 2000)")
        return {"processed": len(items), "alerts": count}

    def listing(self):
        with self.lock:
            profiles = [p for p in self.get("config")["profiles"] if p["enabled"]]
            result = []
            for row in self.db.execute("SELECT * FROM items ORDER BY last_seen DESC LIMIT 2000"):
                item = json.loads(row["data"])
                evaluations = [evaluate(item, p) for p in profiles]
                matches = sorted([e for e in evaluations if e["matched"]], key=lambda x: x["score"], reverse=True)
                history = [dict(p) for p in self.db.execute("SELECT price,at FROM prices WHERE item_id=? ORDER BY id", (item["id"],))]
                result.append(dict(item, first_seen=row["first_seen"], last_seen=row["last_seen"], source=row["source"], favorite=bool(row["favorite"]), hidden=bool(row["hidden"]), matches=matches, evaluations=evaluations, history=history))
            return result


def parse_search_url(url):
    parsed = urllib.parse.urlparse(str(url))
    if parsed.scheme != "https" or parsed.hostname not in ["www.vinted.fr", "vinted.fr"] or not parsed.path.startswith("/catalog") or parsed.username:
        raise ValueError("Colle une URL de recherche https://www.vinted.fr/catalog…")
    params = urllib.parse.parse_qs(parsed.query)
    result = {}
    if params.get("search_text"): result["queries"] = [params["search_text"][0]]
    for field in ["catalog", "brand", "size", "status"]:
        values = params.get(field + "_ids[]", []) + params.get(field + "_ids", []) + params.get("attribute_ids[" + field + "]", [])
        result[field + "_ids"] = sorted({int(x) for v in values for x in v.split(",") if x.isdigit()})
    for field, key in [("price_from", "min_price"), ("price_to", "max_price")]:
        if field in params:
            value = number(params[field][0])
            if value is None or value < 0: raise ValueError("Prix invalide dans l'URL.")
            result[key] = value
    return result


VintedProvider = RemoteProvider


class Monitor:
    def __init__(self, store, provider=None):
        self.store, self.provider = store, provider or RemoteProvider(None)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.running, self.busy, self.next_run = False, False, 0
        self.last_run, self.error, self.failures = None, "", 0
        self.current_query, self.last_count, self.cycles = "", 0, 0
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def status(self):
        with self.lock:
            return {"running": self.running, "busy": self.busy, "next_run": self.next_run,
                    "last_run": self.last_run, "error": self.error, "failures": self.failures,
                    "current_query": self.current_query, "last_count": self.last_count, "cycles": self.cycles,
                    "last_http": getattr(self.provider, "last_http", None), "connector": "service distant"}

    def control(self, action):
        with self.lock:
            if action == "start":
                self.running = True
                self.next_run = max(time.time(), self.next_run)
            elif action == "stop": self.running = False
            else: raise ValueError("Action inconnue.")

    def tick(self):
        with self.lock:
            if self.busy or not self.running: return
        with self.lock:
            if time.time() < self.next_run: return
            self.busy = True
        config = self.store.get("config")
        cycle_started = time.time()
        try:
            jobs = [(p, q, page) for page in range(1, config["pages"] + 1) for p in config["profiles"] if p["enabled"] for q in p["queries"]]
            if not jobs: raise ProviderError("Active au moins un profil.", blocked=True)
            count = 0
            cycle_results = {}
            exhausted = set()
            fetched = False
            for index, (profile, query, page) in enumerate(jobs):
                with self.lock:
                    if not self.running: return
                    self.current_query = query + " · page " + str(page)
                request_key = json.dumps([query, page, config["per_page"], *[profile[k] for k in ["min_price", "max_price", "catalog_ids", "brand_ids", "size_ids", "status_ids"]]], sort_keys=True)
                series_key = json.dumps([query, *[profile[k] for k in ["min_price", "max_price", "catalog_ids", "brand_ids", "size_ids", "status_ids"]]], sort_keys=True)
                if (series_key, page) in exhausted: continue
                if request_key not in cycle_results:
                    if fetched and self.stop_event.wait(2): return
                    with self.lock:
                        if not self.running: return
                    fetched = True
                    cycle_results[request_key] = self.provider.search(query, profile, page=page, per_page=config["per_page"])
                items = cycle_results[request_key]
                if len(items) < config["per_page"]:
                    exhausted.update((series_key, later) for later in range(page + 1, config["pages"] + 1))
                baseline_key = "baseline:" + json.dumps([profile["id"], query, page, profile["catalog_ids"], profile["brand_ids"], profile["size_ids"], profile["status_ids"], profile["min_price"], profile["max_price"], config["per_page"]], sort_keys=True)
                notify = config["notify_initial"] or bool(self.store.get(baseline_key))
                self.store.ingest(items, "vinted", notify=notify, profile_id=profile["id"])
                self.store.set(baseline_key, True)
                count += len(items)
            with self.lock:
                self.error, self.failures, self.last_run = "", 0, now()
                self.last_count, self.cycles = count, self.cycles + 1
                self.next_run = max(time.time() + 2, cycle_started + config["interval_seconds"])
        except Exception as error:
            with self.lock:
                self.failures += 1
                self.error = str(error) if isinstance(error, ProviderError) else "Erreur de traitement des annonces. Vérifier le format du connecteur."
                self.next_run = time.time() + max(getattr(error, "retry", 0), min(3600, config["interval_seconds"] * 2 ** min(self.failures, 5)))
                if getattr(error, "blocked", False): self.running = False
        finally:
            with self.lock:
                self.busy, self.current_query = False, ""

    def loop(self):
        while not self.stop_event.wait(1): self.tick()



def make_handler(store, monitor, port, connection=None):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def respond(self, data, status=200, mime="application/json"):
            body = json.dumps(data, ensure_ascii=False).encode() if mime == "application/json" else data.encode() if isinstance(data, str) else data
            self.send_response(status)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data: https://*.vinted.net; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def do_GET(self):
            if not self.valid_host(): return self.respond({"error": "Hôte refusé"}, 403)
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/state":
                with store.lock:
                    events = [dict(r) for r in store.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")]
                return self.respond({"config": store.get("config"), "status": monitor.status(), "items": store.listing(), "events": events, "token": token,
                    "connection": connection.public() if connection else {"configured": False}, "templates": {"general": GENERIC_PROFILE, "ddr5": DEFAULT_PROFILE, "ddr4": DEFAULT_CONFIG["profiles"][1]}, "version": "2.0.0"})
            if path == "/api/config": return self.respond(store.get("config"))
            if path == "/api/export.csv":
                out = io.StringIO()
                writer = csv.writer(out)
                writer.writerow(["id", "titre", "prix", "url", "source", "profil", "cout_estime", "marge_hypothese"])
                for item in store.listing():
                    for match in item["matches"]:
                        row = [item["id"], item["title"], item["price"], item["url"], item["source"], match["profile_name"], match["landed"], match["margin"]]
                        writer.writerow(["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in row])
                return self.respond(out.getvalue(), mime="text/csv")
            files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
            if path in files:
                file, mime = files[path]
                return self.respond((ROOT / "static" / file).read_bytes(), mime=mime)
            self.respond({"error": "Introuvable"}, 404)

        def do_POST(self):
            if not self.valid_host() or self.headers.get("X-Radar-Token") != token:
                return self.respond({"error": "Accès refusé"}, 403)
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}:
                return self.respond({"error": "Origine refusée"}, 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2_000_000: raise ValueError("Corps de requête vide ou trop volumineux.")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict): raise ValueError("Objet JSON requis.")
                if self.path == "/api/config": store.set("config", validate_config(data))
                elif self.path == "/api/parse-search": return self.respond(parse_search_url(data.get("url")))
                elif self.path == "/api/preview":
                    config = validate_config({"interval_seconds": 300, "profiles": [data.get("profile")]})
                    item = canonical_item(dict(data.get("item", {}), id="preview"))
                    return self.respond(evaluate(item, config["profiles"][0]))
                elif self.path == "/api/control": monitor.control(data.get("action"))
                elif self.path == "/api/connection":
                    if connection is None: raise ValueError("Connexion indisponible.")
                    monitor.control("stop")
                    connection.save(data.get("url", ""), data.get("activation_key", data.get("api_key", "")))
                    return self.respond(connection.public())
                elif self.path == "/api/connection/test":
                    try: return self.respond(monitor.provider.verify())
                    except ProviderError as error: return self.respond({"error": str(error)}, 502)
                elif self.path == "/api/import":
                    items = data.get("items")
                    if not isinstance(items, list) or len(items) > 1000: raise ValueError("Liste de 1000 annonces maximum.")
                    return self.respond(store.ingest(items, "import"))
                elif self.path == "/api/item":
                    if data.get("field") not in ["favorite", "hidden"] or type(data.get("value")) is not bool: raise ValueError("Modification invalide.")
                    with store.lock, store.db:
                        store.db.execute("UPDATE items SET " + data["field"] + "=? WHERE id=?", (int(data["value"]), str(data.get("id"))))
                else: return self.respond({"error": "Introuvable"}, 404)
                self.respond({"ok": True})
            except (ValueError, TypeError, KeyError) as error:
                self.respond({"error": str(error)}, 400)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--open", action="store_true", help="Open the local dashboard in your browser")
    parser.add_argument("--diagnose", action="store_true", help="Test the live Vinted connector without starting a server")
    args = parser.parse_args()
    if args.diagnose:
        try:
            items = RemoteProvider(Connection(args.data_dir / "connection.json")).search("sodimm ddr5", DEFAULT_PROFILE, per_page=5)
            valid = [canonical_item(x) for x in items]
            print(json.dumps({"ok": True, "count": len(valid), "titles": [x["title"] for x in valid]}, ensure_ascii=False))
        except (ProviderError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
            raise SystemExit(1)
        return
    args.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(args.data_dir / "radar.sqlite3")
    connection = Connection(args.data_dir / "connection.json")
    monitor = Monitor(store, RemoteProvider(connection))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store, monitor, args.port, connection))
    except OSError as error:
        print(f"Impossible de démarrer sur le port {args.port} : {error}. Essaie --port 8766.")
        store.db.close()
        raise SystemExit(1)
    monitor.thread.start()
    print(f"Repère : http://127.0.0.1:{args.port} — Ctrl+C pour arrêter", flush=True)
    if args.open: webbrowser.open(f"http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        monitor.stop_event.set()
        server.server_close()


if __name__ == "__main__": main()
