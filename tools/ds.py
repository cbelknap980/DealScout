#!/usr/bin/env python3
"""ds.py: the only way Deal Scout's collectors read and change the data repo.

The data lives in the private repo cbelknap980/dealscout-data as plain JSON files.
Two collectors (cloud and PC) and the page all write to it. To keep them from
overwriting each other, collectors never edit the JSON by hand. Every change goes
through a command here, which (1) applies it to the local files and (2) records it
in .ds-ops.jsonl. `ds.py commit` pushes; if someone else pushed first, it resets
to their newest copy, replays the recorded changes on top, and pushes again.

Python 3 standard library only. Run from the folder that holds both clones
(DealScout/ and dealscout-data/), or pass --data PATH.

Commands (run `ds.py help` for the list):
  summary                          searches, sites, pending work, last runs
  listings <searchId> [filters]    one line per listing
  get <id>                         one listing as JSON (status included)
  score <searchId>                 market price and every deal-priced listing
  queue <searchId> refresh [--for cloud|pc]   what to re-open this run, in order, capped
  queue <searchId> photos          what needs a photo check or price re-confirm, best deal first
  zip <zip> | city "<City, ST>"    coordinates; dist <lat> <lng> [--zip Z] miles
  upsert '<json>' | upsert -       add or update listings (object or array)
  check <id> '<json>'              set a listing's legitimacy check
  gone <id> ["note"]               mark a listing sold or expired
  alerted <id> [iso]               record that a deal email went out
  route <cloud|pc> <site> '<json>' write your own connection route for a site
  site <id> '<json>'               add or update a site's facts (not routes)
  search <id> '<json>'             update a search's fields (collectors never change active, everyDays, activeSince)
  due [--for cloud|pc] [--json]    which searches this run works on: on, and due by their frequency
  run '<json>'                     add a run record (only for a search that is due this run)
  beat [worked|nothing-due|skipped] ["note"]  this collector's check-in for the page, at the end of EVERY run
                                   (v2.8), even one with nothing due; commit after it
  unpaste <id>                     drop a pasted link once its listing is stored
  specs [<searchId>]               fill listing specs from titles (kinds with a title parser, e.g. skis)
  commit "<message>"               push everything recorded since the last commit
"""
import json, math, os, re, subprocess, sys, time, unicodedata
from datetime import datetime, timedelta, timezone

OPS = ".ds-ops.jsonl"
HEART = "heartbeat.json"  # v2.8: each collector's last check-in, so the page shows the runs are still going
BEAT_RESULTS = ("worked", "nothing-due", "skipped", "running")
REPLAY = ".ds-replay"  # present only while commit is replaying; if a run dies there, the next commit starts over
COLLECTOR = os.environ.get("DS_COLLECTOR", "")  # "cloud" or "pc"
HERE = os.path.dirname(os.path.abspath(__file__))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(iso):
    try:
        return datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def die(msg, code=2):
    print("ds.py: " + msg, file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- files
def find_data(argv):
    if "--data" in argv:
        i = argv.index("--data")
        p = argv[i + 1]
        del argv[i:i + 2]
        return os.path.abspath(p)
    for p in (os.environ.get("DS_DATA"), "dealscout-data", "../dealscout-data",
              os.path.join(HERE, "..", "..", "dealscout-data")):
        if p and os.path.isfile(os.path.join(p, "rules.json")):
            return os.path.abspath(p)
    die("can't find the dealscout-data clone; pass --data PATH")


class Store:
    def __init__(self, root):
        self.root = root
        self._cache = {}

    def path(self, rel):
        return os.path.join(self.root, rel)

    def read(self, rel, default=None):
        if rel in self._cache:
            return self._cache[rel]
        p = self.path(rel)
        if not os.path.exists(p):
            val = default
        else:
            with open(p, encoding="utf-8") as f:
                val = json.load(f)
        self._cache[rel] = val
        return val

    def write(self, rel, obj):
        self._cache[rel] = obj
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, sort_keys=True, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, p)

    def reset_cache(self):
        self._cache = {}

    # convenience
    def rules(self):
        return self.read("rules.json", {})

    def searches(self):
        return self.read("searches.json", {})

    def sites(self):
        return self.read("sites.json", {})

    def status(self):
        return self.read("status.json", {})

    def pasted(self):
        return self.read("pasted.json", {})

    def runs(self):
        return self.read("runs.json", [])

    def heartbeat(self):
        hb = self.read(HEART, {})
        return hb if isinstance(hb, dict) else {}

    def listing_files(self):
        d = self.path("listings")
        if not os.path.isdir(d):
            return []
        return sorted("listings/" + f for f in os.listdir(d) if f.endswith(".json"))

    def all_listings(self):
        """id -> listing (with id, searchId, status overlay)."""
        out = {}
        st = self.status()
        for rel in self.listing_files():
            for lid, l in (self.read(rel, {}) or {}).items():
                x = dict(l)
                x["id"] = lid
                x["status"] = (st.get(lid) or {}).get("status") or "new"
                out[lid] = x
        return out

    def find(self, lid):
        for rel in self.listing_files():
            m = self.read(rel, {}) or {}
            if lid in m:
                return rel, m
        return None, None


# ---------------------------------------------------------------- geo
_GEO = None


def geo():
    global _GEO
    if _GEO is None:
        for p in (os.path.join(HERE, "..", "geo.json"), os.path.join(HERE, "geo.json")):
            if os.path.exists(p):
                with open(p) as f:
                    _GEO = json.load(f)
                break
        else:
            _GEO = {"z": {}, "c": {}}
    return _GEO


def zip_coords(z):
    z = str(z or "")
    return geo()["z"].get(z) if re.fullmatch(r"\d{5}", z) else None


def city_alias(city):
    """"St. Paul" and "Saint Paul" are one town; geo.json has most in the long form and a few as "st james"
    (same lookups as coordsFor() in index.html)."""
    for a, b in ((r"^st\.?\s+", "saint "), (r"^ste\.?\s+", "sainte "), (r"^ft\.?\s+", "fort "), (r"^mt\.?\s+", "mount ")):
        city = re.sub(a, b, city)
    return city


def city_coords(loc):
    m = re.match(r"^\s*([^,]+?),\s*([A-Za-z]{2})\b", str(loc or ""))
    if not m:
        return None
    city, st = re.sub(r"\s+", " ", m.group(1).strip().lower()), "|" + m.group(2).upper()
    c = geo()["c"]
    return c.get(city + st) or c.get(city.replace(".", "") + st) or c.get(city_alias(city) + st)


def coords_for(l):
    if isinstance(l.get("lat"), (int, float)) and isinstance(l.get("lng"), (int, float)):
        return [l["lat"], l["lng"]]
    zm = re.search(r"\d{5}", str(l.get("zip") or "")) or re.search(r"\b(\d{5})\b", str(l.get("location") or ""))
    if zm:
        c = zip_coords(zm.group(0))
        if c:
            return c
    return city_coords(l.get("location"))


def miles_between(a, b):
    if not a or not b:
        return None
    R = 3958.8
    rad = math.radians
    dla, dlo = rad(b[0] - a[0]), rad(b[1] - a[1])
    h = math.sin(dla / 2) ** 2 + math.cos(rad(a[0])) * math.cos(rad(b[0])) * math.sin(dlo / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- scoring (mirrors score() in index.html)
def num(x):
    try:
        if x is None or x == "":
            return None
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def hours_since(iso):
    try:
        t = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600
    except Exception:
        return float("inf")


def branded(l, words):
    if l.get("titleStatus"):
        return not re.fullmatch(r"clean", str(l["titleStatus"]), re.I)
    pat = r"\b(" + "|".join(re.escape(str(w)) for w in words) + r")\b"
    return bool(re.search(pat, str(l.get("title") or ""), re.I))


# ---------------------------------------------------------------- item kinds (v2.6, rules v1.7; mirrors kindOf() in index.html)
# rules.json "kinds" defines each kind of item a search can be: the specs recorded on its listings, which of them
# a search can limit (search.specLimits) and the page can filter, its default pricing, whether local pickup ranks
# first (nearFirst) and a shipping estimate for ship-in listings with no shipping price (shipEstimate).
KINDS = {}


def set_kinds(rules):
    global KINDS
    KINDS = (rules or {}).get("kinds") or {}


def kind_key(s):
    k = (s or {}).get("kind")
    if k in KINDS:
        return k
    c = str((s or {}).get("category") or "").lower().strip()
    for key, v in KINDS.items():
        for x in v.get("categories") or []:
            if c and re.search(r"(?<![a-z0-9])" + re.escape(str(x).lower()) + r"(?![a-z0-9])", c):
                return key
    return "other"


def kind_of(s):
    return KINDS.get(kind_key(s)) or {}


def fold(x):
    """Lowercase with accents dropped: Völkl -> volkl."""
    return unicodedata.normalize("NFKD", str(x or "")).encode("ascii", "ignore").decode().lower()


def words_of(x):
    """Words for model matching, with letters and digits counted apart: "108Ti" -> ["108", "ti"]."""
    t = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", fold(x))
    return re.findall(r"[a-z0-9]+", t)


def model_match(l, s):
    """True when the search names no models or every word of one named model is in the listing's title or specs."""
    want = [m for m in ((s or {}).get("models") or []) if str(m).strip()]
    if not want:
        return True
    sp = l.get("specs") or {}
    have = set(words_of(l.get("title")) + words_of(sp.get("brand")) + words_of(sp.get("model")))
    return any(all(w in have for w in words_of(m)) for m in want)


def spec_outside(l, s):
    """The spec limits (search.specLimits) and named models a listing is outside. Unknown specs never count."""
    kd = kind_of(s)
    out = []
    sp = l.get("specs") or {}
    lim = (s or {}).get("specLimits") or {}
    can = set(kd.get("limits") or [])
    for d in kd.get("specs") or []:
        k = d.get("key")
        L = lim.get(k)
        if k not in can or L in (None, {}, []):
            continue
        if d.get("type") == "number" and isinstance(L, dict):
            v, lo, hi = num(sp.get(k)), num(L.get("min")), num(L.get("max"))
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            if v is not None and ((lo is not None and v < lo) or (hi is not None and v > hi)):
                out.append(k)
        elif d.get("type") in ("choice", "flag") and isinstance(L, list):
            v = sp.get(k)
            if d.get("type") == "flag":
                v = "yes" if v is True else "no"
            if v is not None and v not in L:
                out.append(k)
    want = [m for m in ((s or {}).get("models") or []) if str(m).strip()]
    if kd.get("models") and want:
        if sp.get("model"):
            if not model_match(l, s):
                out.append("model")
        elif sp.get("brand"):
            # Model not stated: a brand none of the named models carry still rules it out.
            bw = set(words_of(sp["brand"]))
            if not any(bw <= set(words_of(m)) for m in want):
                out.append("model")
    return out


# --- ski specs from a title written as "<Brand> <Model> <length> cm + <binding>" (vocabulary in rules kinds.skis.parse)
LEN_RE = re.compile(r"(?<![\d.])(\d{2,3})\s*cm\b", re.I)
ACCESSORY = re.compile(r"^(?:and\s+)?(?:\w+\s+)?(?:poles?|skins?|boots?|helmet|goggles|bag|leashes|straps|pack|package)\b", re.I)
GENERIC_SKI = re.compile(r"\b(?:skis?|pair|vintage|used|new|nos|brand new|like new|women'?s|womens|men'?s|mens|unisex|twin[- ]tip|"
                         r"all[- ]mountain|downhill|alpine|carving|racing|mogul|powder|freeride|freestyle|touring|backcountry|telemark|"
                         r"cross[- ]country|xc|nordic|waxless|woodcore|wood|rock|demo|mounted|junior|kids|package|set|at)\b", re.I)
GENERIC_BIND = re.compile(r"\b(?:bindings?|demo|adjustable|integrated|system|vintage|alpine|at|touring|tech|frame|brand new|new|matched|"
                          r"loose|rental|older|used|old|race|racing|plates?|on)\b", re.I)


def _word(text, w):
    return re.search(r"(?<![a-z0-9])" + re.escape(fold(w)) + r"(?![a-z0-9])", text) is not None


def derive_ski_specs(title, note, cfg):
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    out = {}
    m = next((x for x in LEN_RE.finditer(t) if 80 <= int(x.group(1)) <= 230), None)
    if m:
        out["lengthCm"] = int(m.group(1))
    parts = re.split(r"\s\+\s|\s+with\s+|\s+w/\s*", t, maxsplit=1, flags=re.I)
    ski, bind = parts[0], (parts[1] if len(parts) > 1 else "")
    # brand: at the start (after years and words like vintage), else anywhere for names that aren't common words
    low = fold(ski)
    lead = low
    while True:
        nxt = re.sub(r"^(?:\(?(?:19|20)\d{2}(?:[/-]\d{2,4})?\)?|'\d{2}(?:/'?\d{2})?|vintage|new|used|nos|brand new|like new|women'?s|"
                     r"womens|men'?s|mens|pair of|a pair of|demo)[\s,:-]+", "", lead)
        if nxt == lead:
            break
        lead = nxt
    names = [(fold(a), b) for a, b in (cfg.get("aliases") or {}).items()] + [(fold(b), b) for b in cfg.get("brands") or []]
    names.sort(key=lambda x: -len(x[0]))
    common = {fold(x) for x in cfg.get("commonWordBrands") or []}
    brand = hit = None
    for n, b in names:
        if lead == n or lead.startswith(n + " ") or lead.startswith(n + ","):
            brand, hit = b, n
            break
    if not brand:
        for n, b in names:
            if fold(b) not in common and _word(low, n):
                brand, hit = b, n
                break
    if brand:
        out["brand"] = brand
        mdl = re.sub(r"\([^)]*\)", " ", ski)
        mdl = mdl.split(",")[0]
        mdl = re.sub(r"(?<![\d.])(?:19|20)\d{2}(?:[/-]\d{2,4})?(?![\d.])|'\d{2}(?:/'?\d{2})?", " ", mdl)
        mdl = LEN_RE.sub(" ", mdl).replace("~", " ")
        mdl = _drop_folded(mdl, hit)
        mdl = GENERIC_SKI.sub(" ", mdl)
        mdl = re.sub(r"\s+", " ", mdl).strip(" -:;/.'\"")
        if re.search(r"[A-Za-z0-9]", mdl) and len(mdl) <= 40:
            out["model"] = mdl
    # underfoot: stated in the title or note, else the first 60-130 number in the model name
    for src in (t, str(note or "")):
        w = None
        m = re.search(r"(?<![\d.])\d{2,3}\s*/\s*(\d{2,3})\s*/\s*\d{2,3}(?![\d.])", src)
        if m and 50 <= int(m.group(1)) <= 150:
            w = int(m.group(1))
        if w is None:
            m = re.search(r"(?<![\d.])(\d{2,3})(?:\s*-\s*\d{2,3})?\s*mm\s*(?:waist|underfoot)\b|\b(?:waist|underfoot)\s*(?:width\s*)?(?:of|is|:)?\s*"
                          r"(\d{2,3})(?![\d.])|(?<![\d.])(\d{2,3})\s*(?:mm\s*)?underfoot\b|\((?:[^()]*?,\s*)?(\d{2,3})\s*mm\)", src, re.I)
            if m:
                v = int(next(g for g in m.groups() if g))
                if 50 <= v <= 150:
                    w = v
        if w is not None:
            out["waistMm"], out["waistFrom"] = w, "ad"
            break
    if "waistMm" not in out and out.get("model"):
        sp_ = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", out["model"])
        for tok in re.findall(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])", sp_):
            if "." not in tok and 60 <= int(tok) <= 130:
                out["waistMm"], out["waistFrom"] = int(tok), "model name"
                break
    # bindings (the brand's own name is left out of the title words: "Lib Tech" is not a tech binding)
    tl = fold(_drop_folded(t, hit)) if hit else fold(t)
    seg = re.split(r"\s\+\s|\(|,", bind)[0].strip() if bind else ""
    seg = re.sub(r"\s+", " ", re.sub(r"(?<![\d.])(?:19|20)\d{2}(?![\d.])", " ", seg)).strip()
    bl = fold(seg)
    if seg and ACCESSORY.match(seg):
        seg, bl = "", ""
    bbrand = next((b for b in sorted(cfg.get("bindingBrands") or [], key=len, reverse=True) if _word(bl, b)), None)
    if not bbrand:
        bbrand = next((b for k, b in sorted((cfg.get("bindingModels") or {}).items(), key=lambda x: -len(x[0])) if _word(bl, k)), None)
    flat = re.search(r"\b(?:flat|no bindings?|without bindings?|bindings? not included)\b", tl)
    if flat and not bbrand:
        out["bindings"] = "none"
    elif seg or re.search(r"\bbindings?\b|\bmounted\b", tl):
        out["bindings"] = "yes"
        if bbrand:
            out["bindingBrand"] = bbrand
        rest = GENERIC_BIND.sub(" ", seg)
        for b in [bbrand] if bbrand else []:
            rest = re.sub(r"(?<![A-Za-z])" + re.escape(b) + r"(?![A-Za-z])", " ", rest, count=1, flags=re.I)
        if re.search(r"[A-Za-z0-9]", re.sub(r"\s+", " ", rest)) and len(seg) <= 40:
            out["bindingModel"] = re.sub(r"\s+bindings?$", "", seg, flags=re.I).strip()
        tw = {fold(x) for x in cfg.get("titleTypeWords") or []}
        typ = None
        for k in ("xc", "telemark", "touring", "demo"):
            ws = (cfg.get("bindingTypeWords") or {}).get(k) or []
            if any(_word(bl, w) for w in ws) or any(fold(w) in tw and _word(tl, w) for w in ws):
                typ = k
                break
        if typ is None and (bbrand or "bindingModel" in out or _word(tl, "alpine") or seg):
            typ = "alpine"
        if typ:
            out["bindingType"] = typ
    if re.search(r"\b(?:women'?s|womens|ladies|lady|wmns?)\b", tl) or (out.get("model") and re.search(r"(?<![A-Za-z])W(?![A-Za-z'])", out["model"])):
        out["women"] = True
    return out


def _drop_folded(text, name):
    """Remove the first occurrence of a brand name, matching accents loosely (Völkl, Volkl)."""
    f = fold(text)
    m = re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", f)
    if not m:
        return text
    # fold() can change length only for characters it drops; map positions by folding prefix by prefix
    start = next(i for i in range(len(text) + 1) if len(fold(text[:i])) >= m.start())
    end = next(i for i in range(start, len(text) + 1) if len(fold(text[:i])) >= m.end())
    return text[:start] + " " + text[end:]


def fill_specs(l, s):
    """Fill the specs a kind with a title parser defines from the listing's title. A spec a collector sent (not in
    specsAuto) is never overwritten; ones ds.py filled earlier follow the title if it changes. Returns True if changed."""
    cfg = kind_of(s).get("parse")
    if not cfg or kind_key(s) != "skis":
        return False
    before = (json.dumps(l.get("specs"), sort_keys=True), list(l.get("specsAuto") or []))
    sp = dict(l.get("specs") or {})
    auto = set(l.get("specsAuto") or [])
    got = derive_ski_specs(l.get("title"), l.get("note"), cfg)
    for k in list(auto):
        if k not in got:
            sp.pop(k, None)
            auto.discard(k)
    for k, v in got.items():
        if k == "waistFrom":
            continue
        if k not in sp or k in auto:
            sp[k] = v
            auto.add(k)
    if "waistMm" in auto and "waistFrom" in got:
        sp["waistFrom"] = got["waistFrom"]
        auto.add("waistFrom")
    elif "waistFrom" in auto and "waistMm" not in auto:
        auto.discard("waistFrom")
    if sp:
        l["specs"] = sp
    else:
        l.pop("specs", None)
    if auto:
        l["specsAuto"] = sorted(auto)
    else:
        l.pop("specsAuto", None)
    return (json.dumps(l.get("specs"), sort_keys=True), list(l.get("specsAuto") or [])) != before


# ---------------------------------------------------------------- search limits (v2.5, mirrors itemOutside() in index.html)
def item_outside(l, s):
    """Which of the search's item limits a listing is outside: "year" (minYear/maxYear), "miles" (maxMiles) and,
    since v2.6, a spec key such as "lengthCm" (search.specLimits) or "model" (search.models). An unknown never counts. score() adds "price" (over maxPrice) and "ship" (shipOutside, past
    radiusMi and ships is false). Year and miles also keep a listing out of the market median; the ceiling and
    the ship rule don't, since they are about Cameron, not the market. Exclude words stay with the collector."""
    lo, hi = num(s.get("minYear")), num(s.get("maxYear"))
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    y, m, mx = num(l.get("year")), num(l.get("miles")), num(s.get("maxMiles"))
    out = []
    if y is not None and ((lo is not None and y < lo) or (hi is not None and y > hi)):
        out.append("year")
    if m is not None and mx is not None and m > mx:
        out.append("miles")
    # v2.6: the kind's spec limits (ski length, underfoot, bindings, binding type) and named models
    out.extend(spec_outside(l, s))
    return out


def outside_hidden(l, sc):
    """Outside the search's limits and not marked watching or contacted: out of the open list (page: Status: Outside search)."""
    return bool(sc.get("outside")) and (l.get("status") or "new") == "new"


def market_for(s, sid, listings, nums):
    if s.get("pricing") == "per-item":
        return {"value": None, "perItem": True, "basis": "each listing against its own model's fair used price"}
    mp = num(s.get("marketPrice"))
    if mp:
        return {"value": mp, "basis": "set by Cameron"}
    prices = sorted(float(l["price"]) for l in listings.values()
                    if l.get("searchId") == sid and l.get("status") != "passed" and not l.get("gone")
                    and not branded(l, nums.get("brandedWords", [])) and (l.get("check") or {}).get("status") != "failed"
                    and num(l.get("price")) and not item_outside(l, s))
    if not prices:
        return {"value": None, "basis": "no listings yet"}
    n = len(prices)
    med = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
    return {"value": med, "basis": f"median of {n} listing{'s' if n != 1 else ''}"}


def score(l, s, market, nums):
    kd = kind_of(s)
    ref = num(l.get("refPrice")) if s.get("pricing") == "per-item" else market["value"]
    price = num(l.get("price"))
    r = num(s.get("radiusMi"))
    home = miles_between(zip_coords(s.get("zip")), coords_for(l))
    if home is None and isinstance(l.get("distanceMi"), (int, float)):
        home = l["distanceMi"]
    in_radius = True if (r is None or not s.get("zip")) else (home is not None and home <= r)
    # v2.6 shipping estimate: a ship-in listing (past radiusMi, or no location and it ships) with no shipping price
    # is judged on price + the kind's shipEstimate until the ad shows the real cost.
    est = num(kd.get("shipEstimate"))
    ship_in = r is not None and bool(s.get("zip")) and (home > r if home is not None else l.get("ships") is True)
    ship_cost = num(l.get("shipCost"))
    ship_est = est if (est and ship_in and l.get("ships") is not False and ship_cost is None) else None
    cost = None if price is None else price + (ship_cost if ship_cost is not None else (ship_est or 0))
    pct = (ref - cost) / ref if (ref and cost is not None) else None
    thr = (num(s.get("dealPct")) if num(s.get("dealPct")) is not None else nums.get("dealPctDefault", 15)) / 100
    ceiling = num(s.get("maxPrice"))
    under_market = pct is not None and pct >= thr
    under_ceiling = True if ceiling is None else (cost is not None and cost <= ceiling)
    reachable = not (s.get("shipOutside") and r is not None and home is not None and home > r and l.get("ships") is False)
    brand = branded(l, nums.get("brandedWords", [])) and s.get("allowBranded") is not True
    verified = l.get("verified") is not False
    chk = l.get("check") or {}
    exceptional = pct is not None and (pct >= nums.get("exceptionalPct", 35) / 100 or
                                       (pct >= nums.get("exceptionalPrivatePct", 25) / 100 and l.get("sellerType") == "private"))
    need = 3 if exceptional else 2
    passed = chk.get("status") == "passed" and int(chk.get("level") or 0) >= need
    failed = chk.get("status") == "failed"
    confirmed = max([x for x in (l.get("lastChecked"), l.get("lastSeen"), chk.get("at")) if x] or [""])
    # v2.7: a search that runs every few days keeps its prices fresh until its next run (the window grows by the
    # days between runs), so a checked deal doesn't drop to "being checked" on the days it isn't searched.
    fresh = hours_since(confirmed) <= nums.get("priceFreshHours", 24) + cycle_hours(s)
    live = not l.get("gone")
    outside = item_outside(l, s)
    if ceiling is not None and cost is not None and cost > ceiling:
        outside.append("price")
    if not reachable:
        outside.append("ship")
    fits = not outside
    is_deal = live and fits and under_market and under_ceiling and reachable and verified and not brand and passed and fresh
    lead = live and fits and under_market and under_ceiling and reachable and not brand and not is_deal and not failed
    # What a lead is waiting on (v2.1). A caution verdict is settled until the price changes after it or it
    # passes numbers.recheckCheckDays; until then it is not photo work, it is "has concerns" for Cameron.
    chk_at = chk.get("at") or ""
    settled = (chk.get("status") == "caution" and hours_since(chk_at) <= nums.get("recheckCheckDays", 7) * 24
               and str(l.get("priceChangedAt") or "") <= chk_at)
    concern = lead and settled
    reconfirm = lead and passed and not fresh
    photo_work = lead and not passed and not settled
    return {"pct": pct, "ref": ref, "cost": cost, "isDeal": is_deal, "lead": lead, "checkFailed": failed,
            "concern": concern, "reconfirm": reconfirm, "photoWork": photo_work,
            "needLevel": need, "branded": brand, "underMarket": under_market, "underCeiling": under_ceiling,
            "inRadius": in_radius, "homeDist": None if home is None else round(home), "fresh": fresh,
            "checkPassed": passed, "outside": outside, "shipEst": ship_est, "nearFirst": bool(kd.get("nearFirst"))}


# ---------------------------------------------------------------- probably gone (v2.4, mirrors probablyGone() in index.html)
def site_read_at(sites, source, sid):
    """Latest time a route covering this search read the site fine (state ok or partial)."""
    best = ""
    for k in ("pc", "cloud"):
        r = (sites.get(source) or {}).get(k)
        if not isinstance(r, dict) or (isinstance(r.get("searches"), list) and sid not in r["searches"]):
            continue
        for t in (r.get("lastOk"), r.get("at") if r.get("state") in ("ok", "partial") else None):
            if t and str(t) > best:
                best = str(t)
    return best or None


def last_evidence(l):
    """The last time anything showed the listing was live: a search result, an opened ad or a check."""
    return max([str(x) for x in (l.get("lastSeen"), l.get("lastChecked"), (l.get("check") or {}).get("at"), l.get("foundAt")) if x] or [""]) or None


def probably_gone(l, sc, sites, nums):
    """Days unseen when an open listing counts as probably gone, else None. Probably gone = status new, not
    priced like a deal, nothing has shown it for numbers.probablyGoneDays days, and its site has been read
    fine since that many days after it was last seen (so a paused or blocked site hides nothing). Nothing is
    stored: the next search result or opened ad that shows it brings it back."""
    days = float(nums.get("probablyGoneDays", 7) or 0)
    if days <= 0 or l.get("gone") or (l.get("status") or "new") != "new":
        return None
    ev = parse_iso(last_evidence(l))
    if not ev:
        return None
    age = (datetime.now(timezone.utc) - ev).total_seconds() / 86400
    if age < days or (sc["underMarket"] and sc["underCeiling"]):
        return None
    read = parse_iso(site_read_at(sites, l.get("source"), l.get("searchId")))
    if not read or read < ev + timedelta(days=days):
        return None
    return int(age)


# ---------------------------------------------------------------- schedule (v2.7, rules v1.9; mirrors the schedule code in index.html)
# Each search has an on/off switch (active) and a frequency (everyDays: 1 = every day, 3 = every 3 days, 7 = weekly),
# both Cameron's. Time is counted in search days: a search day runs from rules.json schedule.dayStartHour (17, 5 pm)
# to the same hour the next day, in schedule.timezone (America/Denver), so the evening runs (cloud 6:06 pm, PC 8:36 pm)
# open a new day and the cloud's overnight and daytime runs belong to the evening before. A search is due for a
# collector when it is on and that collector has no counted run of it yet, or Cameron turned it on (activeSince)
# after that collector's last run of it, or everyDays search days have passed since that run. A run record counts
# unless it is marked skipped (PC offline, rules unreadable). Each collector keeps its own count, so a PC that was
# off catches up on its next run. Due searches are worked out once per run and kept in .ds-due, so run records
# written later in the same run don't change the answer.
PLAN = ".ds-due"
PLAN_HOURS = 4
SKIP_NOTE = re.compile(r"PC offline|run skipped|rules record unreadable", re.I)


def schedule_cfg(rules):
    sc = (rules or {}).get("schedule") or {}
    ds = num(sc.get("dayStartHour"))
    runs = sc.get("runs") if isinstance(sc.get("runs"), dict) else {}
    return {"tz": str(sc.get("timezone") or "America/Denver"), "dayStart": int(ds) if ds is not None else 17,
            "runs": {k: [str(t) for t in (v if isinstance(v, list) else [v])] for k, v in runs.items()}}


def _us_mountain_offset(dt):
    """UTC offset in hours for America/Denver when zoneinfo has no tz database: MDT (-6) from the second Sunday
    of March 2 am to the first Sunday of November 2 am, MST (-7) otherwise."""
    y = dt.year
    mar = datetime(y, 3, 8, tzinfo=timezone.utc)
    start = mar + timedelta(days=(6 - mar.weekday()) % 7, hours=9)       # 2 am MST = 09:00 UTC
    nov = datetime(y, 11, 1, tzinfo=timezone.utc)
    end = nov + timedelta(days=(6 - nov.weekday()) % 7, hours=8)         # 2 am MDT = 08:00 UTC
    return -6 if start <= dt < end else -7


def to_local(dt, tzname):
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(tzname)).replace(tzinfo=None)
    except Exception:
        off = _us_mountain_offset(dt) if tzname in ("America/Denver", "US/Mountain", "MST7MDT") else 0
        return (dt + timedelta(hours=off)).replace(tzinfo=None)


def search_day(when, cfg):
    """The search day (a date) an ISO time or aware datetime falls in."""
    dt = when if isinstance(when, datetime) else parse_iso(when)
    if not dt:
        return None
    return (to_local(dt, cfg["tz"]) - timedelta(hours=cfg["dayStart"])).date()


def every_days(s):
    n = num((s or {}).get("everyDays"))
    return max(1, min(30, int(n))) if n else 1


def cycle_hours(s):
    """Extra hours a search that runs every few days gets on its freshness and connection windows."""
    return (every_days(s) - 1) * 24


def search_on(s):
    return (s or {}).get("active") is not False


def run_counts(r):
    return not (r.get("skipped") is True or SKIP_NOTE.search(str(r.get("note") or "")))


def last_run(runs, sid, collector=None):
    best = None
    for r in runs or []:
        if r.get("searchId") != sid or (collector and r.get("collector") != collector) or not run_counts(r):
            continue
        if best is None or str(r.get("at") or "") > str(best.get("at") or ""):
            best = r
    return best


def due_info(sid, s, runs, collector, cfg, now=None):
    now = now or datetime.now(timezone.utc)
    n = every_days(s)
    out = {"on": search_on(s), "every": n, "due": False, "why": "", "last": None, "nextDay": None}
    if not out["on"]:
        out["why"] = "off"
        return out
    last = last_run(runs, sid, collector)
    if not last:
        out.update(due=True, why="first run" if not collector else f"no {collector} run of it yet")
        return out
    out["last"] = last.get("at")
    since = s.get("activeSince")
    d0, today = search_day(last.get("at"), cfg), search_day(now, cfg)
    nxt = d0 + timedelta(days=n) if d0 else today
    out["nextDay"] = nxt.isoformat() if nxt else None
    every = "every day" if n == 1 else f"every {n} days"
    t = parse_iso(last.get("at"))
    when = to_local(t, cfg["tz"]).strftime("%a %b %d %I:%M %p").replace(" 0", " ") if t else "?"
    if since and str(last.get("at") or "") < str(since):
        out.update(due=True, why=f"turned on since its last run ({when})")
    elif today and nxt and today >= nxt:
        out.update(due=True, why=f"{every}, last run {when}")
    else:
        out["why"] = f"{every}, last run {when}; next due {nxt:%a %b %d} from {cfg['dayStart'] % 12 or 12} {'pm' if cfg['dayStart'] >= 12 else 'am'}".replace(" 0", " ")
    return out


def run_plan(st, who=None, fresh=False):
    """This run's due searches for collector `who` (default: this collector). Worked out on the run's first
    `ds summary` or `ds due` and kept in .ds-due for PLAN_HOURS; a build chat (no collector) gets it unsaved."""
    who = who or COLLECTOR
    p = st.path(PLAN)
    if who and who == COLLECTOR and not fresh and os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                plan = json.load(f)
            if plan.get("collector") == who and hours_since(plan.get("at") or "") < PLAN_HOURS:
                return plan
        except Exception:
            pass
    cfg = schedule_cfg(st.rules())
    runs = st.runs()
    info = {sid: due_info(sid, s, runs, who, cfg) for sid, s in st.searches().items()}
    plan = {"at": now_iso(), "collector": who, "due": [sid for sid, i in info.items() if i["due"]], "info": info}
    if who and who == COLLECTOR:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=1)
    return plan


def scope_route(st, collector, site, route):
    """A route covers only the searches this run read (v2.7): with no `searches`, ds fills in this run's due
    searches that use the site whenever that is not all of them; a given list keeps only due searches. So a
    search that is off or not due today never looks read, and its listings don't drift to probably gone."""
    plan = run_plan(st, collector)
    users = [sid for sid, s in st.searches().items() if site in (s.get("sources") or [])]
    if isinstance(route.get("searches"), list):
        route["searches"] = [x for x in route["searches"] if x in plan["due"]]
    elif users:
        due_users = [x for x in users if x in plan["due"]]
        if set(due_users) != set(users):
            route["searches"] = due_users
    return route


def schedule_line(sid, s, info):
    if not info["on"]:
        return "OFF"
    every = "every day" if info["every"] == 1 else f"every {info['every']} days"
    return f"on, {every}, " + ("DUE this run (" + info["why"] + ")" if info["due"] else "not due (" + info["why"] + ")")


# ---------------------------------------------------------------- mutations (each is replayable)
CONFIRMED = "Confirmed by Cameron"


def op_upsert(st, items, at):
    if isinstance(items, dict):
        items = [items]
    pasted = st.pasted()
    changed = 0
    for inc in items:
        inc = {k: v for k, v in inc.items() if k not in ("status", "specsAuto")}  # status is Cameron's; specsAuto is ds.py's
        inc_specs = inc.pop("specs", None)
        lid = inc.pop("id", None)
        if not lid:
            die("upsert: every listing needs an id")
        rel, m = st.find(lid)
        if m is None:
            for f in ("searchId", "source", "url", "title"):
                if not inc.get(f):
                    die(f"upsert {lid}: a new listing needs {f}")
            if inc["searchId"] not in st.searches():
                die(f"upsert {lid}: unknown searchId {inc['searchId']}")
            rel = f"listings/{inc['searchId']}.json"
            m = st.read(rel, {}) or {}
            new = {k: v for k, v in inc.items()}
            new.setdefault("foundAt", at)
            new.setdefault("lastSeen", at)
            new.setdefault("priceHistory", [])
            if lid in pasted:
                p = pasted[lid]
                new.setdefault("pasted", p.get("pasted"))
                new.setdefault("via", "paste")
            m[lid] = new
        else:
            cur = m[lid]
            locked = CONFIRMED in str(cur.get("note") or "")
            old_price = num(cur.get("price"))
            new_price = num(inc.get("price"))
            if new_price is not None and old_price is not None and new_price != old_price:
                hist = list(cur.get("priceHistory") or [])
                hist.append({"price": cur.get("price"), "at": at})
                cur["priceHistory"] = hist
                cur["priceChangedAt"] = at
            for k, v in inc.items():
                if v is None:
                    continue
                if k == "foundAt" and cur.get("foundAt"):
                    cur["foundAt"] = min(cur["foundAt"], v)
                    continue
                if locked and k in ("miles", "titleStatus", "note"):
                    continue
                if k == "check" and CONFIRMED.lower() in str((cur.get("check") or {}).get("summary") or "").lower():
                    continue
                if k == "priceHistory":
                    continue
                cur[k] = v
            if "lastSeen" not in inc:
                cur["lastSeen"] = at
        # v2.6 specs: what a collector sends wins key by key and leaves specsAuto; ds.py fills the rest from the title.
        cur = m[lid]
        if isinstance(inc_specs, dict):
            sp, auto = dict(cur.get("specs") or {}), set(cur.get("specsAuto") or [])
            for k, v in inc_specs.items():
                if v is None:
                    continue
                sp[k] = v
                auto.discard(k)
            if inc_specs.get("waistMm") is not None and "waistFrom" not in inc_specs and "waistFrom" in auto:
                sp.pop("waistFrom", None)
                auto.discard("waistFrom")
            cur["specs"] = sp
            if auto:
                cur["specsAuto"] = sorted(auto)
            else:
                cur.pop("specsAuto", None)
        fill_specs(cur, st.searches().get(cur.get("searchId")))
        st.write(rel, m)
        if lid in pasted:
            del pasted[lid]
            st.write("pasted.json", pasted)
        changed += 1
    return f"{changed} listing(s) stored"


def op_check(st, lid, chk, at):
    rel, m = st.find(lid)
    if m is None:
        die(f"check: no listing {lid}")
    cur = m[lid].get("check") or {}
    if CONFIRMED.lower() in str(cur.get("summary") or "").lower():
        return f"{lid}: check confirmed by Cameron, left alone"
    if chk.get("status") not in ("passed", "caution", "failed", "pending"):
        die("check: status must be passed, caution, failed or pending")
    chk.setdefault("at", at)
    if COLLECTOR:
        chk.setdefault("by", COLLECTOR)
    m[lid]["check"] = chk
    st.write(rel, m)
    return f"{lid}: check {chk['status']} (level {chk.get('level')})"


def op_gone(st, lid, note, at):
    rel, m = st.find(lid)
    if m is None:
        die(f"gone: no listing {lid}")
    m[lid].update({"gone": True, "goneAt": at, "lastChecked": at})
    if note:
        m[lid]["note"] = note
    st.write(rel, m)
    return f"{lid}: marked gone"


def op_alerted(st, lid, when, at):
    rel, m = st.find(lid)
    if m is None:
        die(f"alerted: no listing {lid}")
    m[lid]["alertedAt"] = when or at
    st.write(rel, m)
    return f"{lid}: alertedAt {m[lid]['alertedAt']}"


ROUTE_STATES = {"ok", "partial", "auth", "blocked", "error", "setup", "off"}


def op_route(st, collector, site, route, at):
    if collector not in ("cloud", "pc"):
        die("route: collector must be cloud or pc")
    if COLLECTOR and collector != COLLECTOR:
        die(f"route: you are the {COLLECTOR} collector; you may only write the {COLLECTOR} route")
    if route.get("state") not in ROUTE_STATES:
        die("route: state must be one of " + ", ".join(sorted(ROUTE_STATES)))
    if "searches" in route and not (isinstance(route["searches"], list) and all(isinstance(x, str) for x in route["searches"])):
        die("route: searches must be a list of search ids (leave it out when the read covered every search)")
    sites = st.sites()
    s = sites.setdefault(site, {})
    prev = s.get(collector) or {}
    route.setdefault("at", at)
    state = route["state"]
    # failStreak: runs in a row that were not "ok" (an "off" route is not in use, so it resets).
    # blockStreak (v2.3): blocks in a row; an ok or partial read ends it, other states leave it as it was.
    # Only blocks set a pause: numbers.blockPauseHours x 2^(blockStreak - 1), capped at blockPauseMaxHours.
    # A route rewritten while its pause is still running keeps the pause (the run skipped the site).
    prev_pause = prev.get("pausedUntil")
    t_at, t_pause = parse_iso(route["at"]), parse_iso(prev_pause)
    pause_running = bool(t_at and t_pause and t_pause > t_at)
    if state == "ok":
        route.setdefault("lastOk", at)
        route.setdefault("failStreak", 0)
        route.setdefault("blockStreak", 0)
    else:
        route.setdefault("lastOk", prev.get("lastOk"))
        route.setdefault("failStreak", 0 if state == "off" else int(prev.get("failStreak") or 0) + 1)
        if state in ("partial", "off"):
            route.setdefault("blockStreak", 0)
        elif state == "blocked" and not pause_running:
            nums = st.rules().get("numbers", {})
            bs = route.setdefault("blockStreak", int(prev.get("blockStreak") or 0) + 1)
            if "pausedUntil" not in route and t_at:
                hours = min(float(nums.get("blockPauseHours", 24)) * 2 ** (max(int(bs), 1) - 1), float(nums.get("blockPauseMaxHours", 168)))
                route["pausedUntil"] = (t_at + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            route.setdefault("blockStreak", int(prev.get("blockStreak") or (1 if pause_running else 0)))
            if pause_running:
                route.setdefault("pausedUntil", prev_pause)
    # Known issue 9c (v2.1): a cloud site whose pages stay refused even when opened from a search result
    # for 3 runs in a row gets flagged for a move to the PC. Cameron or a build chat changes `how`.
    if collector == "cloud" and route.get("errorKind") == "refused":
        route["refusedStreak"] = int(prev.get("refusedStreak") or 0) + 1
        if route["refusedStreak"] >= 3:
            route["movePc"] = True
    else:
        route.pop("refusedStreak", None)
        route.pop("movePc", None)
    s[collector] = route
    st.write("sites.json", sites)
    return f"{site}: {collector} route {route['state']}"


def op_site(st, site, facts, at):
    if not re.fullmatch(r"[a-z0-9]+", site):
        die("site: id must be lowercase letters and digits")
    sites = st.sites()
    s = sites.setdefault(site, {})
    for k, v in facts.items():
        if k in ("cloud", "pc"):
            die("site: write routes with ds.py route")
        s[k] = v
    st.write("sites.json", sites)
    return f"{site}: facts saved"


def op_search(st, sid, patch, at):
    ss = st.searches()
    if sid not in ss:
        die(f"search: no search {sid} (collectors never create searches)")
    for k in ("id", "createdAt"):
        patch.pop(k, None)
    ss[sid].update(patch)
    ss[sid]["updatedAt"] = at
    st.write("searches.json", ss)
    return f"{sid}: updated {', '.join(sorted(patch))}"


def op_run(st, run, at):
    for f in ("searchId", "collector", "newCount", "note"):
        if f not in run:
            die(f"run: missing {f}")
    run.setdefault("at", at)
    run.setdefault("emailed", False)
    # v2.7: a run that didn't search (PC offline, rules unreadable) is marked skipped, so the search stays due.
    if SKIP_NOTE.search(str(run.get("note") or "")):
        run.setdefault("skipped", True)
    stamp = run["at"][:16].replace(":", "")
    run.setdefault("id", f"{stamp}-{run['searchId']}-{run['collector']}")
    runs = [r for r in st.runs() if r.get("id") != run["id"]]
    runs.append(run)
    runs.sort(key=lambda r: r.get("at", ""), reverse=True)
    keep = int(st.rules().get("numbers", {}).get("runsKeep", 300))
    st.write("runs.json", runs[:keep])
    return f"run {run['id']} recorded"


def op_specs(st, sid, at):
    """Fill specs from titles for every listing of a kind with a title parser (all such searches, or one)."""
    ss = st.searches()
    if sid and sid not in ss:
        die(f"specs: no search {sid}")
    n = 0
    for s_id, s in ss.items():
        if (sid and s_id != sid) or not kind_of(s).get("parse"):
            continue
        rel = f"listings/{s_id}.json"
        m = st.read(rel, {}) or {}
        changed = sum(1 for l in m.values() if fill_specs(l, s))
        if changed:
            st.write(rel, m)
        n += changed
    return f"specs filled from titles on {n} listing(s)"


def op_unpaste(st, lid, at):
    p = st.pasted()
    if lid in p:
        del p[lid]
        st.write("pasted.json", p)
        return f"{lid}: removed from pasted"
    return f"{lid}: not in pasted"


def op_beat(st, collector, beat, at):
    """v2.8: a collector's check-in. One entry per collector in heartbeat.json, replaced by each check-in, so the
    page can show when each collector last ran and what came of it even when nothing was due (no run records)."""
    if collector not in ("cloud", "pc"):
        die("beat: collector must be cloud or pc")
    if beat.get("result") not in BEAT_RESULTS:
        die("beat: result must be one of " + ", ".join(BEAT_RESULTS))
    hb = dict(st.heartbeat())
    b = dict(beat)
    b["at"] = at
    hb[collector] = b
    st.write(HEART, hb)
    return f"{collector} check-in recorded: {b['result']}" + (f" ({b.get('note')})" if b.get("note") else "")


OPS_FN = {
    "upsert": lambda st, a, at: op_upsert(st, a["items"], at),
    "check": lambda st, a, at: op_check(st, a["id"], a["check"], at),
    "gone": lambda st, a, at: op_gone(st, a["id"], a.get("note"), at),
    "alerted": lambda st, a, at: op_alerted(st, a["id"], a.get("when"), at),
    "route": lambda st, a, at: op_route(st, a["collector"], a["site"], a["route"], at),
    "site": lambda st, a, at: op_site(st, a["site"], a["facts"], at),
    "search": lambda st, a, at: op_search(st, a["id"], a["patch"], at),
    "run": lambda st, a, at: op_run(st, a["run"], at),
    "unpaste": lambda st, a, at: op_unpaste(st, a["id"], at),
    "specs": lambda st, a, at: op_specs(st, a.get("id"), at),
    "beat": lambda st, a, at: op_beat(st, a["collector"], a["beat"], at),
}


def record(st, name, args):
    at = now_iso()
    msg = OPS_FN[name](st, json.loads(json.dumps(args)), at)
    with open(st.path(OPS), "a", encoding="utf-8") as f:
        f.write(json.dumps({"op": name, "args": args, "at": at}) + "\n")
    print(msg)


def pending_ops(st):
    p = st.path(OPS)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def make_beat(st, result=None, note=None):
    """This run's check-in: when it started (the run's first ds summary or ds due), what was due, the run records
    it wrote, and the result. With no result given: worked when it wrote counted run records, skipped when every
    record it wrote was skipped (PC offline, rules unreadable), nothing-due when nothing was due."""
    plan = run_plan(st)
    mine = [r for r in st.runs() if r.get("collector") == COLLECTOR and str(r.get("at") or "") >= str(plan.get("at") or "")]
    if result is None:
        if any(run_counts(r) for r in mine):
            result = "worked"
        elif mine:
            result = "skipped"
        elif not plan["due"]:
            result = "nothing-due"
        else:
            die("beat: searches were due (" + ", ".join(plan["due"]) + ") but this run wrote no run records. "
                "Write them first, or say the result: ds beat worked|skipped \"<note>\"")
    if result not in BEAT_RESULTS:
        die("beat: result must be worked, nothing-due or skipped")
    if not note:
        note = {"worked": "Searched " + ", ".join(plan["due"]) if plan["due"] else "Worked",
                "nothing-due": "Nothing due this run", "skipped": next((str(r.get("note")) for r in mine), "Run skipped"),
                "running": "Run in progress"}[result]
    b = {"startedAt": plan.get("at"), "result": result, "note": str(note)[:300], "due": list(plan["due"]),
         "runs": len(mine), "rules": str(st.rules().get("version") or "")}
    nd = {sid: i.get("why") or "not due" for sid, i in (plan.get("info") or {}).items() if not i.get("due")}
    if nd:
        b["notDue"] = nd
    return b


def cmd_beat(st, argv):
    if COLLECTOR not in ("cloud", "pc"):
        die("beat: only a collector run checks in (dealscout-data/.ds-collector says cloud or pc)")
    result = argv[0] if argv and argv[0] in BEAT_RESULTS[:3] else None
    rest = argv[1:] if result else argv
    record(st, "beat", {"collector": COLLECTOR, "beat": make_beat(st, result, " ".join(rest).strip() or None)})


# ---------------------------------------------------------------- git
def git(st, *args, check=True):
    r = subprocess.run(["git", "-C", st.root] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        die(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r


def ensure_ignore(st):
    gi = st.path(".gitignore")
    lines = open(gi).read().splitlines() if os.path.exists(gi) else []
    for want in (OPS, REPLAY, "*.tmp", ".ds-collector", PLAN):
        if want not in lines:
            lines.append(want)
    with open(gi, "w") as f:
        f.write("\n".join(lines) + "\n")


def replay(st, ops):
    """Put origin/main in place and re-apply every recorded change on top. A change that no longer
    applies (its listing or search was removed meanwhile) is skipped and reported; it never stops the
    replay halfway. The .ds-replay marker exists only while this runs, so a run that dies in here is
    redone from the start by the next commit instead of pushing a half-replayed copy."""
    with open(st.path(REPLAY), "w") as f:
        f.write(now_iso() + "\n")
    git(st, "reset", "-q", "--hard", "origin/main")
    st.reset_cache()
    skipped = []
    for o in ops:
        try:
            OPS_FN[o["op"]](st, json.loads(json.dumps(o["args"])), o["at"])
        except (SystemExit, Exception) as e:  # die() raises SystemExit
            skipped.append(f"{o['op']} {json.dumps(o['args'], ensure_ascii=False)[:100]}")
            if not isinstance(e, SystemExit):
                print(f"ds.py: {o['op']} failed on replay: {e}", file=sys.stderr)
    ensure_ignore(st)
    os.remove(st.path(REPLAY))
    for x in skipped:
        print("ds.py: skipped, no longer applies: " + x, file=sys.stderr)
    return skipped


def commit(st, message):
    ops = pending_ops(st)
    if not ops:
        print("nothing to commit")
        return
    # v2.8: a collector's save partway through a run also checks in as "running", unless this run's final
    # check-in (ds beat) is already recorded, so the page shows a run that is going or one that stopped partway.
    if COLLECTOR in ("cloud", "pc") and not any(o.get("op") == "beat" for o in ops):
        hb = st.heartbeat().get(COLLECTOR) or {}
        plan = run_plan(st)
        if not (hb.get("startedAt") == plan.get("at") and hb.get("result") != "running"):
            b = make_beat(st, "running", f"Run in progress: saved {len(ops)} change(s) ({message[:80]})")
            record(st, "beat", {"collector": COLLECTOR, "beat": b})
            ops = pending_ops(st)
    who = COLLECTOR or "build"
    git(st, "config", "user.name", f"Deal Scout {who}")
    git(st, "config", "user.email", "noreply@anthropic.com")
    skipped = []
    if os.path.exists(st.path(REPLAY)):
        print("ds.py: the last commit stopped partway through a replay; replaying again from origin/main", file=sys.stderr)
        skipped = replay(st, ops)
    ensure_ignore(st)
    msg = f"[{who}] {message}"
    for attempt in range(1, 7):
        git(st, "add", "-A")
        if git(st, "diff", "--cached", "--quiet", check=False).returncode != 0:
            git(st, "commit", "-q", "-m", msg)
        ahead = git(st, "rev-list", "--count", "origin/main..HEAD", check=False)
        if ahead.returncode == 0 and ahead.stdout.strip() == "0":
            os.remove(st.path(OPS))
            print(f"nothing new to push: {len(ops)} recorded change(s) already match origin/main"
                  + (f" ({len(skipped)} skipped)" if skipped else ""))
            return
        r = git(st, "push", "-q", "origin", "HEAD:main", check=False)
        landed = r.returncode == 0
        if not landed:
            # a push can report an error and still land; never replay twice
            git(st, "fetch", "-q", "--depth=50", "origin", "main", check=False)
            landed = git(st, "merge-base", "--is-ancestor", "HEAD", "origin/main", check=False).returncode == 0
        if landed:
            os.remove(st.path(OPS))
            print(f"pushed: {msg} ({len(ops) - len(skipped)} change(s)"
                  + (f", {len(skipped)} skipped" if skipped else "") + f", attempt {attempt})")
            return
        # someone pushed first: take their copy and replay our changes on top
        time.sleep(2 * attempt)
        git(st, "fetch", "-q", "--depth=50", "origin", "main")
        skipped = replay(st, ops)
    die("push still rejected after 6 tries; changes are kept in .ds-ops.jsonl, run ds.py commit again", 1)


def pull(st):
    if pending_ops(st):
        die("there are uncommitted changes; run ds.py commit first")
    git(st, "fetch", "-q", "--depth=50", "origin", "main")
    git(st, "reset", "-q", "--hard", "origin/main")
    print("up to date with origin/main")


# ---------------------------------------------------------------- reports
def money(x):
    v = num(x)
    return "-" if v is None else f"${v:,.0f}"


def line(l, sc=None):
    ck = l.get("check") or {}
    bits = [l["id"], money(l.get("price")), (l.get("title") or "")[:60], l.get("location") or "?"]
    if sc and sc.get("homeDist") is not None:
        bits.append(f"{sc['homeDist']} mi")
    if sc and sc.get("pct") is not None:
        bits.append(f"{sc['pct']*100:.0f}% under" if sc["pct"] >= 0 else f"{-sc['pct']*100:.0f}% over")
    if sc and sc.get("shipEst"):
        bits.append(f"+{money(sc['shipEst'])} ship est.")
    sp = l.get("specs") or {}
    if sp:
        w = f"{sp['waistMm']}mm" + ("(model name)" if sp.get("waistFrom") == "model name" else "") if sp.get("waistMm") else "no waist"
        bits.append("[" + " ".join(str(x) for x in (f"{sp['lengthCm']}cm" if sp.get("lengthCm") else "no length", w,
                                                     sp.get("bindingType") or ("flat" if sp.get("bindings") == "none" else "binding?"))) + "]")
    bits.append(f"check {ck.get('status', 'none')}{('/L' + str(ck.get('level'))) if ck.get('level') else ''}")
    if sc and sc.get("isDeal"):
        bits.append("DEAL")
    elif sc and sc.get("reconfirm"):
        bits.append("price needs re-confirming")
    elif sc and sc.get("concern"):
        bits.append("caution (settled)")
    elif sc and sc.get("lead"):
        bits.append(f"needs L{sc['needLevel']} check")
    if l.get("gone"):
        bits.append("GONE")
    elif sc and sc.get("probablyGone") is not None:
        bits.append(f"PROBABLY GONE (unseen {sc['probablyGone']} d)")
    elif sc and sc.get("outside"):
        bits.append("OUTSIDE SEARCH (" + ", ".join(sc["outside"]) + ")")
    if l.get("status") not in (None, "new"):
        bits.append(l["status"])
    bits.append(f"seen {str(l.get('lastSeen') or '')[:16]}")
    return " | ".join(str(b) for b in bits)


def cmd_summary(st):
    r = st.rules()
    nums = r.get("numbers", {})
    L = st.all_listings()
    print(f"rules v{r.get('version')} · {len(st.searches())} searches · {len(L)} listings · {len(st.pasted())} pasted links")
    plans = {COLLECTOR: run_plan(st)} if COLLECTOR else {c: run_plan(st, c) for c in ("cloud", "pc")}
    for who, plan in plans.items():
        print(f"\nTHIS RUN ({who}): " + (f"due: {', '.join(plan['due'])}" if plan["due"] else "NOTHING DUE: no search is on and due for this collector (still check in: ds beat nothing-due, then ds commit)")
              + "  (ds due shows why; work only on due searches)")
    for sid, s in st.searches().items():
        mine = {k: v for k, v in L.items() if v.get("searchId") == sid}
        mk = market_for(s, sid, L, nums)
        sites = st.sites()
        live = [(l, score(l, s, mk, nums)) for l in mine.values() if not l.get("gone") and l.get("status") != "passed"]
        pg = [l for l, sc in live if probably_gone(l, sc, sites, nums) is not None]
        outs = [l for l, sc in live if probably_gone(l, sc, sites, nums) is None and outside_hidden(l, sc)]
        scored = [(l, sc) for l, sc in live if probably_gone(l, sc, sites, nums) is None and not outside_hidden(l, sc)]
        open_ = [l for l, _ in scored]
        leads = [x for x in scored if x[1]["photoWork"] or x[1]["reconfirm"]]
        concerns = [x for x in scored if x[1]["concern"]]
        deals = [x for x in scored if x[1]["isDeal"]]
        pend = [l for l in open_ if (l.get("check") or {}).get("status") == "pending"]
        stale = [l for l, sc in scored if refresh_due(l, sc, nums)[0]]
        sched = " · ".join(f"{who}: {schedule_line(sid, s, plan['info'].get(sid) or due_info(sid, s, st.runs(), who, schedule_cfg(r)))}" for who, plan in plans.items())
        print(f"\n[{sid}] {s.get('name')} · kind {kind_key(s)} · sources {','.join(s.get('sources') or [])}")
        print(f"  schedule: {sched}")
        lim = limits_text(s)
        if lim:
            print(f"  limits: {lim}")
        print(f"  {len(open_)} open · market {money(mk['value']) if mk['value'] else '-'} ({mk['basis']}) · "
              f"{len(deals)} deals · {len(leads)} deal-priced needing checks · {len(concerns)} with concerns (caution) · "
              f"{len(pend)} pending checks · {len(stale)} due for refresh · {len(pg)} probably gone (hidden) · "
              f"{len(outs)} outside the search's limits (hidden)")
    print("\nSITES (route state, last read)")
    for sid, s in sorted(st.sites().items()):
        rs = []
        for c in ("cloud", "pc"):
            x = s.get(c)
            if x:
                rs.append(f"{c}:{x.get('state')}@{str(x.get('at') or '')[:16]}")
        print(f"  {sid:14} how={s.get('how', '?'):5} " + "  ".join(rs))
    print("\nCOLLECTORS (last check-in, heartbeat.json; every run checks in, even with nothing due)")
    hb = st.heartbeat()
    for c in ("cloud", "pc"):
        b = hb.get(c)
        print(f"  {c:5} " + (f"{b.get('at')} {b.get('result')}: {str(b.get('note') or '')[:100]}" if isinstance(b, dict) else "no check-in yet"))
    print("\nLAST RUNS")
    seen = set()
    for run in st.runs():
        key = (run.get("collector"), run.get("searchId"))
        if key in seen:
            continue
        seen.add(key)
        print(f"  {run.get('at')} {str(run.get('collector')):5} {run.get('searchId')}: {str(run.get('note'))[:110]}")
    ops = pending_ops(st)
    if ops:
        print(f"\n{len(ops)} change(s) not yet committed")


def limits_text(s):
    """The search's kind limits in words, e.g. "models Blizzard Brahma 88; lengthCm 170-185; bindings yes"."""
    bits = []
    if s.get("models"):
        bits.append("models " + ", ".join(str(m) for m in s["models"]))
    for k, v in sorted((s.get("specLimits") or {}).items()):
        if isinstance(v, dict) and (v.get("min") is not None or v.get("max") is not None):
            bits.append(f"{k} {v.get('min') if v.get('min') is not None else 'any'}-{v.get('max') if v.get('max') is not None else 'any'}")
        elif isinstance(v, list) and v:
            bits.append(f"{k} {'/'.join(map(str, v))}")
    est = num(kind_of(s).get("shipEstimate"))
    if est:
        bits.append(f"ship-in with no shipping price +{money(est)}")
    if kind_of(s).get("nearFirst"):
        bits.append("pickup first")
    return "; ".join(bits)


def cmd_listings(st, argv):
    if not argv:
        die("listings <searchId> [--source S] [--open] [--pending] [--leads] [--stale] [--json]  (--open leaves out gone, passed, probably gone and outside the search's limits)")
    sid = argv[0]
    s = st.searches().get(sid) or die(f"no search {sid}")
    nums = st.rules().get("numbers", {})
    L = st.all_listings()
    mk = market_for(s, sid, L, nums)
    sites = st.sites()
    rows = []
    for l in L.values():
        if l.get("searchId") != sid:
            continue
        if "--source" in argv and l.get("source") != argv[argv.index("--source") + 1]:
            continue
        sc = score(l, s, mk, nums)
        sc["probablyGone"] = probably_gone(l, sc, sites, nums)
        open_ = not l.get("gone") and l.get("status") != "passed" and sc["probablyGone"] is None and not outside_hidden(l, sc)
        if "--open" in argv and not open_:
            continue
        if "--pending" in argv and (l.get("check") or {}).get("status") != "pending":
            continue
        if "--leads" in argv and not (sc["lead"] and open_):
            continue
        if "--stale" in argv and not (open_ and refresh_due(l, sc, nums)[0]):
            continue
        rows.append((l, sc))
    rows.sort(key=lambda x: (x[1]["pct"] is None, -(x[1]["pct"] or 0)))
    if "--json" in argv:
        print(json.dumps([dict(l, _score=sc) for l, sc in rows], indent=1, ensure_ascii=False))
    else:
        for l, sc in rows:
            print(line(l, sc))
        print(f"({len(rows)} listings)")


def refresh_due(l, sc, nums):
    """Hours after which an open listing is due for a price refresh: numbers.refreshHours, or
    numbers.refreshHoursSlow when it is priced more than numbers.slowOverPct% over its reference."""
    slow = sc["pct"] is not None and sc["pct"] < -nums.get("slowOverPct", 15) / 100
    limit = nums.get("refreshHoursSlow", 72) if slow else nums.get("refreshHours", 12)
    return hours_since(l.get("lastChecked") or l.get("lastSeen") or "") > limit, slow


def collector_sources(st, who):
    if not who:
        return None
    return {k for k, v in st.sites().items() if (v.get("how") or "cloud") == who}


def cmd_queue(st, argv):
    """queue <searchId> refresh|photos [--for cloud|pc] [--all] [--json]
    refresh: open listings due for a price refresh, in the rules' order, capped at numbers.maxRefreshPerRun.
    photos:  deal-priced listings that need the PC's photo check or a price re-confirm, best deal first."""
    if len(argv) < 2 or argv[1] not in ("refresh", "photos"):
        die("queue <searchId> refresh|photos [--for cloud|pc] [--all] [--json]")
    sid, kind = argv[0], argv[1]
    s = st.searches().get(sid) or die(f"no search {sid}")
    # v2.7: a collector gets no work for a search that is off or not due this run.
    nd = not_due_here(st, sid)
    if nd:
        print("[]" if "--json" in argv else f"(0: {sid} is {nd}; skip it this run)")
        return
    nums = st.rules().get("numbers", {})
    L = st.all_listings()
    mk = market_for(s, sid, L, nums)
    who = argv[argv.index("--for") + 1] if "--for" in argv else None
    srcs = collector_sources(st, who) if kind == "refresh" else None
    sites = st.sites()
    rows = []
    for l in L.values():
        if l.get("searchId") != sid or l.get("gone") or l.get("status") == "passed":
            continue
        if srcs is not None and l.get("source") not in srcs:
            continue
        sc = score(l, s, mk, nums)
        if kind == "photos":
            if not (sc["photoWork"] or sc["reconfirm"]):
                continue
            why = "re-confirm price" if sc["reconfirm"] else (f"L{sc['needLevel']} check" + ("" if l.get("verified") is not False else ", ad not opened yet"))
            # v2.6: for a kind with nearFirst (skis), listings within radiusMi come before ship-in ones.
            far = 1 if (sc["nearFirst"] and not sc["inRadius"]) else 0
            rows.append((l, sc, (0 if sc["reconfirm"] else 1, far, -(sc["pct"] or 0), sc["homeDist"] if sc["homeDist"] is not None else 1e9), why))
        else:
            # Order (v2.1): deal-priced leads, then listings Cameron marked, then everything else that is due,
            # oldest first. Unverified listings count as due but no longer jump the queue: shop cards the
            # reader can't open were filling every slot.
            # v2.5: a listing outside the search's limits isn't refreshed unless Cameron marked it.
            if outside_hidden(l, sc):
                continue
            due, slow = refresh_due(l, sc, nums)
            unv = l.get("verified") is False
            if not (due or sc["lead"] or unv):
                continue
            pg = probably_gone(l, sc, sites, nums)
            # v2.4: probably-gone listings come last; re-opening one that is still live brings it back.
            tier = 0 if sc["lead"] else 1 if l.get("status") in ("watching", "contacted") else 3 if pg is not None else 2
            if tier == 1 and not due:
                continue
            last = l.get("lastChecked") or l.get("lastSeen") or ""
            why = ["deal-priced", "you marked it", "unverified" if unv else ("slow tier" if slow else "due"), f"probably gone, unseen {pg} d"][tier]
            rows.append((l, sc, (tier, last), why))
    rows.sort(key=lambda x: x[2])
    total = len(rows)
    if kind == "refresh" and "--all" not in argv:
        rows = rows[: int(nums.get("maxRefreshPerRun", 25))]
    if "--json" in argv:
        print(json.dumps([dict(l, _score=sc, _why=why) for l, sc, _, why in rows], indent=1, ensure_ascii=False))
        return
    for l, sc, _, why in rows:
        print(line(l, sc) + f" | {why}")
    print(f"({len(rows)} of {total} {'due for refresh' if kind == 'refresh' else 'needing photo checks or a price re-confirm'})")


def cmd_due(st, argv):
    """due [--for cloud|pc] [--json]: this run's plan. For a collector it is worked out once, at the run's start."""
    who = argv[argv.index("--for") + 1] if "--for" in argv else COLLECTOR or None
    if who not in (None, "cloud", "pc"):
        die("due [--for cloud|pc] [--json]")
    whos = [who] if who else ["cloud", "pc"]
    plans = {w: run_plan(st, w) for w in whos}
    if "--json" in argv:
        print(json.dumps(plans[whos[0]] if len(whos) == 1 else plans, indent=1))
        return
    ss = st.searches()
    for w, plan in plans.items():
        print(f"{w} run, planned {plan['at']}:")
        for sid, s in ss.items():
            i = plan["info"].get(sid) or {}
            print(f"  {sid:22} {schedule_line(sid, s, i) if i else '?'}" + (f" · last {w} run {str(i.get('last'))[:16]}" if i.get("last") else ""))
        print(f"DUE THIS RUN ({w}): " + (" ".join(plan["due"]) if plan["due"] else "nothing. Read pasted links only, write no run records, then check in (ds beat nothing-due) and commit."))


def not_due_here(st, sid):
    """For a collector: why a search gets no work this run (off or not due), or None when it is due."""
    if not COLLECTOR:
        return None
    plan = run_plan(st)
    if sid in plan["due"]:
        return None
    why = (plan["info"].get(sid) or {}).get("why") or "not due"
    return "off" if why == "off" else "not due this run: " + why


def cmd_score(st, sid):
    s = st.searches().get(sid) or die(f"no search {sid}")
    nd = not_due_here(st, sid)
    if nd:
        print(f"(0: {sid} is {nd}; no checks or deal emails for it this run)")
        return
    nums = st.rules().get("numbers", {})
    L = st.all_listings()
    mk = market_for(s, sid, L, nums)
    print(f"{s.get('name')}: market {money(mk['value']) if mk['value'] else '-'} ({mk['basis']}); deal = "
          f"{num(s.get('dealPct')) or nums.get('dealPctDefault')}% under, ceiling {money(s.get('maxPrice'))}, near-you {s.get('radiusMi')} mi of {s.get('zip')}")
    n = outs = 0
    for l in L.values():
        if l.get("searchId") != sid or l.get("gone") or l.get("status") == "passed":
            continue
        sc = score(l, s, mk, nums)
        if outside_hidden(l, sc):
            outs += 1
            continue
        if sc["underMarket"] or sc["isDeal"]:
            n += 1
            print(line(l, sc) + f" | ref {money(sc['ref'])} cost {money(sc['cost'])}"
                  f"{' | branded' if sc['branded'] else ''}{'' if sc['fresh'] else ' | price not confirmed in ' + str(nums.get('priceFreshHours')) + ' h'}")
    print(f"({n} deal-priced; {outs} outside the search's limits left out, never deals)")


def parse_json_arg(a):
    if a == "-":
        return json.load(sys.stdin)
    if a.startswith("@"):
        with open(a[1:]) as f:
            return json.load(f)
    return json.loads(a)


def main():
    global COLLECTOR
    argv = sys.argv[1:]
    root = find_data(argv)
    st = Store(root)
    set_kinds(st.rules())
    if not COLLECTOR and os.path.exists(st.path(".ds-collector")):
        COLLECTOR = open(st.path(".ds-collector")).read().strip()
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(__doc__)
        return
    c, a = argv[0], argv[1:]
    need = {"get": 1, "score": 1, "zip": 1, "city": 1, "dist": 4, "upsert": 1, "check": 2, "gone": 1,
            "alerted": 1, "route": 3, "site": 2, "search": 2, "run": 1, "unpaste": 1, "listings": 1}
    if len(a) < need.get(c, 0):
        die(usage(c))
    try:
        dispatch(st, c, a)
    except json.JSONDecodeError as e:
        die(f"{c}: that JSON doesn't parse ({e}). {usage(c)}")


def usage(c):
    """The command's line from the list at the top of this file."""
    for ln in __doc__.splitlines():
        t = ln.strip()
        if re.search(r"(^|[;|]\s*)" + re.escape(c) + r"\b", t):
            return "usage: ds.py " + re.sub(r"\s{2,}", "   # ", t)
    return "run ds.py help for the list of commands"


def dispatch(st, c, a):
    if c == "summary":
        cmd_summary(st)
    elif c == "listings":
        cmd_listings(st, a)
    elif c == "get":
        L = st.all_listings()
        print(json.dumps(L.get(a[0]) or die(f"no listing {a[0]}"), indent=1, ensure_ascii=False))
    elif c == "score":
        cmd_score(st, a[0])
    elif c == "queue":
        cmd_queue(st, a)
    elif c == "zip":
        print(json.dumps(zip_coords(a[0])))
    elif c == "city":
        print(json.dumps(city_coords(a[0])))
    elif c == "dist":
        if "--zip" not in a:
            die("dist <lat> <lng> --zip <the search's zip>")
        z = a[a.index("--zip") + 1]
        d = miles_between(zip_coords(z), [float(a[0]), float(a[1])])
        print("null" if d is None else round(d))
    elif c == "upsert":
        record(st, "upsert", {"items": parse_json_arg(a[0])})
    elif c == "check":
        record(st, "check", {"id": a[0], "check": parse_json_arg(a[1])})
    elif c == "gone":
        record(st, "gone", {"id": a[0], "note": a[1] if len(a) > 1 else None})
    elif c == "alerted":
        record(st, "alerted", {"id": a[0], "when": a[1] if len(a) > 1 else None})
    elif c == "route":
        route = parse_json_arg(a[2])
        if COLLECTOR and a[0] == COLLECTOR and isinstance(route, dict):
            route = scope_route(st, a[0], a[1], route)
        record(st, "route", {"collector": a[0], "site": a[1], "route": route})
    elif c == "site":
        record(st, "site", {"site": a[0], "facts": parse_json_arg(a[1])})
    elif c == "search":
        patch = parse_json_arg(a[1])
        if COLLECTOR and isinstance(patch, dict) and set(patch) & {"active", "everyDays", "activeSince"}:
            die("search: active, everyDays and activeSince are Cameron's on/off switch and frequency; collectors never change them")
        record(st, "search", {"id": a[0], "patch": patch})
    elif c == "run":
        run = parse_json_arg(a[0])
        if COLLECTOR and isinstance(run, dict) and run.get("searchId"):
            sid = run["searchId"]
            if sid not in st.searches():
                die(f"run: no search {sid}")
            plan = run_plan(st)
            exempt = run.get("skipped") is True or SKIP_NOTE.search(str(run.get("note") or "")) or re.match(r"\s*(rules v[\d.]+:\s*)?repeat start", str(run.get("note") or ""), re.I)
            if sid not in plan["due"] and not exempt:
                why = (plan["info"].get(sid) or {}).get("why") or "not due"
                die(f"run: {sid} is {'off' if why == 'off' else 'not due this run (' + why + ')'}. Write run records only for the searches ds due lists, and don't search the others.")
        record(st, "run", {"run": run})
    elif c == "due":
        cmd_due(st, a)
    elif c == "beat":
        cmd_beat(st, a)
    elif c == "unpaste":
        record(st, "unpaste", {"id": a[0]})
    elif c == "specs":
        record(st, "specs", {"id": a[0] if a else None})
    elif c == "commit":
        commit(st, a[0] if a else "update")
    elif c == "pull":
        pull(st)
    else:
        die(f"unknown command {c}; run ds.py help")


if __name__ == "__main__":
    main()
