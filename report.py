"""
The cart and a shopping-list comparison as one self-contained HTML page.

The chat cannot embed pictures, so view_cart and shop_list also write a page
(pictures inlined as data: URIs, nothing loaded from AliExpress) that the
MCP serves on the local network (reportserver.py). Same file name every time, so the
address stays the same.

The functions take plain row dicts (built in the server) and return HTML.
"""

import base64
import html
from typing import Callable, Optional

_CSS = """
<style>
/* Layout: one column, summary first, then parts as rows (picture | text | money). */
:root {
  --bg: #f5f5f3; --surface: #ffffff; --fg: #222421; --muted: #6a6e68; --line: #dfe0db;
  --accent: #2f7d5b; --warn: #b4540a; --warn-bg: #fdf1e6; --goods: #7c9a8b; --ship: #b9bcb4; --duty: #c9a063; --off: #2f7d5b;
  --display: "Barlow Semi Condensed", "Arial Narrow", sans-serif;
  --body: "Source Sans 3", "Segoe UI", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #151715; --surface: #1d201d; --fg: #e6e8e3; --muted: #9aa097; --line: #2e322d;
  --accent: #6fbf97; --warn: #f0a35c; --warn-bg: #2e2318; --goods: #6f8f7f; --ship: #6c706a;
  --duty: #c9a063; --off: #6fbf97; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #151715; --surface: #1d201d; --fg: #e6e8e3; --muted: #9aa097; --line: #2e322d;
  --accent: #6fbf97; --warn: #f0a35c; --warn-bg: #2e2318; --goods: #6f8f7f; --ship: #6c706a;
  --duty: #c9a063; --off: #6fbf97; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font: 14px/1.4 var(--body); }
.wrap { max-width: 1040px; margin: 0 auto; padding-block: 20px 40px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 28px; }
h1, h2 { font-family: var(--display); font-weight: 600; margin: 0; text-wrap: balance; letter-spacing: .01em; }
h1 { font-size: 1.15rem; } h2 { font-size: 1.05rem; }
.eyebrow { font: 600 .72rem/1 var(--body); text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
.num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
a { color: var(--accent); }
a:focus-visible, .btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
section { display: grid; gap: 14px; }
.head { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: 12px; }
.total { font: 600 1.35rem/1 var(--mono); font-variant-numeric: tabular-nums; }
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
.btn.ghost { background: transparent; color: var(--accent); border: 1px solid var(--accent); }
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
    lo, hi = sorted(rng) if isinstance(rng, (tuple, list)) else (rng, rng)
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
    link = (f'<a href="{_e(_url(d["link"]))}" target="_blank" rel="noopener">{_e((d.get("link_text") or "Angebot")[:70])}</a>'
            if _url(d.get("link")) else "")
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
    p = d.get("price")
    lo, hi = p if isinstance(p, (tuple, list)) and len(p) == 2 else ((p, p) if isinstance(p, (int, float)) else (0, 0))
    return abs(lo) >= 0.005 or abs(hi) >= 0.005 or bool(d.get("date"))


def _teile(n: int) -> str:
    return "1 Teil" if n == 1 else f"{n} Teile"


def _cart_row(it: dict) -> str:
    chips = []
    if it.get("list"):
        chips.append(f'<span class="chip">{_e(it["list"])}</span>')
    if it.get("slowest"):
        chips.append('<span class="chip global">am langsamsten</span>')
    if it.get("optional"):
        chips.append('<span class="chip off">zum Vergleich</span>')
    extra = f'<small>{_e(it["extra"])}</small>' if it.get("extra") else ""
    if (it.get("pieces") or 1) > 1:
        extra = f'<small>{it["pieces"]} Stk · {eur(it["unit"])}/Stk</small>' + extra
    return (f'<div class="row">{_img(it.get("img"))}<div class="t">'
            f'<a class="need" href="{_e(_url(it["url"]))}" target="_blank" rel="noopener" title="{_e(it["title"])}">'
            f'{_e(it["need"])}</a><div class="meta"><span>{it["qty"]}× {_e(it["variant"])}</span>'
            f'<span>bis {_e(it["until"])}</span>{"".join(chips)}</div></div>'
            f'<div class="money"><b>{eur(it["cost"])}</b>{extra}</div></div>')


def _combo_section(c: dict) -> str:
    """The Kombi-Knaller basket: its own order, import charge gone from 3 pieces, ordered in rounds."""
    def row(it: dict) -> str:
        chips = '<span class="chip choice">angehakt</span>' if it["checked"] else '<span class="chip off">wartet</span>'
        return (f'<div class="row{"" if it["checked"] else " alt"}">{_img(it.get("img"))}<div class="t">'
                f'<a class="need" href="{_e(_url(it["url"]))}" target="_blank" rel="noopener" title="{_e(it["title"])}">'
                f'{_e(it["need"])}</a><div class="meta"><span>{it["qty"]}× {_e(it["variant"])}</span>{chips}</div></div>'
                f'<div class="money"><b>{eur(it["cost"])}</b></div></div>')
    rows = c["rows"]
    covered = c["pieces"] >= 3
    out = [f'<div class="lh g"><span><b>Kombi-Knaller-Korb</b> <span class="eyebrow">{_teile(len(rows))} · '
           f'eigene Kasse, ab 3 Stück ohne Einfuhrabgabe</span></span><span class="num">{eur(c["goods"])}</span></div>',
           f'<div class="note{"" if covered else " warn"}"><p>Angehakt: {c["pieces"]} Stück, Ware {eur(c["goods"])}. '
           + ("Ab 3 Stück: keine Einfuhrabgabe." if covered else "Unter 3 Stück: die Einfuhrabgabe ist NICHT gedeckt.")
           + " Dieser Korb wird getrennt vom Hauptkorb bestellt, die Summe oben gilt nur für den Hauptkorb.</p></div>",
           f'<div class="rows">{"".join(row(it) for it in rows)}</div>']
    for rd in c.get("rounds") or []:
        out.append(f'<div class="note"><p><b>Bestellung {rd["n"]}:</b> {_e(rd["names"])} — {_e(rd["state"])}</p></div>')
    if c.get("url"):
        out.append(f'<div class="go"><a class="btn" href="{_e(_url(c["url"]))}" target="_blank" rel="noopener">'
                   f'Kombi-Kasse ({c["pieces"]} Stück)</a><span>Nichts wird bestellt, bis du dort bezahlst.'
                   ' „Limit exceeded“: in mehreren Bestellungen bestellen.</span></div>')
    return "\n".join(out)


_GROUPS = (("Choice", "Choice", "Versand und Einfuhrabgabe gemeinsam"),
           ("global", "Globale Händler", "Versand je Zeile"))


def cart_page(page: dict, stamp: str) -> str:
    """
    One page for the whole cart: one header, one total, one checkout button, rows grouped by channel.

    page: {name, total, goods, ship, duty, discount, mismatch, latest, slowest, summary,
           checkout_url, checkout_all_url,
           decisions: [{kind, title, img, link, link_text, facts, price: (lo, hi) | None,
                        date: (old, new) | None, warn}],
           items: [{img, need, title, url, qty, variant, cost (goods + shipping), extra, kind, list,
                    until, slowest, optional, pieces, unit}],
           unlisted: [{img, title, url, qty, cost}]}
    Row prices are goods + shipping; the import charge is part of the total only.
    Decisions that change neither price nor date are shown as notes, not as questions.
    """
    items, total = page["items"], page.get("total")
    out = [_head(f"Warenkorb · {_teile(len(items))}", page.get("name") or "Übersicht", "an der Kasse", total)]
    if page.get("latest"):
        out.append(f'<p class="lead">Alles da bis <b>{_e(page["latest"])}</b>'
                   + (f' — am längsten braucht {_e(page["slowest"])}.' if page.get("slowest") else ".") + "</p>")
    if total is not None:
        out.append(_money(page["goods"], page["ship"], page["duty"], page["discount"]))
        out.append(f'<div class="legend"><span>Kasse: Ware + Versand + Einfuhrabgabe − Rabatt = '
                   f'<b class="num">{eur(total)}</b></span></div>')
    if items:
        out.append(f'<div class="go"><a class="btn" href="{_e(_url(page.get("checkout_url")))}" target="_blank" '
                   f'rel="noopener">Alles zur Kasse ({_teile(len(items))})</a>'
                   '<span>Nichts wird bestellt, bis du dort bezahlst.</span></div>')
    if page.get("summary"):
        out.append(f'<div class="note"><p>{_e(page["summary"])}</p></div>')
    if page.get("mismatch"):
        out.append(f'<div class="note warn"><p>Zeilen und Kassenseite weichen um {eur(abs(page["mismatch"]))} ab. '
                   "Meist ist eine Zeile an der Kasse nicht bestellbar, oder es kommt Versand je Bestellung dazu. "
                   "Die Kassensumme gilt.</p></div>")
    decs = [d for d in page.get("decisions") or [] if _is_decision(d)]
    notes = [d for d in page.get("decisions") or [] if not _is_decision(d)]
    if decs:
        out.append('<div class="eyebrow">Zu entscheiden — jede Frage liegt als Karte im Chat</div><div class="cards">'
                   + "".join(_decision(dict(d, title=f"{i}. {d.get('title') or ''}")) for i, d in enumerate(decs, 1))
                   + "</div>")
    for d in notes:
        text = " ".join(str(x) for x in [d.get("title")] + list(d.get("facts") or []) + [d.get("warn")] if x)
        out.append(f'<div class="note"><p>{_e(text)}</p></div>')
    for kind, label, how in _GROUPS:
        rows = [it for it in items if (it["kind"] == "Choice") == (kind == "Choice")]  # unknown kinds count as global
        if rows:
            out.append(f'<div class="lh g"><span><b>{label}</b> <span class="eyebrow">{_teile(len(rows))} · {how}</span></span>'
                       f'<span class="num">{eur(sum(it["cost"] for it in rows))}</span></div>'
                       f'<div class="rows">{"".join(_cart_row(it) for it in rows)}</div>')
    combo = page.get("combo")
    if combo:
        out.append(_combo_section(combo))
    others = page.get("unlisted") or []
    if others:
        rows = "".join(
            f'<div class="row">{_img(o.get("img"))}<div class="t">'
            f'<a class="need" href="{_e(_url(o["url"]))}" target="_blank" rel="noopener">{_e(str(o.get("title") or "")[:70])}</a>'
            f'<div class="meta"><span>{o["qty"]}×</span></div></div>'
            f'<div class="money"><b>{eur(o["cost"])}</b></div></div>' for o in others)
        out.append(f'<div class="lh g"><span><b>Ohne Liste</b> <span class="eyebrow">{_teile(len(others))} · '
                   f'im Korb, aber nicht im Gesamtpreis oben</span></span>'
                   f'<span class="num">{eur(sum(o["cost"] for o in others))}</span></div>'
                   f'<div class="rows">{rows}</div>'
                   f'<div class="go"><a class="btn{" ghost" if items else ""}" href="{_e(_url(page.get("checkout_all_url")))}" '
                   f'target="_blank" rel="noopener">{"Auch diese mitnehmen: " if items else ""}Kasse mit allen '
                   f'{len(items) + len(others)} Zeilen</a></div>')
    out.append(f"<footer>Stand {_e(stamp)}. Zeilenpreise = Ware + Versand"
               + ("; die Einfuhrabgabe steht nur im Gesamtpreis" if total is not None else "; ohne Kassen-Vorschau, Einfuhrabgabe fehlt")
               + ". Preise von der Kassenseite, nichts bestellt.</footer>")
    return _page("Warenkorb " + (page.get("name") or "Übersicht"), "\n".join(out))


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
                f'<a class="title" href="{_e(_url(o["url"]))}" target="_blank" rel="noopener">{_e(o["title"])}</a>'
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


_ICON_YES = '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
_ICON_NO = '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
_ICON_DEL = '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.8 4.5l.6 8.2h5.2l.6-8.2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>'
_ICON_MAYBE = '<span aria-hidden="true" style="font-weight:700">?</span>'

_DECIDE_CSS = """<style>
/* decision page, concept B: one column, a big picture on the left, on the right title, price, the agent's opinion and three
   icon buttons. Tap the picture = mark, tap the text = details. No page header, one slim bar at the bottom. */
.wrap { gap: 0; padding-block: 0 84px; max-width: 720px; padding-left: max(6px, env(safe-area-inset-left, 0px)); padding-right: max(6px, env(safe-area-inset-right, 0px)); }
.hints { padding: 6px 2px; }
.hints .note { margin-top: 6px; }
.hints summary { cursor: pointer; color: var(--muted); font-size: .85rem; }
.cat-h { position: sticky; top: 0; z-index: 5; display: flex; align-items: center; gap: 10px; padding: 9px 2px 7px;
  background: var(--bg); border-bottom: 1px solid var(--line); }
.cat-h h2 { flex: 1; min-width: 0; margin: 0; font: 600 .95rem var(--body); letter-spacing: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cat-h .sub { font: .78rem var(--mono); color: var(--muted); white-space: nowrap; }
.cat-h button { font: inherit; font-size: .78rem; background: none; border: 0; padding: 4px 2px; color: var(--accent); cursor: pointer; }
section[data-cat] .rows { display: block; background: none; border: 0; border-radius: 0; }
.row.dec { display: grid; grid-template-columns: 112px minmax(0, 1fr); gap: 4px 8px; align-items: start; padding: 8px 0;
  border-top: 1px solid var(--line); background: none; }
.row.dec:first-child { border-top: 0; }
.row.dec .thumb { grid-column: 1; grid-row: 1; position: relative; width: 112px; height: 112px; padding: 0; border: 0; background: var(--line);
  border-radius: 8px; overflow: hidden; cursor: pointer; }
.row.dec .thumb img, .row.dec .thumb .noimg { display: block; width: 112px; height: 112px; border-radius: 0; object-fit: cover; cursor: pointer; }
.row.marked .thumb::after { content: "\\2713"; position: absolute; inset: 0; display: grid; place-items: center;
  background: color-mix(in srgb, var(--accent) 62%, transparent); color: #fff; font-weight: 700; font-size: 2.2rem; }
.tx { grid-column: 2; grid-row: 1 / 3; min-width: 0; cursor: pointer; display: grid; gap: 2px; align-content: start; }
.tx .need { display: block; font: 600 .95rem/1.25 var(--body); color: var(--fg); text-decoration: none; overflow: hidden; white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.tx .pr { font: 600 1rem var(--mono); font-variant-numeric: tabular-nums; }
.tx .pr small { font: 400 .75rem var(--body); color: var(--muted); margin-left: 6px; }
.tx .vd { font-size: .8rem; line-height: 1.3; color: var(--muted); overflow: hidden; display: -webkit-box; -webkit-line-clamp: 5; -webkit-box-orient: vertical; }
.tx .vd b { font-weight: 700; }
.tx .vd .ok { color: var(--accent); } .tx .vd .q { color: var(--warn); }
.row.open .tx .vd { display: block; }
.ctl { grid-column: 1; grid-row: 2; }
.note-in { width: 100%; box-sizing: border-box; margin-top: 3px; padding: 3px 6px; font: .78rem var(--body); border: 1px solid var(--line); border-radius: 6px; background: var(--surface); color: var(--fg); }
.seg { display: grid; width: 112px; box-sizing: border-box; grid-auto-flow: column; grid-auto-columns: 1fr; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; position: relative; }
.seg input { position: absolute; opacity: 0; pointer-events: none; }
.seg label { height: 26px; line-height: 0; display: grid; place-items: center; cursor: pointer; border-left: 1px solid var(--line); color: var(--muted); background: var(--surface); }
.seg input:first-child + label { border-left: 0; }
.seg input:focus-visible + label { outline: 2px solid var(--accent); outline-offset: -2px; }
.seg input:checked + label.y { background: var(--accent); color: var(--surface); }
.seg input:checked + label.m { background: var(--duty); color: #222421; }
.seg input:checked + label.n { background: var(--ship); color: var(--fg); }
.seg input:checked + label.d { background: var(--warn); color: var(--surface); }
.more { grid-column: 1 / -1; grid-row: 3; display: none; font-size: .82rem; color: var(--muted); gap: 6px; padding-top: 2px; }
.row.open .more { display: grid; }
.more .meta { display: flex; flex-wrap: wrap; gap: 4px; }
.more .qrow { display: flex; align-items: center; gap: 8px; }
.qty { width: 3.4rem; padding: 6px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); color: var(--fg); font: inherit; }
.row.dec.is-no .thumb, .row.dec.is-no .tx { opacity: .55; }
.row.dec.is-del .thumb, .row.dec.is-del .tx { opacity: .35; }
.row.dec.is-del .need { text-decoration: line-through; }
.dbar { position: fixed; left: 0; right: 0; bottom: 0; z-index: 20; background: var(--surface); border-top: 1px solid var(--line);
  padding: 6px max(12px, env(safe-area-inset-left, 0px)) calc(6px + env(safe-area-inset-bottom, 0px)); }
.dbar .in { max-width: 720px; margin: 0 auto; display: grid; gap: 6px; }
.dbar .bulk { display: flex; align-items: center; gap: 6px; }
.dbar .bulk[hidden] { display: none; }
.dbar .bulk span { flex: 1; font-size: .85rem; font-weight: 600; }
.dbar .bulk button { width: 40px; height: 34px; display: grid; place-items: center; border-radius: 8px; border: 1px solid var(--line);
  background: var(--bg); color: var(--fg); cursor: pointer; font: inherit; }
.dbar .bulk button.y { background: var(--accent); border-color: var(--accent); color: var(--surface); }
.dbar .bulk button.d { background: var(--warn); border-color: var(--warn); color: var(--surface); }
.dbar .main { display: flex; align-items: center; gap: 8px; }
.dbar .dsum { flex: 1; min-width: 0; display: grid; }
.dbar .tot { font: 600 1.05rem var(--mono); font-variant-numeric: tabular-nums; line-height: 1.1; }
.dbar .st { font-size: .72rem; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dbar .send { font: inherit; font-weight: 600; cursor: pointer; border: 0; border-radius: 8px; padding: 0 16px; height: 38px;
  background: var(--accent); color: var(--surface); }
.dbar .cp { width: 38px; height: 38px; border-radius: 8px; border: 1px solid var(--line); background: var(--bg); color: var(--muted); cursor: pointer; font: inherit; }
.dbar textarea { width: 100%; min-height: 4.5rem; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--fg);
  font: .72rem var(--mono); padding: 6px 8px; box-sizing: border-box; }
</style>"""

_DECIDE_JS = r"""<script>
(function () {
  var NAME = __NAME__, TH = __TH__, CS = __CS__;
  var T = document.title, key = "decide:" + NAME, saved = {};
  try { saved = JSON.parse(localStorage.getItem(key) || "{}"); } catch (e) {}
  var rows = [].slice.call(document.querySelectorAll(".row.dec"));
  function f(x) { return x.toFixed(2).replace(".", ",").replace(/\B(?=(\d{3})+(?!\d))/g, ".") + " €"; }
  function st(r) { return r.querySelector("input[type=radio]:checked").value; }
  function qty(r) { return Math.max(1, parseInt(r.querySelector(".qty").value, 10) || 1); }
  rows.forEach(function (r) {
    var s = saved[r.dataset.id];
    if (s) { var el = r.querySelector('input[value="' + s.st + '"]'); if (el) el.checked = true; r.querySelector(".qty").value = s.q; if (s.n) r.querySelector(".note-in").value = s.n; }
  });
  var total = 0, stEl = document.getElementById("sb-state"), send = document.getElementById("sb-send");
  function calc() {
    var goods = 0, duty = 0, ship = 0, ch = 0, n = 0, keep = {};
    rows.forEach(function (r) {
      var v = st(r), q = qty(r); r.classList.toggle("is-no", v === "no" || v === "maybe"); r.classList.toggle("is-del", v === "delete");
      keep[r.dataset.id] = { st: v, q: q, n: r.querySelector('.note-in').value };
      r.querySelector(".qx").textContent = q > 1 ? "×" + q : "";
      if (v !== "yes") return;
      n++; var g = parseFloat(r.dataset.price) * q; goods += g; duty += parseFloat(r.dataset.duty);
      if (r.dataset.choice === "1") ch += g; else ship += parseFloat(r.dataset.ship);
    });
    if (ch > 0 && ch < TH) ship += CS;
    total = goods + ship + duty;
    [].forEach.call(document.querySelectorAll("section[data-cat]"), function (s) {
      var g = 0, k = 0;
      [].forEach.call(s.querySelectorAll(".row.dec"), function (r) { if (st(r) === "yes") { k++; g += parseFloat(r.dataset.price) * qty(r); } });
      s.querySelector(".sub").textContent = k + " · " + f(g);
    });
    document.getElementById("sb-total").textContent = f(total);
    stEl.textContent = n + " ja · Ware " + f(goods) + " · Versand " + f(ship) + " · Abgabe " + f(duty) + " (gesch.)";
    try { localStorage.setItem(key, JSON.stringify(keep)); } catch (e) {}
  }
  function msg() {
    var items = [], cnt = { yes: 0, maybe: 0, no: 0, "delete": 0 }, dels = [];
    rows.forEach(function (r) {
      var v = st(r), q = qty(r), cid = r.dataset.cart;
      cnt[v]++;
      if (v === "delete") dels.push(r.dataset.title.slice(0, 28));
      if (cid && v === r.dataset.state0 && q === parseInt(r.dataset.qty0, 10) && !r.querySelector(".note-in").value.trim()) return;
      var it = { item_id: r.dataset.item, sku_id: r.dataset.sku, qty: q, state: v, category: r.closest("section").dataset.cat };
      if (cid) { it.cart_id = cid; it.qty0 = parseInt(r.dataset.qty0, 10); }
      var nt = r.querySelector('.note-in').value.trim(); if (nt) it.note = nt;
      items.push(it);
    });
    var head = "Auswahl " + T + ": " + cnt.yes + " ja, " + cnt.no + " nein" + (cnt.maybe ? ", " + cnt.maybe + " vielleicht" : "") +
      (cnt["delete"] ? ", " + cnt["delete"] + " löschen (" + dels.slice(0, 5).join("; ") + (dels.length > 5 ? " ..." : "") + ")" : "") +
      " | Summe ca. " + f(total) + " (geschätzt)";
    return head + "\n```json\n" + JSON.stringify({ action: "select", report: NAME, items: items }) + "\n```";
  }
  var box = document.getElementById("sb-text");
  send.addEventListener("click", function () {
    var t = msg(); box.value = t; box.hidden = false;
    if (t.length > 19500) { stEl.textContent = "Zu lang (" + t.length + " Zeichen): erst einen Teil entscheiden."; return; }
    try { window.parent.postMessage({ type: "claudevoice:send", text: t }, "*"); } catch (e) {}
    try { navigator.clipboard.writeText(t).then(function () { stEl.textContent = "An den Chat übergeben. Dort auf Senden tippen."; },
      function () { stEl.textContent = "An den Chat übergeben. Sonst: Text kopieren."; }); }
    catch (e) { stEl.textContent = "An den Chat übergeben. Sonst: Text kopieren."; }
  });
  document.getElementById("sb-copy").addEventListener("click", function () {
    var t = msg(); box.value = t; box.hidden = false; box.select();
    try { navigator.clipboard.writeText(t); stEl.textContent = "Kopiert."; } catch (e) { stEl.textContent = "Text markiert, bitte kopieren."; }
  });
  window.addEventListener("message", function (e) {
    if (e.data && e.data.type === "claudevoice:sent") { stEl.textContent = "Gesendet. Die Auswahl ist im Chat angekommen."; send.textContent = "Gesendet ✓"; }
  });
  var bulk = document.getElementById("sb-bulk");
  function marked() { return rows.filter(function (r) { return r.classList.contains("marked"); }); }
  function showBulk() { var n = marked().length; bulk.hidden = n === 0; document.getElementById("sb-marked").textContent = n + " markiert"; }
  document.addEventListener("click", function (e) {
    var t = e.target, th = t.closest && t.closest(".thumb");
    if (th) { th.closest(".row").classList.toggle("marked"); showBulk(); return; }
    var all = t.closest && t.closest("button[data-all]");
    if (all) {
      var rs = [].slice.call(all.closest("section").querySelectorAll(".row.dec")), on = rs.every(function (r) { return r.classList.contains("marked"); });
      rs.forEach(function (r) { r.classList.toggle("marked", !on); }); showBulk(); return;
    }
    var k = t.closest && t.closest("button[data-bulk]");
    if (k) {
      marked().forEach(function (r) {
        if (k.dataset.bulk !== "none") { var el = r.querySelector('input[value="' + k.dataset.bulk + '"]'); if (el) el.checked = true; }
        r.classList.remove("marked");
      });
      showBulk(); calc(); return;
    }
    var tx = t.closest && t.closest(".tx");
    if (tx && !(t.closest && t.closest("a, input"))) tx.closest(".row").classList.toggle("open");
  });
  document.addEventListener("input", function () { send.textContent = "Senden"; calc(); });
  document.addEventListener("change", calc);
  calc();
})();
</script>"""


def decide_page(name: str, title: str, subtitle: str, facts: list[dict], notes: list[dict],
                categories: list[dict], stamp: str, choice_threshold: float = 10.0, choice_ship: float = 1.99) -> str:
    """
    A page to decide on, dense enough for a long cart. Per category one row per offer: picture (tap = mark),
    short title, the agent's opinion in one line (fit, rating, verdict; tap the text for the rest), price and three icon
    buttons (Ja / Nein / Löschen; offers also have Vielleicht). A slim bar at the bottom shows the total of the Ja rows,
    acts on marked rows and sends the choice to the chat.
    With cart_id on a card (a line already in the cart): Ja = check for checkout, Nein = uncheck, Löschen = remove it.

    categories: [{heading, cards: [{title (short), img (data: URI), price (EUR per piece), ship (EUR per line, 0 = free),
                  choice (bool), duty (EUR per line), qty, default (yes|maybe|no), verdict (the agent's opinion, one sentence),
                  rating ("4,8★ 208"), fit ("✓ Highland genannt" / "Modell nicht genannt"), tags, text, url,
                  item_id, sku_id, cart_id}]}]
    Raises ValueError when a card has no picture or no item_id/sku_id.
    """
    import json as _json
    missing = [c["title"] for sec in categories for c in sec["cards"] if not c.get("img")]
    if missing:
        raise ValueError("no picture for: " + "; ".join(missing))
    bad = [c["title"] for sec in categories for c in sec["cards"] if not (c.get("item_id") and c.get("sku_id"))]
    if bad:
        raise ValueError("item_id and sku_id needed for: " + "; ".join(bad))
    out = []
    if notes:
        sym = "⚠ " if any(n.get("warn") for n in notes) else ""
        out.append(f'<details class="hints"><summary>{sym}Hinweise ({len(notes)})</summary>'
                   + "".join(f'<div class="note{" warn" if n.get("warn") else ""}"><p>{_e(n["text"])}</p></div>' for n in notes)
                   + "</details>")
    icons = {"yes": (_ICON_YES, "Ja"), "maybe": (_ICON_MAYBE, "Vielleicht"), "no": (_ICON_NO, "Nein"), "delete": (_ICON_DEL, "Löschen")}
    for si, sec in enumerate(categories):
        cart_mode = any(c.get("cart_id") for c in sec["cards"])
        order = ("yes", "maybe", "no", "delete")
        rows = []
        for ci, c in enumerate(sec["cards"]):
            state = c.get("default") if c.get("default") in ("yes", "maybe", "no") else "no"
            if cart_mode and state == "maybe":
                state = "no"
            fit = c.get("fit")
            vd = []
            if fit:
                vd.append(f'<b class="{"ok" if str(fit).startswith("✓") else "q"}">{_e(fit)}</b>')
            if c.get("rating"):
                vd.append(_e(c["rating"]))
            if c.get("verdict"):
                vd.append(_e(c["verdict"]))
            vd_html = f'<div class="vd">{" · ".join(vd)}</div>' if vd else ""
            chips = "".join(f'<span class="chip{" ok" if t.startswith("✓") else ""}">{_e(t)}</span>' for t in c.get("tags") or [])
            link = (f'<a href="{_e(_url(c["url"]))}" target="_blank" rel="noopener">Angebot öffnen</a>' if c.get("url") else "")
            qty = int(c.get("qty") or 1)
            meta_html = '<div class="meta">' + chips + "</div>" if chips else ""
            text_html = "<div>" + _e(c["text"]) + "</div>" if c.get("text") else ""
            more = (f'<div class="more">{meta_html}{text_html}'
                    f'<label class="qrow">Menge <input class="qty" type="number" min="1" max="99" value="{qty}"></label>{link}</div>')
            rid = f"r{si}_{ci}"
            seg = "".join(
                f'<input type="radio" id="{rid}{v[0]}" name="{rid}" value="{v}"{" checked" if v == state else ""}>'
                f'<label class="{v[0]}" for="{rid}{v[0]}" title="{icons[v][1]}" aria-label="{icons[v][1]}">{icons[v][0]}</label>'
                for v in order)
            price = float(c.get("price") or 0)
            ship = float(c.get("ship") or 0)
            duty = float(c.get("duty") or 0)
            cart = f' data-cart="{_e(c["cart_id"])}"' if c.get("cart_id") else ""
            rows.append(
                f'<div class="row dec" data-id="{_e(c.get("cart_id") or "")}{_e(c["item_id"])}-{_e(c["sku_id"])}" data-item="{_e(c["item_id"])}" '
                f'data-sku="{_e(c["sku_id"])}"{cart} data-price="{price:.2f}" data-ship="{ship:.2f}" data-duty="{duty:.2f}" '
                f'data-choice="{1 if c.get("choice") else 0}" data-title="{_e(c["title"])}" data-state0="{state}" data-qty0="{qty}">'
                f'<button type="button" class="thumb" aria-label="markieren">{_img(c["img"])}</button>'
                f'<div class="tx"><span class="need" title="{_e(c["title"])}">{_e(c["title"])}</span>'
                f'<div class="pr">{eur(price)}<small class="qx"></small></div>{vd_html}'
                f'<input class="note-in" type="text" placeholder="Anmerkung" maxlength="300"></div>'
                f'<div class="ctl"><div class="seg" role="radiogroup" aria-label="{_e(c["title"])}">{seg}</div></div>{more}</div>')
        out.append(f'<section data-cat="{_e(sec["heading"])}"><div class="cat-h"><h2>{_e(sec["heading"])}</h2>'
                   f'<span class="sub"></span><button type="button" data-all="1">alle</button></div>'
                   f'<div class="rows">{"".join(rows)}</div></section>')
    out.append('<div class="dbar"><div class="in"><div class="bulk" id="sb-bulk" hidden><span id="sb-marked"></span>'
               f'<button type="button" class="y" data-bulk="yes" aria-label="Ja">{_ICON_YES}</button>'
               f'<button type="button" data-bulk="no" aria-label="Nein">{_ICON_NO}</button>'
               f'<button type="button" class="d" data-bulk="delete" aria-label="Löschen">{_ICON_DEL}</button>'
               '<button type="button" data-bulk="none" aria-label="Markierung aufheben">↺</button></div>'
               '<textarea id="sb-text" readonly hidden aria-label="Nachricht"></textarea>'
               '<div class="main"><div class="dsum"><span class="tot" id="sb-total"></span><span class="st" id="sb-state"></span></div>'
               '<button type="button" id="sb-copy" class="cp" aria-label="Kopieren">⧉</button>'
               '<button type="button" id="sb-send" class="send">Senden</button></div></div></div>')
    js = (_DECIDE_JS.replace("__NAME__", _json.dumps(name).replace("</", "<\\/"))
          .replace("__TH__", repr(float(choice_threshold))).replace("__CS__", repr(float(choice_ship))))
    out.append(js)
    return _page(title, _DECIDE_CSS + "\n" + "\n".join(out))
