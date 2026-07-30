from __future__ import annotations

import io
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import segno
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import db, ocr, split, translate, tricount

BASE = Path(__file__).parent
MAX_UPLOAD = int(os.environ.get("ZECHE_MAX_UPLOAD_MB", "12")) * 1024 * 1024
OCR_LANG = os.environ.get("ZECHE_OCR_LANG", "deu+eng")
# Set this to https://bill.kronask.cc so QR codes and shared links point at the
# tunnel hostname rather than whatever the container thinks it is called.
PUBLIC_URL = os.environ.get("ZECHE_BASE_URL", "").rstrip("/")

# Chip colours for participants — picked to stay distinguishable on the dark UI
# and for the most common colour-blindness types.
PALETTE = [
    "#E0A82E", "#4AA8C9", "#C96F4A", "#7FB069",
    "#B07FC9", "#D9576B", "#4ECDC4", "#9C8B5E",
]

app = FastAPI(title="Zeche", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


@app.on_event("startup")
def _startup():
    db.init()
    # The Tricount client writes its device credentials to the working
    # directory; park the process in the data volume so they survive a rebuild.
    os.chdir(db.DATA_DIR)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sid(n: int = 9) -> str:
    return secrets.token_urlsafe(n)


def public_url(request: Request, path: str) -> str:
    root = PUBLIC_URL or str(request.base_url).rstrip("/")
    return f"{root}{path}"


def get_bill(bill_id: str) -> dict:
    bill = db.one("SELECT * FROM bills WHERE id = ?", (bill_id,))
    if not bill:
        raise HTTPException(404, "No bill with that link")
    return bill


def require_admin(bill: dict, token: str | None):
    if not token or not secrets.compare_digest(token, bill["admin_token"]):
        raise HTTPException(403, "This link can view the bill but not edit it")


def bill_state(bill_id: str) -> dict:
    bill = get_bill(bill_id)
    items = db.rows("SELECT * FROM items WHERE bill_id=? ORDER BY position", (bill_id,))
    people = db.rows("SELECT * FROM people WHERE bill_id=? ORDER BY created_at", (bill_id,))
    claims = db.rows(
        "SELECT c.* FROM claims c JOIN items i ON i.id=c.item_id WHERE i.bill_id=?",
        (bill_id,),
    )
    public = {k: v for k, v in bill.items() if k != "admin_token"}
    return {
        "bill": public,
        "items": items,
        "people": people,
        "claims": claims,
        "totals": split.compute(bill, items, people, claims),
    }


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

@app.get("/")
def page_upload(request: Request):
    return templates.TemplateResponse(request, "upload.html")


@app.get("/b/{bill_id}/edit")
def page_edit(request: Request, bill_id: str, t: str = ""):
    bill = get_bill(bill_id)
    require_admin(bill, t)
    return templates.TemplateResponse(
        request, "edit.html", {"bill_id": bill_id, "token": t}
    )


@app.get("/b/{bill_id}")
def page_bill(request: Request, bill_id: str):
    get_bill(bill_id)
    return templates.TemplateResponse(request, "bill.html", {"bill_id": bill_id})


@app.get("/b/{bill_id}/photo")
def bill_photo(bill_id: str):
    bill = get_bill(bill_id)
    if not bill["image_name"]:
        raise HTTPException(404, "No photo for this bill")
    path = db.IMAGE_DIR / bill["image_name"]
    if not path.exists():
        raise HTTPException(404, "No photo for this bill")
    return FileResponse(path)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@app.post("/api/bills")
async def create_bill(
    request: Request,
    image: UploadFile | None = File(None),
    title: str = Form(""),
    paid_by: str = Form(""),
    currency: str = Form("€"),
    tip_amount: float = Form(0.0),
    tip_mode: str = Form("equal"),
    unclaimed_mode: str = Form("equal"),
    translate_to: str = Form(""),
):
    bill_id, token = sid(6), sid(16)
    image_name, parsed_items, stated_total, raw_text = None, [], None, ""

    if image is not None and image.filename:
        data = await image.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, f"Photo is larger than {MAX_UPLOAD // 1024 // 1024} MB")
        ext = Path(image.filename).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp"}:
            raise HTTPException(415, "Upload a JPEG, PNG or WebP photo")
        image_name = f"{bill_id}{ext}"
        (db.IMAGE_DIR / image_name).write_bytes(data)
        try:
            # OCR and machine translation both block for seconds; keep them off
            # the event loop or the whole app stalls while one photo is read.
            result = await run_in_threadpool(ocr.scan, data, OCR_LANG)
            parsed_items = result.items
            stated_total = result.stated_total
            raw_text = result.raw_text
        except Exception as exc:  # OCR failure still gets you a usable bill
            raw_text = f"OCR failed: {exc}"

    target = translate_to.strip().lower()
    translated: list[str] = [i.name for i in parsed_items]
    if target and translate.enabled() and parsed_items:
        translated = await run_in_threadpool(
            translate.translate, [i.name for i in parsed_items], target
        )

    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO bills (id, admin_token, title, paid_by, currency, tip_amount,
                                  tip_mode, unclaimed_mode, stated_total, translate_to,
                                  image_name, raw_text, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (bill_id, token, title.strip(), paid_by.strip(), currency.strip() or "€",
             tip_amount, tip_mode, unclaimed_mode, stated_total, target,
             image_name, raw_text, now()),
        )
        for pos, (it, shown) in enumerate(zip(parsed_items, translated)):
            cur.execute(
                "INSERT INTO items (id, bill_id, position, name, name_src, qty, total)"
                " VALUES (?,?,?,?,?,?,?)",
                (sid(), bill_id, pos, shown, it.name if shown != it.name else None,
                 it.qty, it.total),
            )

    return {"id": bill_id, "token": token, "items_found": len(parsed_items),
            "edit_url": f"/b/{bill_id}/edit?t={token}",
            "share_url": public_url(request, f"/b/{bill_id}")}


@app.get("/api/bills/{bill_id}")
def read_bill(bill_id: str):
    return bill_state(bill_id)


class ItemIn(BaseModel):
    id: str | None = None
    name: str
    name_src: str | None = None
    qty: float = 1
    total: float = 0


class BillUpdate(BaseModel):
    title: str | None = None
    paid_by: str | None = None
    currency: str | None = None
    tip_amount: float | None = None
    tip_mode: str | None = None
    unclaimed_mode: str | None = None
    locked: bool | None = None
    items: list[ItemIn] | None = None


@app.put("/api/bills/{bill_id}")
def update_bill(bill_id: str, payload: BillUpdate, t: str = ""):
    bill = get_bill(bill_id)
    require_admin(bill, t)

    fields, values = [], []
    for col in ("title", "paid_by", "currency", "tip_amount", "tip_mode", "unclaimed_mode"):
        val = getattr(payload, col)
        if val is not None:
            fields.append(f"{col}=?")
            values.append(val)
    if payload.locked is not None:
        fields.append("locked=?")
        values.append(1 if payload.locked else 0)

    with db.cursor() as cur:
        if fields:
            cur.execute(f"UPDATE bills SET {', '.join(fields)} WHERE id=?", (*values, bill_id))

        if payload.items is not None:
            keep = set()
            for pos, item in enumerate(payload.items):
                name = item.name.strip() or "Position"
                qty = max(item.qty, 0.0)
                total = round(item.total, 2)
                src = (item.name_src or "").strip() or None
                if item.id:
                    cur.execute(
                        "UPDATE items SET position=?, name=?, name_src=?, qty=?, total=?"
                        " WHERE id=? AND bill_id=?",
                        (pos, name, src, qty, total, item.id, bill_id),
                    )
                    keep.add(item.id)
                else:
                    new_id = sid()
                    cur.execute(
                        "INSERT INTO items (id, bill_id, position, name, name_src, qty, total)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (new_id, bill_id, pos, name, src, qty, total),
                    )
                    keep.add(new_id)
            existing = {r["id"] for r in
                        db_rows_nolock(cur, "SELECT id FROM items WHERE bill_id=?", (bill_id,))}
            for gone in existing - keep:
                cur.execute("DELETE FROM claims WHERE item_id=?", (gone,))
                cur.execute("DELETE FROM items WHERE id=?", (gone,))

    return bill_state(bill_id)


def db_rows_nolock(cur, sql, params):
    return [dict(r) for r in cur.execute(sql, params).fetchall()]


class PersonIn(BaseModel):
    name: str


@app.post("/api/bills/{bill_id}/people")
def add_person(bill_id: str, payload: PersonIn):
    bill = get_bill(bill_id)
    if bill["locked"]:
        raise HTTPException(409, "This bill is closed for changes")
    name = payload.name.strip()[:40]
    if not name:
        raise HTTPException(422, "Enter a name so people know whose share is whose")

    existing = db.rows("SELECT * FROM people WHERE bill_id=?", (bill_id,))
    for p in existing:
        if p["name"].lower() == name.lower():
            return p  # same name rejoining from another device
    if len(existing) >= 30:
        raise HTTPException(409, "This bill already has 30 people on it")

    person = {
        "id": sid(), "bill_id": bill_id, "name": name,
        "color": PALETTE[len(existing) % len(PALETTE)], "created_at": now(),
    }
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO people (id, bill_id, name, color, created_at) VALUES (?,?,?,?,?)",
            tuple(person[k] for k in ("id", "bill_id", "name", "color", "created_at")),
        )
    return person


@app.delete("/api/bills/{bill_id}/people/{person_id}")
def remove_person(bill_id: str, person_id: str):
    get_bill(bill_id)
    with db.cursor() as cur:
        cur.execute("DELETE FROM claims WHERE person_id=?", (person_id,))
        cur.execute("DELETE FROM people WHERE id=? AND bill_id=?", (person_id, bill_id))
    return {"ok": True}


class ClaimIn(BaseModel):
    person_id: str
    item_id: str
    shares: float


@app.post("/api/bills/{bill_id}/claims")
def set_claim(bill_id: str, payload: ClaimIn):
    bill = get_bill(bill_id)
    if bill["locked"]:
        raise HTTPException(409, "This bill is closed for changes")
    if not db.one("SELECT id FROM items WHERE id=? AND bill_id=?", (payload.item_id, bill_id)):
        raise HTTPException(404, "That item is not on this bill")
    if not db.one("SELECT id FROM people WHERE id=? AND bill_id=?", (payload.person_id, bill_id)):
        raise HTTPException(404, "You are not on this bill yet")

    shares = max(0.0, min(payload.shares, 99.0))
    with db.cursor() as cur:
        if shares <= 0:
            cur.execute("DELETE FROM claims WHERE item_id=? AND person_id=?",
                        (payload.item_id, payload.person_id))
        else:
            cur.execute(
                """INSERT INTO claims (item_id, person_id, shares) VALUES (?,?,?)
                   ON CONFLICT(item_id, person_id) DO UPDATE SET shares=excluded.shares""",
                (payload.item_id, payload.person_id, shares),
            )
    return bill_state(bill_id)


class TranslateIn(BaseModel):
    target: str = "en"


@app.post("/api/bills/{bill_id}/translate")
async def translate_items(bill_id: str, payload: TranslateIn, t: str = ""):
    bill = get_bill(bill_id)
    require_admin(bill, t)
    if not translate.enabled():
        raise HTTPException(503, "No translation service is configured on this server")

    items = db.rows("SELECT * FROM items WHERE bill_id=? ORDER BY position", (bill_id,))
    if not items:
        return bill_state(bill_id)

    # Re-translating a bill uses the printed original, not the previous
    # translation, so running it twice can't drift.
    sources = [i["name_src"] or i["name"] for i in items]
    target = payload.target.strip().lower() or "en"
    out = await run_in_threadpool(translate.translate, sources, target)

    with db.cursor() as cur:
        cur.execute("UPDATE bills SET translate_to=? WHERE id=?", (target, bill_id))
        for item, src, shown in zip(items, sources, out):
            cur.execute(
                "UPDATE items SET name=?, name_src=? WHERE id=?",
                (shown, src if shown != src else None, item["id"]),
            )
    return bill_state(bill_id)


@app.post("/share-target")
async def share_target(request: Request, photo: UploadFile | None = File(None), title: str = Form("")):
    """Android share sheet lands here: photo in, edit page out."""
    if photo is None or not photo.filename:
        return RedirectResponse("/", status_code=303)
    created = await create_bill(
        request=request, image=photo, title=title, paid_by="", currency="€",
        tip_amount=0.0, tip_mode="equal", unclaimed_mode="equal",
        translate_to=translate.TARGET if translate.enabled() else "",
    )
    return RedirectResponse(created["edit_url"], status_code=303)


@app.get("/api/config")
def config(request: Request):
    return {
        "base_url": public_url(request, ""),
        "tricount": tricount.available(),
        "tricount_default_key": tricount.DEFAULT_KEY,
        "translate": translate.enabled(),
        "translate_provider": translate.describe(),
        "translate_target": translate.TARGET,
    }


class TricountLookup(BaseModel):
    link: str = ""


@app.post("/api/bills/{bill_id}/tricount/lookup")
async def tricount_lookup(bill_id: str, payload: TricountLookup, t: str = ""):
    """Join the tricount and hand back its members plus a suggested mapping."""
    bill = get_bill(bill_id)
    require_admin(bill, t)
    if not tricount.available():
        raise HTTPException(503, "The tricount client isn't installed on this server")

    raw = payload.link.strip() or bill["tricount_key"] or tricount.DEFAULT_KEY
    if not raw:
        raise HTTPException(422, "Paste the tricount sharing link first")
    try:
        key = tricount.parse_key(raw)
        title, members = await run_in_threadpool(tricount.fetch, key)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Could not reach that tricount: {exc}")

    people = db.rows("SELECT * FROM people WHERE bill_id=? ORDER BY created_at", (bill_id,))
    saved = {p["id"]: p["tricount_member"] for p in people if p["tricount_member"]}
    guessed = tricount.match_names(people, members)
    guessed.update(saved)  # a mapping the owner already confirmed wins

    with db.cursor() as cur:
        cur.execute("UPDATE bills SET tricount_key=? WHERE id=?", (key, bill_id))

    return {
        "key": key,
        "title": title,
        "members": [{"id": m.id, "name": m.name} for m in members],
        "mapping": guessed,
        "pushed_tx": bill["tricount_tx"],
    }


class TricountPush(BaseModel):
    link: str = ""
    mapping: dict[str, str]
    description: str = ""
    force: bool = False


@app.post("/api/bills/{bill_id}/tricount/push")
async def tricount_push(bill_id: str, payload: TricountPush, t: str = ""):
    bill = get_bill(bill_id)
    require_admin(bill, t)
    if not tricount.available():
        raise HTTPException(503, "The tricount client isn't installed on this server")
    if bill["tricount_tx"] and not payload.force:
        raise HTTPException(
            409, "This bill is already in the tricount. Pushing again would double it."
        )

    state = bill_state(bill_id)
    totals = state["totals"]
    rows = [r for r in totals["rows"] if r["owes"] > 0]
    if not rows:
        raise HTTPException(422, "Nobody has claimed anything yet")

    payer_person = next(
        (p for p in state["people"] if p["name"].casefold() == (bill["paid_by"] or "").casefold()),
        None,
    )
    if payer_person is None:
        raise HTTPException(
            422,
            f"'{bill['paid_by'] or 'the payer'}' isn't on this bill — "
            "whoever paid has to join and claim their own items first",
        )

    mapping = payload.mapping
    unmapped = [r["name"] for r in rows if not mapping.get(r["person_id"])]
    if unmapped or not mapping.get(payer_person["id"]):
        missing = unmapped or [payer_person["name"]]
        raise HTTPException(422, "Not matched to a tricount member: " + ", ".join(missing))

    # Work in cents from here. totals.rows already sums to grand_total exactly.
    allocations = [(mapping[r["person_id"]], round(r["owes"] * 100)) for r in rows]
    total_cents = round(totals["grand_total"] * 100)
    drift = total_cents - sum(c for _, c in allocations)
    if drift:
        # Only ever a cent from float rounding; put it on the largest share.
        biggest = max(range(len(allocations)), key=lambda i: allocations[i][1])
        mid, cents = allocations[biggest]
        allocations[biggest] = (mid, cents + drift)

    raw = payload.link.strip() or bill["tricount_key"] or tricount.DEFAULT_KEY
    description = (payload.description.strip() or bill["title"] or "Restaurant").strip()

    try:
        key = tricount.parse_key(raw)
        tx = await run_in_threadpool(
            tricount.push, key, description, total_cents,
            mapping[payer_person["id"]], allocations,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Tricount rejected the expense: {exc}")

    with db.cursor() as cur:
        cur.execute(
            "UPDATE bills SET tricount_key=?, tricount_tx=?, locked=1 WHERE id=?",
            (key, tx, bill_id),
        )
        for person_id, member_id in mapping.items():
            cur.execute(
                "UPDATE people SET tricount_member=? WHERE id=? AND bill_id=?",
                (member_id, person_id, bill_id),
            )

    return {
        "tx": tx,
        "description": description,
        "total": round(total_cents / 100, 2),
        "shares": [{"name": r["name"], "owes": r["owes"]} for r in rows],
    }


@app.get("/b/{bill_id}/qr.svg")
def bill_qr(request: Request, bill_id: str):
    get_bill(bill_id)
    buf = io.BytesIO()
    segno.make(public_url(request, f"/b/{bill_id}"), error="m").save(
        buf, kind="svg", scale=8, border=2, dark="#14161b", light="#e9ebef"
    )
    return Response(
        buf.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/manifest.webmanifest")
def manifest():
    return Response(
        (BASE / "static" / "manifest.json").read_bytes(),
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def service_worker():
    # Must be served from the root or it cannot control /b/* pages.
    return Response(
        (BASE / "static" / "sw.js").read_bytes(),
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 404:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request, "error.html", {"message": exc.detail}, status_code=exc.status_code
    )
