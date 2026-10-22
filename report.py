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
  --bg: #f5f7f6; --surface: #ffffff; --fg: #1b2420; --muted: #5d6b65; --line: #dbe2df;
  --accent: #0f7a5c; --warn: #b4540a; --warn-bg: #fdf1e6; --goods: #2f6f8f; --ship: #8a6bb8;
  --duty: #c2593a; --off: #7fa892;
  --display: "Barlow Semi Condensed", "Arial Narrow", sans-serif;
  --body: "Source Sans 3", "Segoe UI", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #121715; --surface: #1a211e; --fg: #e3ebe7; --muted: #95a59e; --line: #2c3632;
  --accent: #4cc39b; --warn: #f0a35c; --warn-bg: #2e2318; --goods: #6fb1d1; --ship: #b49ae0;
  --duty: #e8876a; --off: #6f9a84; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #121715; --surface: #1a211e; --fg: #e3ebe7; --muted: #95a59e; --line: #2c3632;
  --accent: #4cc39b; --warn: #f0a35c; --warn-bg: #2e2318; --goods: #6fb1d1; --ship: #b49ae0;
  --duty: #e8876a; --off: #6f9a84; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font: 15px/1.45 var(--body); }
.wrap { max-width: 860px; margin: 0 auto; padding-block: 20px 40px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 28px; }
h1, h2 { font-family: var(--display); font-weight: 600; margin: 0; text-wrap: balance; letter-spacing: .01em; }
h1 { font-size: 1.9rem; } h2 { font-size: 1.35rem; }
.eyebrow { font: 600 .72rem/1 var(--body); text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
.num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
a { color: var(--accent); }
a:focus-visible, .btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
section { display: grid; gap: 14px; }
.head { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: 12px; }
.total { font: 600 2.4rem/1 var(--mono); font-variant-numeric: tabular-nums; }
.bar { display: flex; height: 12px; border-radius: 6px; overflow: hidden; background: var(--line); }
.bar span { display: block; height: 100%; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--muted); font-size: .9rem; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }
.rows { display: grid; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; }
.row { display: grid; grid-template-columns: 64px minmax(0, 1fr) auto; gap: 12px; align-items: center;
  padding: 10px 14px; border-top: 1px solid var(--line); }
.row:first-child { border-top: 0; }
.row img, .row .noimg { width: 64px; height: 64px; object-fit: cover; border-radius: 6px; background: var(--line); }
.row .t { min-width: 0; display: grid; gap: 2px; }
.row .need { font-weight: 600; }
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
.notes { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.notes li { background: var(--warn-bg); border-radius: 8px; padding: 8px 12px; }
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
</style>
"""

_FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Semi+Condensed:wght@600'
          '&family=IBM+Plex+Mono:wght@400;600&family=Source+Sans+3:wght@400;600&display=swap">')


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

    def small(u: str) -> str:
        # `fetch` asks the CDN for the small size itself.
        return "https:" + u if u.startswith("//") else u

    def one(u: str) -> tuple[str, Optional[str]]:
        try:
            data = fetch(small(u))
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


def cart_page(lists: list[dict], others: list[dict], stamp: str) -> str:
    """
    lists: [{name, total, goods, ship, duty, discount, latest, slowest, summary,
             checkout_url, decisions: [{kind, title, img, link, link_text, facts,
             price: (lo, hi) | None, date: (old, new) | None, warn}],
             items: [{img, need, title, url, qty, variant, cost, extra, kind, until,
                      slowest, optional}]}]
    others: [{img, title, url, qty, cost}]
    """
    out = []
    for L in lists:
        top = (f'<header class="head"><div><div class="eyebrow">Einkaufsliste · {len(L["items"])} Teile</div>'
               f'<h1>{_e(L["name"])}</h1></div>')
        if L.get("total") is not None:
            top += (f'<div class="sum"><div class="eyebrow">an der Kasse</div>'
                    f'<div class="total">{eur(L["total"])}</div></div>')
        top += "</header>"
        lead = ""
        if L.get("latest"):
            lead = (f'<p class="lead">Alles da bis <b>{_e(L["latest"])}</b>'
                    + (f' — am längsten braucht {_e(L["slowest"])}.' if L.get("slowest") else ".") + "</p>")
        rec = f'<div class="rec"><div class="eyebrow">Empfehlung</div><p>{_e(L["summary"])}</p></div>' \
            if L.get("summary") else ""
        dec = ""
        if L.get("decisions"):
            dec = ('<section><h2>Zu entscheiden</h2><div class="cards">'
                   + "".join(_decision(d) for d in L["decisions"]) + "</div></section>")
        money = ""
        if L.get("total") is not None:
            money = _breakdown([("Ware", L["goods"], "--goods"), ("Versand", L["ship"], "--ship"),
                                ("Einfuhrabgaben", L["duty"], "--duty"), ("Rabatte", -L["discount"], "--off")])
        go = (f'<div class="go"><a class="btn" href="{_e(L["checkout_url"])}" target="_blank" rel="noopener">'
              f'Zur Kasse mit allen {len(L["items"])} Teilen</a><span>Nichts wird bestellt, bis du dort bezahlst.</span></div>')
        rows = []
        for it in L["items"]:
            chips = [f'<span class="chip {"choice" if it["kind"] == "Choice" else "global"}">{_e(it["kind"])}</span>']
            if it.get("slowest"):
                chips.append('<span class="chip global">am langsamsten</span>')
            if it.get("optional"):
                chips.append('<span class="chip off">zum Vergleich</span>')
            extra = f'<small>{_e(it["extra"])}</small>' if it.get("extra") else ""
            if (it.get("pieces") or 1) > 1:
                extra = f'<small>{it["pieces"]} Stk · {eur(it["unit"])}/Stk</small>' + extra
            rows.append(
                f'<div class="row">{_img(it.get("img"))}<div class="t">'
                f'<a class="need" href="{_e(it["url"])}" target="_blank" rel="noopener" title="{_e(it["title"])}">'
                f'{_e(it["need"])}</a><div class="meta"><span>{it["qty"]}× {_e(it["variant"])}</span>'
                f'<span>bis {_e(it["until"])}</span>{"".join(chips)}</div></div>'
                f'<div class="money"><b>{eur(it["cost"])}</b>{extra}</div></div>')
        parts = (f'<section><h2>Die Teile</h2>{money}<div class="rows">{"".join(rows)}</div></section>')
        out.append(top + lead + rec + dec + go + parts)
    if others:
        rows = "".join(
            f'<div class="row">{_img(o.get("img"))}<div class="t">'
            f'<a class="need" href="{_e(o["url"])}" target="_blank" rel="noopener">{_e(o["title"][:70])}</a>'
            f'<div class="meta"><span>{o["qty"]}×</span></div></div>'
            f'<div class="money"><b>{eur(o["cost"])}</b></div></div>' for o in others)
        out.append(f'<details class="others"><summary>Sonst im Korb: {len(others)} Zeilen auf keiner Liste</summary>'
                   f'<div class="rows">{rows}</div></details>')
    out.append(f"<footer>Stand {_e(stamp)}. Preise von der Kassenseite, nichts bestellt. Preis je Teil = Ware + Versand "
               "+ Einfuhrabgabe (Choice-Teile zahlen ihre Abgabe gemeinsam, siehe oben).</footer>")
    return _page("Warenkorb " + (lists[0]["name"] if lists else "Übersicht"), "\n".join(out))


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
                f'<span class="chip {"choice" if o["kind"] == "Choice" else "global"}">{_e(o["kind"])}</span>'
                f'<span>{_e(o["days"])}</span></div></div>'
                f'<div class="money"><b>{eur(o["net"])}</b>{diff}</div></div>')

    out = [f'<div class="head"><div><div class="eyebrow">Vergleich · {len(picks)} Teile</div><h1>{_e(name)}</h1></div>'
           f'<div style="text-align:right"><div class="eyebrow">Plan gesamt</div>'
           f'<div class="total">{eur(total["total"])}</div></div></div>',
           _breakdown([("Ware", total["goods"], "--goods"), ("Versand", total["ship"], "--ship"),
                       ("Einfuhrabgaben", total["duty"], "--duty"), ("Rabatte", -total["saved"], "--off")])]
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
