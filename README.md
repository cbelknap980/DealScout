# Deal Scout

Cameron's deal finder. He saves a search for an item (a Triumph Daytona 675R, used skis), and two collectors scan classified sites across the US, read every ad, check that the ad is real and the price current, and only call something a deal when it is clearly under market. Deals arrive by email; everything shows on the page.

- Page: https://cbelknap980.github.io/DealScout/
- Data: a separate private repo (`cbelknap980/dealscout-data`). This repo holds code only: no listings, searches, email addresses or tokens.

## How it fits together

| Part | Where | What it does |
|---|---|---|
| Page | `index.html`, `js/github-db.js` | GitHub Pages site. Reads the data repo with a fine-grained GitHub token pasted once per device; re-scores every listing in the browser; writes only `searches.json`, `pasted.json` and `status.json`. |
| Cloud collector | `collectors/cloud.md` | Scheduled Claude task, 4x daily. Reads cloud-readable sites with web fetch, makes site plans, reads each ad, marks deal-priced ads for a photo check. Never emails. |
| PC collector | `collectors/pc.md` | Scheduled Claude task, 4x daily, with Cameron's PC linked. Reads Facebook Marketplace, Cycle Trader, eBay, nationwide Craigslist and Powder7 in the Claude app's browser, reviews the photos of every deal-priced ad, sends deal emails. |
| Data helper | `tools/ds.py` | The only way collectors read and change the data. Records each change and, on push, replays them onto the newest copy if the other collector pushed first. Python 3 standard library only. |
| Rules | `dealscout-data/rules.json` | Thresholds, check rules and wording, and the item kinds (Vehicle, Skis, Something else) with each kind's fields and filters, read by both collectors and the page. Change a rule there, once. |
| Map | `architecture.json` | The page's "How it works" tab, searchable. |
| Geo | `geo.json` | ZIP and city coordinates for distances from home. |

Each scheduled task's prompt only says to clone both repos and follow its file in `collectors/`, so a change to how a collector works is a commit here.

## Run it locally

The page is static. From this folder:

```
python3 -m http.server 8000
```

Open http://localhost:8000 and paste a GitHub token when asked (fine-grained, repository access: only `dealscout-data`, permission Contents: read and write). Without access to the data repo the page shows the token screen and nothing else.

To use the data helper against a local clone of the data repo:

```
python3 tools/ds.py --data ../dealscout-data summary
python3 tools/ds.py --data ../dealscout-data score used-skis
```

`ds.py specs [<searchId>]` re-reads ski specs (brand, model, length, underfoot, bindings) from stored titles and notes, without touching what a collector recorded.

## History

- v2.6 (Nov 20, 2026): item kinds. Each search is a Vehicle, Skis or Something else search, and the kind sets the editor fields, the sidebar filters and what the collectors record. Ski searches filter and limit by brand, model, length, underfoot and bindings, rank pickup listings ahead of ship-in ones, and price ship-in skis with no shipping shown at a $40 estimate (rules v1.7).
- v2.5 (Nov 20, 2026): a search's own limits from Edit search (year range, max miles, price ceiling, ship rule) decide which listings are in it. Listings outside them never count as deals, leave the open list for Status: Outside search, and year and miles keep them out of the median; the sidebar filters only narrow inside the limits (rules v1.6).
- v2.4 (Nov 20, 2026): a listing nothing has shown for 7 days, while its site was read fine, counts as probably gone and leaves the open list (rules v1.5).
- v2.3 (Nov 20, 2026): bug-fix batch from the Nov 19 bug hunt. Sites judges each site per search, a block pause counts blocks only and ds.py sets it, ds.py replays safely after a push race, the search editor saves any number, and phone fields no longer zoom.
- v2.2 (Nov 19, 2026): Listings is the home page, with filters, chips and Watch, Contacted and Pass on each row.
- v2.1 (Nov 19, 2026): page fixes plus refresh and photo-check work queues in ds.py.
- v2.0 (Nov 18, 2026): moved from a claude.ai artifact to GitHub Pages plus a private data repo; photo checks moved to the PC; no paid services.
- v1.0 to v1.3 (Oct 31 to Nov 2, 2026): claude.ai artifact with its own database.

The full spec, with the change log and design decisions, is the "Deal Scout: App Spec" doc in Claude Docs.
