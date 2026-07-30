# Zeche

Self-hosted bill splitting, built for a phone at a restaurant table. Photograph the receipt, let Tesseract read the line items, optionally have them translated, then hold up a QR code. Everyone taps what they had and sees what they owe the person who paid.

FastAPI + Tesseract + SQLite in one container. LibreTranslate alongside it if you want translation. No accounts.

## Run it

```bash
cp .env.example .env          # only if you use the bundled cloudflared
docker compose up -d --build
```

First boot takes a while — LibreTranslate downloads a language model per pair listed in `LT_LOAD_ONLY`. Zeche waits for its healthcheck. Everything persists in `./data`: `zeche.db`, the receipt photos, and `lt-models/`.

Set `ZECHE_TRANSLATE_PROVIDER: none` and delete the `libretranslate` service if you don't want translation at all.

## bill.kronask.cc

`ZECHE_BASE_URL` is what makes the shared links and the QR code come out as your tunnel hostname instead of `http://172.18.0.2:8080`. It's already set in `docker-compose.yml`.

**Using your existing tunnel** (the cloudflared LXC already serving kronask.cc) — add the rule from `cloudflared-ingress.yml` to its `config.yml` above the catch-all, then:

```bash
cloudflared tunnel route dns <tunnel-name> bill.kronask.cc
systemctl restart cloudflared
```

**Or run a tunnel next to the app:** create a tunnel in the Zero Trust dashboard, point its public hostname `bill.kronask.cc` at `http://zeche:8080`, put the token in `.env`, and:

```bash
docker compose --profile tunnel up -d
```

Either way, drop the `ports:` block from the `zeche` service once the tunnel works — nothing needs to reach 8080 directly.

Two Cloudflare details that bite: the free plan caps uploads at 100 MB (receipt photos are a few MB, fine), and OCR plus translation can take 20–30 seconds on a loaded node, so keep `connectTimeout` generous.

**There's no login.** Anyone who can reach `bill.kronask.cc` can create bills. Put Cloudflare Access on `/` and `POST /api/bills` and leave `/b/*` open, otherwise your guests can't claim.

## On the phone

Open `bill.kronask.cc` and add it to the home screen — manifest, icons and a service worker are all there, so it launches without browser chrome. On Android it also registers as a share target: photograph a receipt, share it to Zeche, and you land straight in the editor.

Everything is sized for one-handed use: 48px tap targets, a camera tile as the first thing on the upload screen, a stepper for splitting multi-quantity lines, a short vibration on each claim, and a running total pinned above the home indicator.

## A bill, start to finish

1. **`/`** — photo, tip, currency, three toggles. Upload.
2. **`/b/<id>/edit?t=<token>`** — one card per item, all editable. If the receipt printed a SUMME/TOTAL line, the page compares it against the sum of the items and says so when they disagree, which is how you catch `8,40` read as `0,40`. Then **Save & get the link**.
3. **The QR code.** Hold the phone up, everyone scans it, no link to type. There's a share button for the group chat too.
4. **`/b/<id>`** — enter a name, tap items. Coloured chips show who's already on each line; the bar at the bottom keeps a running total.
5. **Close the bill** freezes it once everyone has settled up.

Every upload creates a new id — `/b/1PpZ9OUS`, `/b/YdJ9Rb-E` — with its own QR code and its own admin token. Old bills stay reachable at their links.

The edit URL is the only thing carrying the admin token. The plain `/b/<id>` link claims but can't change prices. Bookmark the edit URL; there's no recovery for it.

## Translation

Item names are translated on upload and **the printed original is kept**. It shows as a grey monospace line under the translation, because nobody can match "grilled pork belly" back to the paper receipt otherwise. There's a toggle on the bill page to flip which one is primary.

Re-translating always works from the stored original, so running it twice can't drift.

| `ZECHE_TRANSLATE_PROVIDER` | What it needs |
| --- | --- |
| `none` | nothing — names stay as printed |
| `libretranslate` | the bundled container, no key |
| `ollama` | `ZECHE_TRANSLATE_URL=http://10.10.10.x:11434`, `ZECHE_TRANSLATE_MODEL=qwen2.5:7b` |
| `openai` | any OpenAI-compatible endpoint plus `ZECHE_TRANSLATE_KEY` |

LibreTranslate is fast and offline but literal — it will happily turn "Gulasch" into "Goulash" and also mangle the odd dish name. An LLM does noticeably better on menus because the prompt tells it to leave brands, wine varieties and dishes like Wiener Schnitzel alone. If you wake up the ollama LXC, point it there.

Whatever the provider, a failure is non-fatal: the names stay as OCR read them.

## Tricount

One bill goes in as **one expense with a custom split** — payer, total, and a per-person
amount that already sums to the total exactly. On the edit page: paste the sharing link,
hit **Match names**, check the pairings, **Add to the tricount**. The bill locks itself
afterwards so nobody can re-tap items and drift out of sync with what the tricount says.

Set `ZECHE_TRICOUNT_KEY` to the vacation's sharing key and the field comes prefilled.

**This uses an unofficial API.** Tricount has no public write endpoint; the only official
import is a Splitwise CSV that creates a *new* tricount, which is no use mid-trip. So this
goes through [`tricount-api`](https://pypi.org/project/tricount-api/), reverse-engineered
from the Android app. It can break whenever bunq ships an update, and it is presumably not
something bunq's terms contemplate. Your tricount, your data, your call.

Because it can break, **Copy the numbers** sits next to it and always works — payer, total
and each person's share on the clipboard, in the order Tricount's custom-split screen asks
for them. Typing that in takes about twenty seconds.

Guards worth knowing about:

- A bill that's already been pushed returns 409 rather than doubling the expense.
- A push whose shares don't add up to the total is refused before it leaves the server.
- Anyone not matched to a tricount member blocks the push by name.
- People who owe nothing are dropped from the expense instead of sent as a zero share.
- The payer has to be on the bill and claiming their own items, or there's nobody to
  attribute the expense to.

Device credentials are generated on first use and stored as `data/tricount_credentials.json`.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `ZECHE_BASE_URL` | *(empty)* | Public origin for links and QR codes |
| `ZECHE_DATA` | `/data` | Database and uploaded photos |
| `ZECHE_OCR_LANG` | `deu+eng` | Any Tesseract language string |
| `ZECHE_MAX_UPLOAD_MB` | `12` | Larger photos are rejected |
| `ZECHE_TRANSLATE_PROVIDER` | `none` | `none` / `libretranslate` / `ollama` / `openai` |
| `ZECHE_TRANSLATE_URL` | `http://libretranslate:5000` | Provider endpoint |
| `ZECHE_TRANSLATE_MODEL` | `qwen2.5:7b` | Ollama and OpenAI only |
| `ZECHE_TRANSLATE_KEY` | *(empty)* | API key where the provider wants one |
| `ZECHE_TRANSLATE_TARGET` | `en` | Target language |
| `ZECHE_TRICOUNT_KEY` | *(empty)* | Prefills the sharing link on the edit page |

For another OCR language, add the package and extend the string:

```dockerfile
RUN apt-get install -y tesseract-ocr-ita tesseract-ocr-fra
```

## Splitting rules

An item's cost divides by the shares people actually claimed, not by its printed quantity. Three people each claiming one plate of nachos pay a third; one person claiming 2 of 3 espressos pays two thirds. Totals round to cents and the leftover cent lands on the largest share, so what everyone pays adds up to the bill exactly.

**Split the tip equally** — on, divided by headcount. Off, in proportion to what each person ate.

**Share leftover items equally** — on, anything unclaimed spreads across everyone on the bill. Off, it stays with whoever paid.

## Reading quality

Tesseract wants a flat, in-focus, well-lit receipt filling the frame. Curled or faded thermal paper reads badly whatever you do. The parser handles comma decimals, tax-class letters (`19,80 B`), `2 Stk` / `2x` prefixes, dot leaders, and lines carrying both unit price and line total. It drops MwSt, Trinkgeld, Bar, Rückgeld, UID, table and waiter numbers.

Expect to fix a line or two every time. That's what the edit step is for.

## Backup

```bash
sqlite3 data/zeche.db ".backup '/tmp/zeche-$(date +%F).db'"
```

Or snapshot `data/` — nothing lives outside it. Excluding `data/lt-models/` saves a few hundred MB of re-downloadable model files.
