# Test fixtures

Every binary in this directory is redistributable. If you add one, add its row
here first.

## portrait.jpg

The stand-in for "a headshot taken against a plain wall", which is the input this
demo asks for.

| | |
|---|---|
| Title | Fashionable College Portrait - Quinn |
| Author | `exit78` on Flickr |
| Source | https://www.flickr.com/photos/28826830@N00/54780937283 |
| Licence | CC0 1.0 Universal, https://creativecommons.org/publicdomain/zero/1.0/ |
| Checked | 2026-09-21, Flickr license id 9 |
| Changes | Resized to 1024 px on the long side, re-encoded, EXIF dropped |

## cutout.png

`portrait.jpg` with the wall removed, RGBA, produced on this machine by
`card.procedural.plain_wall_cutout`. It is checked in so the compositor and
renderer tests do not have to re-run the matte, and so they compare against the
same pixels every time.

Reproduce it with:

```
uv run python -c "
from PIL import Image
from card.procedural import plain_wall_cutout
plain_wall_cutout(Image.open('tests/fixtures/portrait.jpg')).save('tests/fixtures/cutout.png')"
```

It inherits the CC0 dedication of `portrait.jpg`.

## Stadium backgrounds

Not checked in. `card.procedural.stadium()` draws them from a seed, so the tests
generate what they need. Nothing is downloaded.

## Fonts

The fonts are not fixtures. They ship with the card in `card/fonts/`, under the
SIL Open Font License 1.1, with a copy of the licence next to each one.
