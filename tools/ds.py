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
  search <id> '<json>'             update a search's fields
  run '<json>'                     add a run record
  unpaste <id>                     drop a pasted link once its listing is stored
  commit "<message>"               push everything recorded since the last commit
"""
import json, math, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone

OPS = ".ds-ops.jsonl"
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


def market_for(s, sid, listings, nums):
    if s.get("pricing") == "per-item":
        return {"value": None, "perItem": True, "basis": "each listing against its own model's fair used price"}
    mp = num(s.get("marketPrice"))
    if mp:
        return {"value": mp, "basis": "set by Cameron"}
    prices = sorted(float(l["price"]) for l in listings.values()
                    if l.get("searchId") == sid and l.get("status") != "passed" and not l.get("gone")
                    and not branded(l, nums.get("brandedWords", [])) and (l.get("check") or {}).get("status") != "failed"
                    and num(l.get("price")))
    if not prices:
        return {"value": None, "basis": "no listings yet"}
    n = len(prices)
    med = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
    return {"value": med, "basis": f"median of {n} listing{'s' if n != 1 else ''}"}


def score(l, s, market, nums):
    ref = num(l.get("refPrice")) if s.get("pricing") == "per-item" else market["value"]
    price = num(l.get("price"))
    cost = None if price is None else price + (num(l.get("shipCost")) or 0)
    pct = (ref - cost) / ref if (ref and cost is not None) else None
    thr = (num(s.get("dealPct")) if num(s.get("dealPct")) is not None else nums.get("dealPctDefault", 15)) / 100
    ceiling = num(s.get("maxPrice"))
    under_market = pct is not None and pct >= thr
    under_ceiling = True if ceiling is None else (cost is not None and cost <= ceiling)
    r = num(s.get("radiusMi"))
    home = miles_between(zip_coords(s.get("zip")), coords_for(l))
    if home is None and isinstance(l.get("distanceMi"), (int, float)):
        home = l["distanceMi"]
    in_radius = True if (r is None or not s.get("zip")) else (home is not None and home <= r)
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
    fresh = hours_since(confirmed) <= nums.get("priceFreshHours", 24)
    live = not l.get("gone")
    is_deal = live and under_market and under_ceiling and reachable and verified and not brand and passed and fresh
    lead = live and under_market and under_ceiling and reachable and not brand and not is_deal and not failed
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
            "checkPassed": passed}


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


# ---------------------------------------------------------------- mutations (each is replayable)
CONFIRMED = "Confirmed by Cameron"


def op_upsert(st, items, at):
    if isinstance(items, dict):
        items = [items]
    pasted = st.pasted()
    changed = 0
    for inc in items:
        inc = {k: v for k, v in inc.items() if k != "status"}  # status is Cameron's (status.json)
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
    stamp = run["at"][:16].replace(":", "")
    run.setdefault("id", f"{stamp}-{run['searchId']}-{run['collector']}")
    runs = [r for r in st.runs() if r.get("id") != run["id"]]
    runs.append(run)
    runs.sort(key=lambda r: r.get("at", ""), reverse=True)
    keep = int(st.rules().get("numbers", {}).get("runsKeep", 300))
    st.write("runs.json", runs[:keep])
    return f"run {run['id']} recorded"


def op_unpaste(st, lid, at):
    p = st.pasted()
    if lid in p:
        del p[lid]
        st.write("pasted.json", p)
        return f"{lid}: removed from pasted"
    return f"{lid}: not in pasted"


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


# ---------------------------------------------------------------- git
def git(st, *args, check=True):
    r = subprocess.run(["git", "-C", st.root] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        die(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r


def ensure_ignore(st):
    gi = st.path(".gitignore")
    lines = open(gi).read().splitlines() if os.path.exists(gi) else []
    for want in (OPS, REPLAY, "*.tmp", ".ds-collector"):
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
    if l.get("status") not in (None, "new"):
        bits.append(l["status"])
    bits.append(f"seen {str(l.get('lastSeen') or '')[:16]}")
    return " | ".join(str(b) for b in bits)


def cmd_summary(st):
    r = st.rules()
    nums = r.get("numbers", {})
    L = st.all_listings()
    print(f"rules v{r.get('version')} · {len(st.searches())} searches · {len(L)} listings · {len(st.pasted())} pasted links")
    for sid, s in st.searches().items():
        mine = {k: v for k, v in L.items() if v.get("searchId") == sid}
        mk = market_for(s, sid, L, nums)
        sites = st.sites()
        live = [(l, score(l, s, mk, nums)) for l in mine.values() if not l.get("gone") and l.get("status") != "passed"]
        pg = [l for l, sc in live if probably_gone(l, sc, sites, nums) is not None]
        scored = [(l, sc) for l, sc in live if probably_gone(l, sc, sites, nums) is None]
        open_ = [l for l, _ in scored]
        leads = [x for x in scored if x[1]["photoWork"] or x[1]["reconfirm"]]
        concerns = [x for x in scored if x[1]["concern"]]
        deals = [x for x in scored if x[1]["isDeal"]]
        pend = [l for l in open_ if (l.get("check") or {}).get("status") == "pending"]
        stale = [l for l, sc in scored if refresh_due(l, sc, nums)[0]]
        print(f"\n[{sid}] {s.get('name')} · {'active' if s.get('active') else 'PAUSED'} · sources {','.join(s.get('sources') or [])}")
        print(f"  {len(open_)} open · market {money(mk['value']) if mk['value'] else '-'} ({mk['basis']}) · "
              f"{len(deals)} deals · {len(leads)} deal-priced needing checks · {len(concerns)} with concerns (caution) · "
              f"{len(pend)} pending checks · {len(stale)} due for refresh · {len(pg)} probably gone (hidden)")
    print("\nSITES (route state, last read)")
    for sid, s in sorted(st.sites().items()):
        rs = []
        for c in ("cloud", "pc"):
            x = s.get(c)
            if x:
                rs.append(f"{c}:{x.get('state')}@{str(x.get('at') or '')[:16]}")
        print(f"  {sid:14} how={s.get('how', '?'):5} " + "  ".join(rs))
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


def cmd_listings(st, argv):
    if not argv:
        die("listings <searchId> [--source S] [--open] [--pending] [--leads] [--stale] [--json]  (--open leaves out gone, passed and probably gone)")
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
        open_ = not l.get("gone") and l.get("status") != "passed" and sc["probablyGone"] is None
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
            rows.append((l, sc, (0 if sc["reconfirm"] else 1, -(sc["pct"] or 0), sc["homeDist"] if sc["homeDist"] is not None else 1e9), why))
        else:
            # Order (v2.1): deal-priced leads, then listings Cameron marked, then everything else that is due,
            # oldest first. Unverified listings count as due but no longer jump the queue: shop cards the
            # reader can't open were filling every slot.
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


def cmd_score(st, sid):
    s = st.searches().get(sid) or die(f"no search {sid}")
    nums = st.rules().get("numbers", {})
    L = st.all_listings()
    mk = market_for(s, sid, L, nums)
    print(f"{s.get('name')}: market {money(mk['value']) if mk['value'] else '-'} ({mk['basis']}); deal = "
          f"{num(s.get('dealPct')) or nums.get('dealPctDefault')}% under, ceiling {money(s.get('maxPrice'))}, near-you {s.get('radiusMi')} mi of {s.get('zip')}")
    n = 0
    for l in L.values():
        if l.get("searchId") != sid or l.get("gone") or l.get("status") == "passed":
            continue
        sc = score(l, s, mk, nums)
        if sc["underMarket"] or sc["isDeal"]:
            n += 1
            print(line(l, sc) + f" | ref {money(sc['ref'])} cost {money(sc['cost'])}"
                  f"{' | branded' if sc['branded'] else ''}{'' if sc['fresh'] else ' | price not confirmed in ' + str(nums.get('priceFreshHours')) + ' h'}")
    print(f"({n} deal-priced)")


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
        record(st, "route", {"collector": a[0], "site": a[1], "route": parse_json_arg(a[2])})
    elif c == "site":
        record(st, "site", {"site": a[0], "facts": parse_json_arg(a[1])})
    elif c == "search":
        record(st, "search", {"id": a[0], "patch": parse_json_arg(a[1])})
    elif c == "run":
        record(st, "run", {"run": parse_json_arg(a[0])})
    elif c == "unpaste":
        record(st, "unpaste", {"id": a[0]})
    elif c == "commit":
        commit(st, a[0] if a else "update")
    elif c == "pull":
        pull(st)
    else:
        die(f"unknown command {c}; run ds.py help")


if __name__ == "__main__":
    main()
