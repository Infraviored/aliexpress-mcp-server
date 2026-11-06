"""
The cart and a shopping-list comparison as one self-contained HTML page.

The chat cannot embed pictures, so view_cart and shop_list also write a page
(pictures inlined as data: URIs, nothing loaded from AliExpress) that the
assistant publishes as an artifact. Same file path every time, so the
artifact keeps its link.

The functions take plain row dicts (built in the server) and return HTML.
"""

import base64
import html
from typing import Callable, Optional

_CSS = """
<style>
/* Layout: one column, summary first, then parts as rows (picture | text | money). */
:root {
  --bg: #f4f4f8; --surface: #ffffff; --fg: #1c1d26; --muted: #62647a; --line: #dcdde8;
  --accent: #3b4bd8; --warn: #b4540a; --warn-bg: #fdf1e6; --goods: #3b6fd8; --ship: #8a6bb8;
  --duty: #c2593a; --off: #5f8f7a;
  --display: "Barlow Semi Condensed", "Arial Narrow", sans-serif;
  --body: "Source Sans 3", "Segoe UI", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #13141b; --surface: #1b1c26; --fg: #e6e7f2; --muted: #9a9cb3; --line: #2b2d3d;
  --accent: #8f9bff; --warn: #f0a35c; --warn-bg: #2e2318; --goods: #7fa3ff; --ship: #b49ae0;
  --duty: #e8876a; --off: #7fb09a; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #13141b; --surface: #1b1c26; --fg: #e6e7f2; --muted: #9a9cb3; --line: #2b2d3d;
  --accent: #8f9bff; --warn: #f0a35c; --warn-bg: #2e2318; --goods: #7fa3ff; --ship: #b49ae0;
  --duty: #e8876a; --off: #7fb09a; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font: 15px/1.45 var(--body); }
.wrap { max-width: 1040px; margin: 0 auto; padding-block: 20px 40px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 28px; }
h1, h2 { font-family: var(--display); font-weight: 600; margin: 0; text-wrap: balance; letter-spacing: .01em; }
h1 { font-size: 1.3rem; } h2 { font-size: 1.1rem; }
.eyebrow { font: 600 .72rem/1 var(--body); text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
.num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
a { color: var(--accent); }
a:focus-visible, .btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
section { display: grid; gap: 14px; }
.head { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: 12px; }
.total { font: 600 1.6rem/1 var(--mono); font-variant-numeric: tabular-nums; }
.bar { display: flex; height: 12px; border-radius: 6px; overflow: hidden; background: var(--line); }
.bar span { display: block; height: 100%; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--muted); font-size: .9rem; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }
.rows { display: grid; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; }
.row { display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; gap: 10px; align-items: center;
  padding: 6px 12px; border-top: 1px solid var(--line); }
.row:first-child { border-top: 0; }
.row img, .row .noimg { width: 44px; height: 44px; object-fit: cover; border-radius: 6px; background: var(--line); }
.row .t { min-width: 0; display: grid; gap: 2px; }
.row .title { color: var(--muted); font-size: .88rem; overflow-wrap: anywhere; }
.row .meta { display: flex; flex-wrap: wrap; gap: 6px; font-size: .82rem; color: var(--muted); }
.row .money { text-align: right; display: grid; gap: 2px; }
.row .money b { font: 600 1rem var(--mono); font-variant-numeric: tabular-nums; }
.row .money small { color: var(--muted); font-family: var(--mono); font-size: .78rem; }
.chip { display: inline-block; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--line); font-size: .76rem; }
.chip.choice { color: var(--accent); border-color: var(--accent); }
.chip.global { color: var(--warn); border-color: var(--warn); }
.chip.off { color: var(--muted); border-style: dashed; }
.row.alt { grid-template-columns: 48px minmax(0, 1fr) auto; background: var(--bg); }
.row.alt img, .row.alt .noimg { width: 48px; height: 48px; }
.btn { justify-self: start; display: inline-block; background: var(--accent); color: var(--surface);
  padding: 10px 18px; border-radius: 8px; text-decoration: none; font-weight: 600; }
.why { font-size: .85rem; color: var(--accent); }
footer { color: var(--muted); font-size: .82rem; }
@media (max-width: 520px) {
  .row { grid-template-columns: 52px minmax(0, 1fr); }
  .row img, .row .noimg { width: 52px; height: 52px; }
  .row .money { grid-column: 2; text-align: left; display: flex; gap: 10px; align-items: baseline; }
}
.lead { margin: 0; font-size: 1.05rem; }
.sum { text-align: right; }
.rec { background: var(--surface); border: 1px solid var(--accent); border-radius: 10px; padding: 12px 16px; }
.rec p { margin: 4px 0 0; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr)); gap: 12px; }
.card { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 12px; background: var(--surface);
  border: 1px solid var(--line); border-radius: 10px; padding: 14px; }
.card img { width: 72px; height: 72px; object-fit: cover; border-radius: 6px; }
.card .c { min-width: 0; display: grid; gap: 6px; align-content: start; }
.card .ct { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; justify-content: space-between; }
.card h3 { margin: 0; font: 600 1.05rem var(--body); }
.card p { margin: 0; color: var(--muted); font-size: .88rem; }
.card .warn { color: var(--warn); }
.card a { font-size: .85rem; overflow-wrap: anywhere; }
.date { font-size: .92rem; } .date s { color: var(--muted); }
.pill { font: 600 .9rem var(--mono); font-variant-numeric: tabular-nums; padding: 2px 8px; border-radius: 6px; white-space: nowrap; }
.pill.up { background: var(--warn-bg); color: var(--warn); }
.pill.down { background: color-mix(in srgb, var(--accent) 14%, transparent); color: var(--accent); }
.pill.mixed { background: var(--line); color: var(--fg); }
.go { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; color: var(--muted); font-size: .88rem; }
.row .need { font-weight: 600; color: var(--fg); text-decoration: none; }
.row .need:hover { text-decoration: underline; }
.others summary { cursor: pointer; color: var(--muted); padding-block: 6px; }
h3 { text-wrap: balance; }

/* generic report: summary tiles, notes, one row per article */
.facts { display: flex; flex-wrap: wrap; gap: 10px; }
.fact { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; min-width: 0; display: grid; gap: 2px; }
.fact b { font: 600 1.25rem var(--mono); font-variant-numeric: tabular-nums; }
.fact span { color: var(--muted); font-size: .82rem; }
.note { background: var(--surface); border: 1px solid var(--line); border-left: 4px solid var(--accent); border-radius: 8px; padding: 10px 14px; }
.note.warn { border-left-color: var(--warn); background: var(--warn-bg); }
.note p { margin: 2px 0 0; }
.row.chosen { box-shadow: inset 3px 0 0 var(--accent); }
.row.pick { grid-template-columns: 22px 64px minmax(0, 1fr) auto; }
.row.pick input[type=radio] { width: 18px; height: 18px; accent-color: var(--accent); margin: 0; }
.row.pick.alt { background: var(--bg); }
.row .why { font-size: .86rem; color: var(--fg); }
.row .diff { font-size: .84rem; color: var(--muted); }
details.nt summary { cursor: pointer; color: var(--muted); font-size: .85rem; padding-block: 2px; }
.sec-note { width: 100%; margin-top: 6px; max-width: 100%; padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--surface); color: var(--fg); font: inherit; font-size: .88rem; }
.pickbar { position: sticky; bottom: 0; background: var(--surface); border: 1px solid var(--accent); border-radius: 10px;
  padding: 8px 14px; display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: baseline; justify-content: space-between; }
.pickbar code { font: 600 1rem var(--mono); letter-spacing: .06em; }
.code { font: 600 .72rem var(--mono); color: var(--accent); border: 1px solid var(--accent); border-radius: 4px; padding: 0 5px; }
.row img { cursor: zoom-in; }
#lb { position: fixed; inset: 0; z-index: 50; display: none; align-items: center; justify-content: center;
  background: rgba(10, 11, 18, .86); padding: 16px; cursor: zoom-out; }
#lb.on { display: flex; }
#lb img { max-width: min(96vw, 900px); max-height: 92vh; width: auto; height: auto; object-fit: contain; border-radius: 8px; background: #fff; }
.chip.changed { color: var(--warn); border-color: var(--warn); }
@media (max-width: 520px) { .row.pick { grid-template-columns: 22px 52px minmax(0, 1fr); }
  .row.pick .money { grid-column: 3; text-align: left; } }
.chip.ok { color: var(--accent); border-color: var(--accent); font-weight: 600; }
.lh { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.lh.g { border-bottom: 1px solid var(--line); padding-bottom: 4px; }
.total.sm { font-size: 1.15rem; }
section.list { gap: 10px; }
.row.tail { background: var(--bg); }
.intro { margin: 0; color: var(--muted); max-width: 65ch; }
</style>
"""

_FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Semi+Condensed:wght@600'
          '&family=IBM+Plex+Mono:wght@400;600&family=Source+Sans+3:wght@400;600&display=swap">')


def _url(u) -> str:
    """Link target for a report: http(s) only, so a card cannot carry a javascript: link."""
    u = str(u or "").strip()
    return u if u.lower().startswith(("https://", "http://")) else ""


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


# The account currency; the server sets it from ALIEXPRESS_CURRENCY.
CURRENCY = "EUR"


def eur(x: float) -> str:
    """German money format in the account currency: 1.234,50 €."""
    return (f"{x:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
            + (" €" if CURRENCY == "EUR" else f" {CURRENCY}"))


def inline_images(urls: list[str], fetch: Callable[[str], Optional[bytes]]) -> dict[str, str]:
    """url → data: URI of its thumbnail (`fetch` returns the small JPEG; fetched in parallel)."""
    from concurrent.futures import ThreadPoolExecutor

    def _abs(u: str) -> str:
        # `fetch` asks the CDN for the small size itself.
        return "https:" + u if u.startswith("//") else u

    def one(u: str) -> tuple[str, Optional[str]]:
        try:
            data = fetch(_abs(u))
        except Exception:
            data = None
        return u, ("data:image/jpeg;base64," + base64.b64encode(data).decode()) if data else None

    todo = list(dict.fromkeys(u for u in urls if u))
    with ThreadPoolExecutor(max_workers=6) as ex:
        return {u: d for u, d in ex.map(one, todo) if d}


def _img(src: Optional[str]) -> str:
    return f'<img src="{_e(src)}" alt="" loading="lazy">' if src else '<span class="noimg"></span>'


def _page(title: str, body: str) -> str:
    return (f"<title>{_e(title)}</title>\n<meta name=\"claude:viewport-fit-sides\" content=\"cover\">\n"
            f"{_FONTS}\n{_CSS}\n<div class=\"wrap\">\n{body}\n</div>\n")


def _breakdown(parts: list[tuple[str, float, str]]) -> str:
    total = sum(v for _, v, _ in parts if v > 0) or 1
    bar = "".join(f'<span style="width:{v / total * 100:.2f}%;background:var({c})"></span>'
                  for _, v, c in parts if v > 0)
    legend = "".join(f'<span><i style="background:var({c})"></i>{_e(n)} <span class="num">{eur(v)}</span></span>'
                     for n, v, c in parts if v)
    return f'<div class="bar" role="img" aria-label="Zusammensetzung">{bar}</div><div class="legend">{legend}</div>'


def _delta(rng) -> tuple[str, str]:
    """('−3,64 bis −7,21 €', 'down') for a (lo, hi) EUR change."""
    lo, hi = sorted(rng)
    sign = lambda x: ("+" if x > 0 else "−" if x < 0 else "±") + eur(abs(x))
    text = sign(lo) if abs(hi - lo) < 0.01 else f"{sign(lo)} bis {sign(hi)}"
    return text, ("up" if lo > 0 else "down" if hi < 0 else "mixed")


def _decision(d: dict) -> str:
    pill = ""
    if d.get("price"):
        text, cls = _delta(d["price"])
        pill = f'<span class="pill {cls}">{_e(text)}</span>'
    date = (f'<div class="date">Alles da: <s>{_e(d["date"][0])}</s> → <b>{_e(d["date"][1])}</b></div>'
            if d.get("date") else "")
    facts = "".join(f"<p>{_e(f)}</p>" for f in d.get("facts") or [])
    link = (f'<a href="{_e(d["link"])}" target="_blank" rel="noopener">{_e((d.get("link_text") or "Angebot")[:70])}</a>'
            if d.get("link") else "")
    warn = f'<p class="warn">{_e(d["warn"])}</p>' if d.get("warn") else ""
    img = f'<img src="{_e(d["img"])}" alt="">' if d.get("img") else ""
    return (f'<article class="card {d.get("kind", "")}">{img}<div class="c">'
            f'<div class="ct"><h3>{_e(d["title"])}</h3>{pill}</div>{date}{facts}{warn}{link}</div></article>')


def _head(eyebrow: str, title: str, label: str, total: Optional[float]) -> str:
    right = (f'<div class="sum"><div class="eyebrow">{label}</div><div class="total">{eur(total)}</div></div>'
             if total is not None else "")
    return f'<header class="head"><div><div class="eyebrow">{eyebrow}</div><h1>{_e(title)}</h1></div>{right}</header>'


def _money(goods: float, ship: float, duty: float, off: float) -> str:
    return _breakdown([("Ware", goods, "--goods"), ("Versand", ship, "--ship"),
                       ("Einfuhrabgaben", duty, "--duty"), ("Rabatte", -off, "--off")])


def _kind_chip(kind: str) -> str:
    return f'<span class="chip {"choice" if kind == "Choice" else "global"}">{_e(kind)}</span>'


def _is_decision(d: dict) -> bool:
    """A real decision changes price or date; everything else is a note."""
    lo, hi = d.get("price") or (0, 0)
    return abs(lo) >= 0.005 or abs(hi) >= 0.005 or bool(d.get("date"))


def _cart_row(it: dict) -> str:
    chips = []
    if it.get("slowest"):
        chips.append('<span class="chip global">am langsamsten</span>')
    if it.get("optional"):
        chips.append('<span class="chip off">zum Vergleich</span>')
    extra = f'<small>{_e(it["extra"])}</small>' if it.get("extra") else ""
    if (it.get("pieces") or 1) > 1:
        extra = f'<small>{it["pieces"]} Stk · {eur(it["unit"])}/Stk</small>' + extra
    return (f'<div class="row">{_img(it.get("img"))}<div class="t">'
            f'<a class="need" href="{_e(it["url"])}" target="_blank" rel="noopener" title="{_e(it["title"])}">'
            f'{_e(it["need"])}</a><div class="meta"><span>{it["qty"]}× {_e(it["variant"])}</span>'
            f'<span>bis {_e(it["until"])}</span>{"".join(chips)}</div></div>'
            f'<div class="money"><b>{eur(it["cost"])}</b>{extra}</div></div>')


def _cart_list(L: dict, number_from: int) -> tuple[str, int]:
    """One list as a section of the page. Returns (html, next decision number)."""
    items = L["items"]
    total = L.get("total")
    right = f'<span class="total sm">{eur(total)}</span>' if total is not None else ""
    out = [f'<div class="lh"><h2>{_e(L["name"])} <span class="eyebrow">{len(items)} Teile</span></h2>{right}</div>']
    if L.get("latest"):
        out.append(f'<p class="lead">Alles da bis <b>{_e(L["latest"])}</b>'
                   + (f' — am längsten braucht {_e(L["slowest"])}.' if L.get("slowest") else ".") + "</p>")
    decs = [d for d in L.get("decisions") or [] if _is_decision(d)]
    notes = [d for d in L.get("decisions") or [] if not _is_decision(d)]
    n = number_from
    if decs:
        cards = []
        for d in decs:
            cards.append(_decision(dict(d, title=f"{n}. {d['title']}")))
            n += 1
        out.append('<div class="eyebrow">Zu entscheiden — jede Frage liegt als Karte im Chat</div>'
                   '<div class="cards">' + "".join(cards) + "</div>")
    for d in notes:
        text = " ".join([d["title"]] + list(d.get("facts") or []) + ([d["warn"]] if d.get("warn") else []))
        out.append(f'<div class="note"><p>{_e(text)}</p></div>')
    if L.get("summary"):
        out.append(f'<div class="note"><p>{_e(L["summary"])}</p></div>')
    if L.get("mismatch"):
        out.append(f'<div class="note warn"><p>Zeilen und Kassenseite weichen um {eur(abs(L["mismatch"]))} ab. '
                   "Meist ist eine Zeile an der Kasse nicht bestellbar, dann zählt sie dort nicht mit. "
                   "Die Kassensumme gilt.</p></div>")
    for kind in ("Choice", "global"):
        rows = [it for it in items if it["kind"] == kind]
        if not rows:
            continue
        label = ("Choice — Versand gemeinsam, Abgabe gemeinsam" if kind == "Choice"
                 else "Globale Händler — Versand und Abgabe je Zeile")
        sub = sum(it["cost"] for it in rows)
        duty = (L.get("duty_choice") or 0.0) if kind == "Choice" else 0.0
        tail = ('<div class="row tail"><div></div><div class="t"><span class="meta">Einfuhrabgabe für alle Choice-Zeilen</span></div>'
                f'<div class="money"><b>{eur(duty)}</b></div></div>' if duty else "")
        out.append(f'<div class="lh g"><span class="eyebrow">{label}</span><span class="num">{eur(sub + duty)}</span></div>'
                   f'<div class="rows">{"".join(_cart_row(it) for it in rows)}{tail}</div>')
    if total is not None:
        out.append(_money(L["goods"], L["ship"], L["duty"], L["discount"]))
        out.append(f'<div class="legend"><span>Kasse: Ware + Versand + Abgabe − Rabatt = <b class="num">{eur(total)}</b></span></div>')
    out.append(f'<div class="go"><a class="btn" href="{_e(L["checkout_url"])}" target="_blank" rel="noopener">'
               f'Zur Kasse mit allen {len(items)} Teilen</a><span>Nichts wird bestellt, bis du dort bezahlst.</span></div>')
    return f'<section class="list">{"".join(out)}</section>', n


def cart_page(lists: list[dict], others: list[dict], stamp: str) -> str:
    """
    One page for the whole cart: one header, one total, a section per list.

    lists: [{name, total, goods, ship, duty, duty_choice, discount, mismatch, latest, slowest, summary,
             checkout_url, decisions: [{kind, title, img, link, link_text, facts,
             price: (lo, hi) | None, date: (old, new) | None, warn}],
             items: [{img, need, title, url, qty, variant, cost, extra, kind, until,
                      slowest, optional}]}]
    others: [{img, title, url, qty, cost}]
    Decisions that change neither price nor date are shown as notes, not as questions.
    """
    totals = [L["total"] for L in lists if L.get("total") is not None]
    grand = sum(totals) if totals and len(totals) == len(lists) else None
    n_items = sum(len(L["items"]) for L in lists)
    out = [_head(f"Warenkorb · {n_items} Teile auf {len(lists)} Listen" if len(lists) > 1 else f"Warenkorb · {n_items} Teile",
                 lists[0]["name"] if len(lists) == 1 else "Übersicht", "an der Kasse", grand)]
    n = 1
    for L in lists:
        html, n = _cart_list(L, n)
        out.append(html)
    if others:
        rows = "".join(
            f'<div class="row">{_img(o.get("img"))}<div class="t">'
            f'<a class="need" href="{_e(o["url"])}" target="_blank" rel="noopener">{_e(o["title"][:70])}</a>'
            f'<div class="meta"><span>{o["qty"]}×</span></div></div>'
            f'<div class="money"><b>{eur(o["cost"])}</b></div></div>' for o in others)
        out.append(f'<details class="others"><summary>Sonst im Korb: {len(others)} Zeilen auf keiner Liste</summary>'
                   f'<div class="rows">{rows}</div></details>')
    out.append(f"<footer>Stand {_e(stamp)}. Preise von der Kassenseite, nichts bestellt. Globale Zeilen enthalten ihre "
               "Einfuhrabgabe, bei Choice steht sie einmal unter der Gruppe.</footer>")
    return _page("Warenkorb " + (lists[0]["name"] if len(lists) == 1 else "Übersicht"), "\n".join(out))


def plan_page(name: str, picks: list[dict], total: dict, stamp: str) -> str:
    """
    picks: [{need, pieces, why, winner: offer, alts: [offer + diff]}],
      offer = {img, title, url, buy, net, unit, kind, days}
    total: {goods, ship, duty, saved, total, slowest}
    """
    def offer_row(o: dict, alt: bool = False) -> str:
        diff = (f'<small>{"+" if o["diff"] >= 0 else "−"}{eur(abs(o["diff"]))}</small>' if alt else
                f'<small>{eur(o["unit"])}/Stk</small>')
        return (f'<div class="row{" alt" if alt else ""}">{_img(o.get("img"))}<div class="t">'
                f'<a class="title" href="{_e(o["url"])}" target="_blank" rel="noopener">{_e(o["title"])}</a>'
                f'<div class="meta"><span>{_e(o["buy"])}</span>'
                f'{_kind_chip(o["kind"])}'
                f'<span>{_e(o["days"])}</span></div></div>'
                f'<div class="money"><b>{eur(o["net"])}</b>{diff}</div></div>')

    out = [_head(f"Vergleich · {len(picks)} Teile", name, "Plan gesamt", total["total"]),
           _money(total["goods"], total["ship"], total["duty"], total["saved"])]
    for p in picks:
        alts = "".join(offer_row(a, True) for a in p["alts"])
        why = f'<div class="why">{_e(p["why"])}</div>' if p.get("why") else ""
        out.append(f'<section><div><div class="eyebrow">{p["pieces"]} Stück</div><h2>{_e(p["need"])}</h2>{why}</div>'
                   f'<div class="rows">{offer_row(p["winner"])}{alts}</div></section>')
    if total.get("slowest"):
        out.append(f'<div class="legend">Langsamstes Teil: {_e(total["slowest"])}</div>')
    out.append(f"<footer>Stand {_e(stamp)}. Preis je Zeile = Ware + Versand + Einfuhrabgabe − automatische Rabatte; "
               "die Alternativen zeigen den Aufpreis zum gewählten Angebot.</footer>")
    return _page("Vergleich " + name, "\n".join(out))


_PICK_JS = r"""<script>
(function () {
  var key = "report:" + document.title, saved = {};
  try { saved = JSON.parse(localStorage.getItem(key) || "{}"); } catch (e) {}
  var secs = [].slice.call(document.querySelectorAll("section[data-sec]")).filter(function (s) { return s.querySelector("input[type=radio]"); });
  secs.forEach(function (s) {
    var i = s.getAttribute("data-i"), r = saved["s" + i], n = saved["n" + i];
    if (/^\d+$/.test(String(r))) { var el = s.querySelector('input[value="' + r + '"]'); if (el) el.checked = true; }
    if (n) { s.querySelector(".sec-note").value = n; s.querySelector("details.nt").open = true; }
  });
  function render() {
    var codes = [], st = {}, changed = 0;
    secs.forEach(function (s) {
      var i = s.getAttribute("data-i"), r = s.querySelector("input[type=radio]:checked");
      st["s" + i] = r.value; st["n" + i] = s.querySelector(".sec-note").value.trim();
      if (r.value !== "0") changed++;
      codes.push(String.fromCharCode(65 + Number(i)) + (Number(r.value) + 1));
    });
    document.getElementById("pk-out").textContent = codes.join(" ");
    document.getElementById("pk-msg").textContent = changed ? changed + " geändert" : "wie vorgeschlagen";
    try { localStorage.setItem(key, JSON.stringify(st)); } catch (e) {}
  }
  document.addEventListener("input", render);
  render();
})();
</script>"""

_LB_JS = r"""<script>
(function () {
  var lb = document.getElementById("lb"), big = lb.querySelector("img");
  document.addEventListener("click", function (e) {
    var im = e.target.closest && e.target.closest(".row img");
    if (im) { e.preventDefault(); big.src = im.src; lb.className = "on"; }
    else if (lb.className === "on") lb.className = "";
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") lb.className = ""; });
})();
</script>"""



def generic_page(title: str, subtitle: str, facts: list[dict], notes: list[dict],
                 sections: list[dict], stamp: str) -> str:
    """
    The one page layout every report uses: header, summary figures, notes, then
    sections with one row per article (small picture). Raises ValueError when a card has no picture.

    facts: [{label, value}]   notes: [{text, warn: bool}]
    sections: [{heading, intro, pick (radio per card, first card = chosen), cards: [{title, img (data: URI), price, tags, text,
                url, link_text, chosen}]}]
    """
    missing = [c["title"] for sec in sections for c in sec["cards"] if not c.get("img")]
    if missing:
        raise ValueError("no picture for: " + "; ".join(missing))
    out = [f'<header class="head"><div><div class="eyebrow">{_e(subtitle)}</div><h1>{_e(title)}</h1></div></header>']
    if facts:
        out.append('<div class="facts">' + "".join(
            f'<div class="fact"><b>{_e(f["value"])}</b><span>{_e(f["label"])}</span></div>' for f in facts) + "</div>")
    for n in notes:
        out.append(f'<div class="note{" warn" if n.get("warn") else ""}"><p>{_e(n["text"])}</p></div>')
    pickable = False
    for si, sec in enumerate(sections):
        intro = f'<p class="intro">{_e(sec["intro"])}</p>' if sec.get("intro") else ""
        cards = sec["cards"]
        pick = bool(sec.get("pick")) and len(cards) > 1
        pickable = pickable or pick
        rows = []
        for ci, c in enumerate(cards):
            chips = "".join(f'<span class="chip{" ok" if t.startswith("✓") else ""}">{_e(t)}</span>'
                            for t in c.get("tags") or [])
            if pick:
                chips = f'<span class="code">{chr(65 + si)}{ci + 1}</span>' + chips
            name = (f'<a class="need" href="{_e(_url(c["url"]))}" target="_blank" rel="noopener">{_e(c["title"])}</a>'
                    if c.get("url") else f'<span class="need">{_e(c["title"])}</span>')
            kind = "why" if ci == 0 else "diff"
            text = f'<span class="{kind}">{_e(c["text"])}</span>' if c.get("text") else ""
            price = f'<b>{_e(c["price"])}</b>' if c.get("price") else ""
            radio = (f'<input type="radio" name="s{si}" value="{ci}" data-title="{_e(c["title"])}" '
                     f'data-url="{_e(_url(c.get("url")))}" data-price="{_e(c.get("price") or "")}" '
                     f'{"checked" if ci == 0 else ""} aria-label="{_e(c["title"])} wählen">' if pick else "")
            cls = "row" + (" pick" if pick else "") + (" chosen" if ci == 0 and c.get("chosen") else "") \
                + (" alt" if pick and ci > 0 else "")
            rows.append(f'<div class="{cls}">{radio}{_img(c["img"])}'
                        f'<div class="t">{name}<div class="meta">{chips}</div>{text}</div>'
                        f'<div class="money">{price}</div></div>')
        note = (f'<details class="nt"><summary>Notiz</summary><input class="sec-note" type="text" name="n{si}" '
                f'placeholder="Notiz zu {_e(sec["heading"])}"></details>' if pick else "")
        out.append(f'<section data-sec="{_e(sec["heading"])}" data-i="{si}"><h2>{_e(sec["heading"])}</h2>{intro}'
                   f'<div class="rows">{"".join(rows)}</div>{note}</section>')
    if pickable:
        out.append('<div class="pickbar"><span>Wahl: <code id="pk-out"></code></span>'
                   '<span id="pk-msg" class="eyebrow"></span></div>')
        out.append(_PICK_JS)
    out.append('<div id="lb" role="dialog" aria-label="Bild"><img alt=""></div>')
    out.append(_LB_JS)
    out.append(f"<footer>Stand {_e(stamp)}. Nichts bestellt, Bezahlen ist dein Klick.</footer>")
    return _page(title, "\n".join(out))
