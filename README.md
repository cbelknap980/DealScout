# Deal Scout

Cameron's deal finder. He saves a search for an item (a Triumph Daytona 675R, used skis), and two collectors scan classified sites across the US, read every ad, check that the ad is real and the price current, and only call something a deal when it is clearly under market. Deals arrive by email; everything shows on the page.

- Page: https://cbelknap980.github.io/DealScout/
- Data: a separate private repo (`cbelknap980/dealscout-data`). This repo holds code only: no listings, searches, email addresses or tokens.

## How it fits together

| Part | Where | What it does |
|---|---|---|
| Page | `index.html`, `js/github-db.js` | GitHub Pages site. Reads the data repo with a fine-grained GitHub token pasted once per device; re-scores every listing in the browser; writes only `searches.json`, `pasted.json` and `status.json`. |
| Cloud collector | `collectors/cloud.md` | Scheduled Claude task, 4x daily. Reads cloud-readable sites with web fetch, makes site plans, reads each ad, marks deal-priced ads for a photo check. Never emails. |
| PC collector | `collectors/pc.md` | Scheduled Claude task, 4x daily, with Cameron's PC linked. Reads Facebook Marketplace, Cycle Trader and eBay in the Claude app's browser, reviews the photos of every deal-priced ad, sends deal emails. |
| Data helper | `tools/ds.py` | The only way collectors read and change the data. Records each change and, on push, replays them onto the newest copy if the other collector pushed first. Python 3 standard library only. |
| Rules | `dealscout-data/rules.json` | Thresholds, check rules and wording, read by both collectors and the page. Change a rule there, once. |
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

## History

- v2.0 (Nov 18, 2026): moved from a claude.ai artifact to GitHub Pages plus a private data repo; photo checks moved to the PC; no paid services.
- v1.0 to v1.3 (Oct 31 to Nov 2, 2026): claude.ai artifact with its own database.

The full spec, with the change log and design decisions, is the "Deal Scout: App Spec" doc in Claude Docs.
