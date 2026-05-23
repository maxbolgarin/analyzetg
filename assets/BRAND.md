# unread — brand assets

The mark is square brackets with a dot: `[●]`. The brackets read as a citation
reference (`[#1586]`) and as code, the dot is the universal "unread" signal.
One accent color, everything else monochrome.

## Accent color — the only variable

```
#3b82f6   rgb(59, 130, 246)   notification blue   ← default
```

This is the single point of change. To rebrand to terminal green, find-replace
`#3b82f6` → `#22c55e` across the `.svg` files and update `ACCENT` in
`terminal/banner.py` to `(34, 197, 94)`, then re-render. Nothing else carries color.

## What's in the pack

```
icon/        self-contained dark tile — use where the mark sits on unknown backgrounds
  icon.svg                 master (scalable)
  favicon.ico              16/32/48 multi-size, drop in repo root
  favicon-16/32/48.png
  apple-touch-icon.png     180×180
  icon-512.png             PWA / store / large

mark/        the bare bracket-dot, transparent, for placing on your own surfaces
  mark-mono.svg            brackets = currentColor (adapts to text color), dot = accent
  mark-onlight.svg/.png    baked dark brackets, for light backgrounds
  mark-ondark.svg/.png     baked light brackets, for dark backgrounds

wordmark/    horizontal lockup: mark + "unread"
  wordmark.svg             currentColor, use inline in HTML/README
  wordmark-onlight.*       baked, for light backgrounds
  wordmark-ondark.*        baked, for dark backgrounds

social/      1280×640 — GitHub social preview + OpenGraph/Twitter card
  social.svg
  social-1280x640.png

terminal/    banner printed by the CLI
  banner.py                drop-in module, NO_COLOR-aware
  banner.txt               plain-text reference
```

## README hero (auto light/dark)

```html
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/wordmark/wordmark-ondark.png">
    <img alt="unread" src="assets/wordmark/wordmark-onlight.png" width="320">
  </picture>
</p>
<p align="center"><em>Read your unread. Without reading it.</em></p>
```

## Favicon (docs site / landing)

```html
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
```

## GitHub social preview

Repo → Settings → General → Social preview → upload `social-1280x640.png`.
Same file works as the OpenGraph image (`og:image`) for the landing page.

## Terminal banner

```python
from unread.banner import print_banner
print_banner(version="0.1.0")
```

Prints `[●] unread 0.1.0` with the dot in accent blue, falling back to `[*]`
under NO_COLOR or when piped. Wire it into the no-args / `--version` path.

## Clearspace & don'ts

- Keep padding around the mark equal to one bracket's width. Don't crowd it.
- Don't recolor the brackets per-context — they're mono (black, white, or
  currentColor). Only the dot carries the accent.
- Don't add a second accent color, gradient, or shadow.
- Don't stretch — scale uniformly.
- Minimum legible mark size ≈ 16px (verified on `favicon-16`).
