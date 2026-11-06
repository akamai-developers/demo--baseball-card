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

## linode/

Three answers of the Linode API, recorded for `tests/unit/test_gpu_check.py`,
which never calls the live API. Recorded on 2026-11-06 at 22:28 UTC with no
token: `GET /v4/linode/types`, `GET /v4/regions` and all five pages of
`GET /v4/regions/availability`, at `page_size=500`.

Trimmed to what the check reads. `types.json` keeps the nine RTX 4000 Ada
plans and two others, `g1-gpu-rtx6000-1` and `g6-standard-2`, so a test can
show they are left out. `regions.json` keeps each region's `id`, `label` and
`capabilities`. `availability.json` keeps the rows for those eleven plans.
Each file is one page, and the test's stand-in API serves it in pages again.

At that time six regions could sell every one- and two-card RTX 4000 Ada
plan: `de-fra-2`, `fr-par`, `in-bom-2`, `jp-osa`, `sg-sin-2` and `us-ord`.
Earlier the same day none could. Public data, no licence attached.

## Stadium backgrounds

Not checked in. `card.procedural.stadium()` draws them from a seed, so the tests
generate what they need. Nothing is downloaded.

## Fonts

The fonts are not fixtures. They ship with the card in `card/fonts/`, under the
SIL Open Font License 1.1, with a copy of the licence next to each one.
