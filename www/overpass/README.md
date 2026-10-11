# overpass — an Overpass API over a GeoDesk GOL

A prototype. It speaks the Overpass API (`/api/interpreter`, Overpass QL in,
OSM JSON / XML / CSV out) but has no Overpass database behind it: the script
is parsed and run in Python against a [GeoDesk](https://www.geodesk.com) GOL
built from the region PBFs the stack already keeps current - in the stack,
every region of `datasvc/config.yaml`, whichever they are, merged into one file.

```
datasvc-job (02:00):  Geofabrik diffs ──► region PBFs
                      then, if they changed:
                      osmium merge | osmium time-filter | gol build -w ──► data/gol/regions.gol
                                                                                │ (mmap)
POST /api/interpreter ──► parser ──► evaluator ──► output ──► JSON / XML / CSV
                          (QL→AST)   (sets of elements; GOQL + GeoDesk
                                      spatial filters + Python)
```

| File | Purpose |
| --- | --- |
| `syntax.py` | the parsed form of a script: settings, statements, filters, `if:` expressions |
| `parser.py` | Overpass QL → `syntax`, with Overpass's own `line N: parse/static error` messages |
| `evaluator.py` | runs the statements over named sets; where GOQL ends and Python takes over |
| `output.py` | Overpass JSON, OSM XML, CSV, and the HTML error page |
| `library.py` | opens the GOL lazily, reopens it once it is replaced, reads `timestamp_osm_base` |
| `server.py` | Flask: `/api/interpreter`, `/api/status`, `/api/kill_my_queries`, `/health` |
| `../otmlib/gol.py` | PBFs → one GOL, atomically, dated by the newest edit in the data |
| `../datasvc/job.py` | `build_gol`: the step after the nightly sync that runs it |

## Running it

Locally, with GeoDesk's [GOL tool](https://github.com/clarisma/geodesk-gol/releases)
and osmium on `PATH` (or in `OTM_GOL_BIN` / `OTM_OSMIUM_BIN`):

```bash
cd www
python3 -m venv overpass/.venv
overpass/.venv/bin/pip install -r overpass/requirements.txt
overpass/.venv/bin/python -m otmlib.gol mapsvc/data/gol/armenia.gol \
    mapsvc/data/geofabrik-cache/armenia-latest.osm.pbf
OTM_OVERPASS_GOL=mapsvc/data/gol/armenia.gol overpass/.venv/bin/python -m overpass.server
curl -d '[out:json];node[amenity=pub](40.17,44.50,40.19,44.52);out 3;' \
    http://127.0.0.1:8080/api/interpreter
```

In the stack (`www/docker-compose.yml`) the service is `overpass`, behind
nginx at `/api/`, answering from `data/gol/regions.gol` (mounted read-only). It
answers 503 until that file exists, and picks up a rebuilt one on the next
query, without a restart.

The file is datasvc's: `gol: true` in `www/datasvc/config.yaml` makes the
nightly job, once its sync is done, merge every configured region into
`data/gol/regions.gol` (`datasvc.job.build_gol`, `otmlib.gol`). It rebuilds
only when the extracts are not the ones the GOL was built from - their names,
sizes and mtimes are recorded next to it - so a night with no diffs costs a
few `stat()` calls, and a night whose build failed is made good by the next
one. The stock config (the Russian federal districts, ~4 GB of PBF) takes
about five minutes on a laptop. To build it now:

```bash
docker compose run --rm datasvc-job python -m datasvc
```

(the sync part is a no-op when the extracts are current).

One GOL for all the regions, not one per region, so a way or a relation
crossing a district border is one element, as in Overpass, not two clipped
halves. The extracts overlap along those borders; `osmium merge` drops an
object repeated across them only when it is the same version in each, so the
merge goes through `osmium time-filter`, which keeps the newest version: a
region that missed a night's diffs still yields one copy of every border
object.

`timestamp_osm_base` is the newest edit in the data, taken from the stalest of
the merged extracts. osmium reads it out of the objects themselves: the PBF
headers and Geofabrik's state.txt next to them stop being true once datasvc
has applied a diff.

`gol` itself is compiled from source in the datasvc image (a build stage of
`www/datasvc/Dockerfile`), native on amd64 and arm64. The released binary is
x86_64 only, wants glibc 2.38 (newer than datasvc's bookworm), and under qemu
on Apple Silicon it fails outright - it maps its output in one multi-gigabyte
`mmap(MAP_SHARED)` that qemu-user refuses with ENOMEM. gol's build fetches
libgeodesk from its main branch and cpp-httplib untagged; the Dockerfile pins
both.

The GOL is merged and built in `OTM_GOL_WORK_DIR` (`/work`, the `gol_work`
volume, in datasvc-job) and copied into `data/gol/` when done. On macOS the
data dir is a host directory Colima mounts into its VM over sshfs, and gol
cannot build there - it stops with "Operation not supported"; the volume lives
on the VM's own disk. It needs about seven times the size of the configured
PBFs while building - gol's sort files are far bigger than the GOL it ends
with; 28 GB at the peak for the stock config's 4.2 GB - and it is freed when
the build ends. `otmlib.gol` checks for 8x before it starts: a full disk does
not reach gol as an error but as SIGBUS (it maps its files into memory), and
that is now reported as a full disk instead of a bare signal.

The `overpass` image itself is `linux/amd64`: geodesk's Python wheels exist
for x86_64 alone. On Apple Silicon it runs under qemu, which is fine for
reading a GOL.

| Variable | Default | What it does |
| --- | --- | --- |
| `OTM_OVERPASS_GOL` | `/app/data/gol/regions.gol` | the GOL to answer from |
| `OTM_OVERPASS_MAX_TIMEOUT` | `600` | the ceiling on a script's `[timeout:]` |
| `OTM_OVERPASS_MAX_ELEMENTS` | `2000000` | the most elements one set may hold before the query stops |
| `OTM_OVERPASS_WORKERS` | `2` | gunicorn workers, one query each at a time |
| `OTM_GOL_BIN` | `gol` | the GOL tool, for `otmlib.gol` and the tests |
| `OTM_OSMIUM_BIN` | `osmium` | osmium, for `otmlib.gol` |

## What runs

* settings: `[out:json|xml|csv(...)]`, `[timeout:]`, `[maxsize:]` (accepted,
  ignored — the limit is `OTM_OVERPASS_MAX_ELEMENTS`), `[bbox:]`;
* queries: `node`, `way`, `rel`, `nwr`, `nw`, `nr`, `wr`, `area`, with input
  sets (`node.a.b`) and `->.x`;
* tag filters: `[k]`, `[!k]`, `[k=v]`, `[k!=v]`, `[k~re]`, `[k!~re]`,
  `[~kre~vre]`, `,i`;
* `(s,w,n,e)`, `(bbox)`, `(id:…)`, `(around:r,lat,lon[,lat,lon…])`,
  `(around.set:r)`, `(poly:"…")`, `(area[.set])`, `(area:id)`, `(pivot)`,
  `(w)`, `(r[:role])`, `(bn)`, `(bw)`, `(br)` with roles, `(if: …)`;
* unions, differences, `.a->.b`, `>`, `>>`, `<`, `<<`, `foreach`,
  `map_to_area`, `is_in`;
* `out` with `ids`/`skel`/`body`/`tags`/`meta`/`count`, `geom`/`bb`/`center`,
  and a limit;
* `if:` expressions: `t[]`, `is_tag`, `id`, `type`, `count_tags`,
  `count_members`, `is_closed`, `length`, `lat`, `lon`, `number`,
  `is_number`, `suffix`, `abs`, `min`, `max`, the aggregate `count(nodes|ways|relations|areas|nwr|…)` over the set `_`, the usual operators and `?:`.

Refused as a `static error` rather than answered wrongly: history and metadata
(`[date:]`, `[diff:]`, `[adiff:]`, `newer`, `changed`, `user`, `uid`), and the
statements not written yet (`make`, `convert`, `complete`, `retro`,
`compare`, `timeline`, `if`/`for` blocks). The XML query language is refused
too.

## Where the answers differ from Overpass

* **Untagged nodes are not features in a GOL.** They come back through their
  ways (`>`, `node(w)`, `out geom`), but `node(area)`, `node(bbox)`,
  `node(around…)` and `node(id)` see tagged nodes only.
* **No metadata.** A GOL has no version, timestamp, changeset or user:
  `out meta` prints what `out body` does, and the CSV `::user`-like fields are
  empty.
* **Areas are GeoDesk's.** Every closed way with area tags and every
  multipolygon/boundary relation is an area, ids `2400000000 + way` and
  `3600000000 + relation` as in Overpass. Overpass's own area rules (a `name`,
  …) are not applied, so `area[...]` can find more.
* **`(bbox)` for nodes is exact; for ways it goes by the line**, as in
  Overpass. `out geom(s,w,n,e)` is accepted and not clipped.
* **The data is a snapshot**: `timestamp_osm_base` is the newest edit in the
  data the GOL was built from (the oldest such across merged regions).

## GeoDesk specifics the code relies on

Found by probing geodesk 2.3.0 against a real GOL; `evaluator.py` has the
details.

* GOQL is typed: number-shaped values are stored as numbers, so
  `[admin_level="4"]` and `[k~"^4$"]` miss `admin_level=4`, and quoted values
  are globs (`"pu*"`). GOQL therefore only narrows the candidates, and every
  tag filter is checked again in Python on the tag's text, the Overpass way.
* `w` in GOQL excludes ways that are areas, so `way` is queried as `wa`.
* **A query iterator abandoned half-way crashes the process** (segfault).
  Every GeoDesk collection is read whole with `list()`.
* A GOL must be built with `-w` (waynode IDs), or every untagged way node has
  id 0; `library.py` refuses such a file.
* The query timeout is checked between elements. A single GeoDesk call cannot
  be interrupted, so gunicorn's `--timeout` is the hard stop.

## Tests

```bash
cd www
OTM_GOL_BIN=/path/to/gol overpass/.venv/bin/python -m pytest overpass/tests
```

`tests/fixture.osm` is a small hand-made town whose answers are worked out in
the tests. It goes through osmium and `otmlib.gol`; without osmium or `gol` the
tests that need a GOL skip and only the parser tests run.
