r"""Ask a real browser what the light scheme's contrast actually is.

Light mode went unexamined for over a week, and the reason recorded at the time was that a
faithful render was thought impossible. It was not; the harness was wrong in a specific way,
and this script is that fix made repeatable so the gap cannot re-open quietly. It did once
already: the palette changed, nobody could check it, and nine muted classes sat below WCAG AA
until someone finally computed the light figures by hand.

    cd frontend && python3 scripts/contrast-audit/run.py

**It cannot be a test.** It needs Chrome, and `make check` must not — so this is a tool you
run when the palette changes, and a record you read. Same standing as `scripts/maps-probe/`.

## Rendering light faithfully needs *both* halves

`:root` declares `color-scheme: light dark`, and each half of the fix alone produces a state
that cannot occur in reality:

- Force `color-scheme: light` alone and the user agent paints its own surfaces light while
  every `@media (prefers-color-scheme: dark)` rule still matches — light chrome over a dark
  palette. A tool-failed entry read as amber-on-cream that way and was nearly filed as a
  contrast defect that does not exist.
- Strip the dark block alone and the UA still paints selects, scrollbars and `Canvas` from the
  OS preference — the failure the original attempt hit, and the reason it was abandoned.

Removing the block is honest rather than a cheat: a media block that does not match contributes
nothing to the cascade, so a sheet without it is exactly what a light-preference browser
computes.

## The bar is per element, and every one of these was learned the hard way

The first run of this audit reported nine failures and **all nine were the audit's fault**. An
audit that cries wolf gets ignored; one quietly tuned down to stop crying wolf is worse than
none. So the thresholds are encoded per element, with the reason attached:

- **A disabled control has no contrast requirement at all.** WCAG exempts an inactive component
  outright. Three of the nine were `<option>`s inside a `<select disabled>`, which a closed
  select never paints in the first place.
- **A labelled icon-only control is a UI component, not text**: SC 1.4.11 at 3:1, not SC 1.4.3
  at 4.5:1. Six of the nine were `×` glyphs on `.points__remove` and `.places__ignore`, both of
  which carry a real `aria-label`. That is exactly what `--ink-faint` documents itself as being
  for, at exactly the figure it records (3.95:1 light), so it is a decision already made and
  written down — not something for this script to re-litigate.
- **Large text is 3:1** — 24 px, or 18.66 px bold.
- Everything else is text at 4.5:1.

A failure here is a real one. If this ever reports something legitimate, the answer is to widen
the *reason*, in this docstring, where the next person can read it.

## What is checked, and the number that catches a gap

Every class the dark blocks redefine must appear in `fixture.html`, and this says so when one
does not. That is what makes "all of light mode" a claim rather than a hope, and it has already
caught two different versions of the same mistake:

- A dump of the running app reached **31 of 55** classes, because it never gets to the landing
  screen, the Places detail pane or the error states.
- The extraction itself read **55 of 88**, because `index.css` has **two** dark blocks and the
  first attempt used `str.index` and stopped at the first one. Every count and every claim built
  on that number was wrong, including "none of them is a map overlay" — four are. Hence
  `strip_dark` loops rather than finding once, and hence this paragraph.

## What counts as a scheme-dependent class

**A class name appearing in *selector position* inside a dark block** — in the text before a
`{`, and nothing else. Compound parts count (`.poi-pane__kind .poi--mark` is two), and so does
a modifier owning no declaration of its own but appearing in a selector: both change what a
rider sees.

**What it excludes, and it is the one that matters:** class-like tokens in *declaration* text.
The obvious way to count is to scan the whole block for `\.[a-zA-Z0-9_-]+`, and that answers
**108** rather than 88. Every one of the twenty extras is the fractional part of a number —
`rgb(0 0 0 / 0.5)` gives `.5`, `0.12` gives `.12`, and so on through `.05 .06 .07 .08 .1 .12`
`.14 .16 .2 .22 .25 .28 .4 .45 .5 .6 .7 .8 .85 .9`. Not one is a class. Checked the other
direction too: the selector-position set is a strict *subset* of the naive one, so being
stricter loses nothing real.

Written down because the number is a tripwire, and a tripwire whose figure nobody else can
reproduce gets silenced rather than investigated. Both figures print on every run for that
reason, and `redefined_classes` exits loudly if it ever extracts a purely numeric name — which
is what reading declarations by mistake looks like.

**88 scheme-dependent classes across two blocks, measured 2026-09-04.** If the stylesheet grows
an eighty-ninth and this still says 88, the extraction has broken rather than the stylesheet
having stayed still.

**Four of the 88 are map-layer classes** — `map-pane`, `map-canvas__overlay`, `map-locate` and
`pin` — and they are *not* out of reach. A pin is an advanced marker's DOM content and the
overlay is a plain div, so all of it renders without the Maps API. The fixture puts them over a
striped ground rather than a flat pane colour, because the map is satellite imagery and a
figure computed against `#e8e6e1` describes a surface that exists only while the map is loading.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).parent
FRONTEND = HERE.parent.parent
CSS = FRONTEND / "src" / "index.css"
CHROME = "google-chrome-stable"
DARK_AT = "@media (prefers-color-scheme: dark) {"

EXPECTED_CLASSES = 88
"""Scheme-dependent classes across both dark blocks, as of 2026-09-04.

A mismatch is worth a look either way. This started life as 55 — the first block only — which
is exactly the failure the count is here to catch.
"""


def strip_dark(css: str) -> tuple[str, str]:
    """The stylesheet as a light-preference browser computes it, plus the block removed."""
    kept: list[str] = []
    blocks: list[str] = []
    i = 0
    while True:
        at = css.find(DARK_AT, i)
        if at == -1:
            kept.append(css[i:])
            return "".join(kept), "".join(blocks)
        kept.append(css[i:at])
        depth, j = 1, at + len(DARK_AT)
        while depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        blocks.append(css[at:j])
        i = j


def redefined_classes(block: str) -> set[str]:
    """Class names in selector position — the text before a `{`. See the docstring for why."""
    rules = re.findall(r"(?:^|\})\s*((?:[^{}/]|/\*.*?\*/)+?)\{", block, re.S)
    names: set[str] = set()
    for rule in rules:
        names.update(re.findall(r"\.([a-zA-Z0-9_-]+)", re.sub(r"/\*.*?\*/", "", rule, flags=re.S)))
    # A purely numeric name means the extraction has started reading declarations: the `0.5`
    # in `rgb(0 0 0 / 0.5)` looks exactly like a class to a `.[\w-]+` pattern. Loud, rather
    # than silently inflating the count by twenty — which is precisely the difference between
    # this method and the obvious one.
    numeric = sorted(n for n in names if n.isdigit())
    if numeric:
        sys.exit(f"extraction is reading declarations, not selectors: {', '.join(numeric)}")
    return names


def class_like_tokens(block: str) -> set[str]:
    """The obvious method, kept so its answer can be reconciled rather than argued about."""
    return set(re.findall(r"\.([a-zA-Z0-9_-]+)", block))


AUDIT = """
<script>
function parse(c) {
  const m = c.match(/rgba?\\(([^)]+)\\)/); if (!m) return null
  const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number)
  return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }
}
const over = (fg, bg) => ({
  r: fg.r * fg.a + bg.r * (1 - fg.a),
  g: fg.g * fg.a + bg.g * (1 - fg.a),
  b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1,
})
function lum(c) {
  const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b)
}
function ratio(a, b) {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p)
  return (x + 0.05) / (y + 0.05)
}
// Composited down the ancestor chain, not read off the element. A translucent colour over a
// translucent panel is the ordinary case here, and its own value says nothing about what a
// rider sees.
function backdrop(el) {
  let acc = { r: 255, g: 255, b: 255, a: 1 }
  const chain = []
  for (let n = el; n && n !== document.documentElement; n = n.parentElement) chain.push(n)
  for (const n of chain.reverse()) {
    const bg = parse(getComputedStyle(n).backgroundColor)
    if (bg && bg.a > 0) acc = over(bg, acc)
  }
  return acc
}
window.addEventListener('load', () => {
  const rows = []
  // `style` and `script` hold text nobody sees; measuring them reports a stylesheet as a
  // 21:1 pass and buries the real rows under it.
  const INVISIBLE = new Set(['STYLE', 'SCRIPT', 'TITLE', 'HEAD', 'META', 'LINK'])
  for (const el of document.querySelectorAll('*')) {
    if (INVISIBLE.has(el.tagName)) continue
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue
    const cs = getComputedStyle(el)
    const fg = parse(cs.color); if (!fg) continue
    const bg = backdrop(el)
    const r = ratio(fg.a < 1 ? over(fg, bg) : fg, bg)
    const px = parseFloat(cs.fontSize)
    const large = px >= 24 || (parseInt(cs.fontWeight, 10) >= 700 && px >= 18.66)
    const inactive = el.closest('[disabled],:disabled') !== null
    const iconOnly = el.tagName === 'BUTTON' && el.hasAttribute('aria-label')
                     && el.textContent.trim().length <= 2
    const need = inactive ? 0 : (iconOnly || large) ? 3.0 : 4.5
    const why = inactive ? 'inactive component, exempt'
              : iconOnly ? 'labelled icon-only control, 1.4.11'
              : large ? 'large text' : 'text, 1.4.3'
    rows.push({
      cls: String(el.className || el.tagName), text: el.textContent.trim().slice(0, 40),
      color: cs.color, ratio: +r.toFixed(2), need, why, fails: r < need,
    })
  }
  document.body.insertAdjacentHTML('afterbegin',
    '<pre id=audit>' + JSON.stringify(rows) + '</pre>')
})
</script>
"""


def main() -> None:
    css = CSS.read_text()
    if DARK_AT not in css:
        sys.exit("no dark block in index.css — the stylesheet has changed shape, not just colour")
    light, dark_block = strip_dark(css)
    assert "prefers-color-scheme" not in light, "a dark block survived the strip"

    classes = redefined_classes(dark_block)
    fixture = (HERE / "fixture.html").read_text()
    missing = sorted(c for c in classes if not re.search(r'class="[^"]*\b' + re.escape(c) + r'\b', fixture))

    naive = class_like_tokens(dark_block)
    decimals = sorted(naive - classes)
    print(f"scheme-dependent classes: {len(classes)} (recorded: {EXPECTED_CLASSES})")
    # Printed every run so the obvious method's answer reconciles itself on sight, rather
    # than someone arriving at a bigger number and having to ask which of us is wrong.
    shown = ", ".join("." + d for d in decimals[:6])
    print(f"  a naive scan of the whole block finds {len(naive)}; the {len(decimals)} extras"
          f" are decimals inside declarations rather than classes: {shown}…")
    if len(classes) != EXPECTED_CLASSES:
        print("  ! the count moved. Update EXPECTED_CLASSES and the docstring, deliberately.")
    if missing:
        print(f"  ! not exercised by fixture.html ({len(missing)}): {', '.join(missing)}")
        print("    Add them. A class the fixture never renders is a class nobody has looked at.")

    # Not a warning that they exist — four do, and the fixture renders them. A warning that a
    # *new* one has appeared, since a map-layer class needs the striped ground rather than the
    # page background to be measured against anything real.
    overlays = sorted(c for c in classes if re.match(r"(map|pin|fan|cluster|locate|marker|poi-cluster)", c))
    unplaced = [c for c in overlays if c in missing]
    print(f"map-layer classes: {len(overlays)} ({', '.join(overlays)})")
    if unplaced:
        print(f"  ! not over the striped ground: {', '.join(unplaced)}")
        print("    Put them inside `.map-pane` in the fixture. Measured against the page")
        print("    background instead, the figure describes a surface a rider never sees.")

    work = pathlib.Path("/tmp/motorooter-contrast-audit")
    work.mkdir(exist_ok=True)
    page = work / "light.html"
    page.write_text(
        f'<!doctype html><meta charset="utf-8"><style>{light}</style>'
        # Both halves. Either alone renders a state that cannot occur — see the docstring.
        "<style>:root{color-scheme:light !important}</style>"
        # Viewing only, and it changes no colour. `.landing` is a fixed full-screen overlay in
        # the real app, which is right there and useless here: it covers every other surface in
        # the screenshot. Laid out in flow so one image shows all of them.
        "<style>.landing{position:static;min-height:auto;padding:1.5rem}"
        ".app{height:auto}.poi-pane,.chat-pane{height:auto}"
        ".map-pane{min-height:180px;position:relative}"
        "#audit{display:none}</style>" + AUDIT + fixture
    )

    dom = subprocess.run(  # noqa: S603
        [CHROME, "--headless", "--disable-gpu", "--virtual-time-budget=3000",
         "--window-size=1400,2200", "--dump-dom", str(page)],
        capture_output=True, text=True, check=False,
    ).stdout
    found = re.search(r'<pre id="audit">(.*?)</pre>', dom, re.S)
    if not found:
        sys.exit("the page never reported — is google-chrome-stable installed?")
    rows = json.loads(found.group(1))

    failures = [r for r in rows if r["fails"]]
    print(f"\nelements measured: {len(rows)}")
    print(f"contrast failures:  {len(failures)}")
    for r in failures:
        print(f"  {r['ratio']:>5} (needs {r['need']}, {r['why']})  {r['cls'][:38]:<38} {r['text']!r}")

    tight = sorted((r for r in rows if not r["fails"] and r["ratio"] < r["need"] + 1.0),
                   key=lambda r: r["ratio"])
    if tight:
        print("\nclosest to the bar, in case a palette change is about to cross it:")
        for r in tight[:5]:
            print(f"  {r['ratio']:>5} (needs {r['need']}, {r['why']})  {r['cls'][:38]:<38} {r['text']!r}")

    subprocess.run(  # noqa: S603
        [CHROME, "--headless", "--disable-gpu", f"--screenshot={work / 'light.png'}",
         "--window-size=1400,2200", "--hide-scrollbars", str(page)],
        capture_output=True, check=False,
    )
    print(f"\nrendered: {work / 'light.png'} — look at it as well as reading the numbers.")


if __name__ == "__main__":
    main()
