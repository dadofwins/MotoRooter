# Upstream report: ORS returns elevation `0` for failed lookups on `cycling-mountain`

**Status: written, not filed.** Filing this on openrouteservice's tracker is an outward-facing
action and needs whoever owns the account to do it. The text below is ready to paste.

Written while the measurement was fresh, which is the only time this is cheap to write.

---

## Summary

On the hosted API, `cycling-mountain` routes requested with `elevation: true` contain geometry
positions whose elevation is exactly `0.0` where the surrounding terrain is 500–1600 m. They
appear in adjacent pairs between two points with plausible elevations, and the reported
`properties.ascent` includes them — so a single failed lookup adds roughly twice the local
elevation to the route's total climb.

`driving-car` over the same coordinates returns no such points.

## Why it matters

`properties.ascent` is a cumulative sum of positive deltas, so each excursion to `0` and back
is counted twice. Twelve such points in a 2,763-point route inflated the reported ascent from
3,605 m to 6,729 m — an 87% overstatement, on a figure a user reads directly.

The value is self-consistent with the geometry, so a client that recomputes ascent from the
returned elevations gets the same wrong answer. There is no way to detect the problem from the
summary alone.

## Reproduction

Two corridors in Washington State, USA. `POST /v2/directions/{profile}/geojson` with
`{"coordinates": [...], "elevation": true}`.

### Chinook Pass

```json
{"coordinates": [[-121.5340, 46.9720], [-121.5165, 46.8722]], "elevation": true}
```

| profile | positions | positions with elevation `0` | reported `ascent` | ascent excluding them |
|---|---|---|---|---|
| `cycling-mountain` | 311 | 4 | 3725.3 | 1495 |
| `driving-car` | 269 | 0 | 941.1 | 941 |

The clearest instance, positions 287–290:

```
[-121.518228, 46.869478, 1621.0]
[-121.518228, 46.869489,    0.0]
[-121.518236, 46.869532,    0.0]
[-121.518238, 46.869539, 1621.0]
```

The first two positions are about 1.2 m apart horizontally. A 1,621 m drop over 1.2 m is not
terrain.

The reported figure of 3725.3 m over 16.2 km is 230 m/km — a sustained 23% average gradient,
over a paved highway pass that `driving-car` reports at 52 m/km.

### Ellensburg to Cashmere

```json
{"coordinates": [[-120.9560, 47.1946], [-120.4630, 47.5210]], "elevation": true}
```

| profile | positions | positions with elevation `0` | reported `ascent` | ascent excluding them |
|---|---|---|---|---|
| `cycling-mountain` | 2763 | 12 | 6729.4 | 3605 |
| `driving-car` | 977 | 0 | 1501.3 | 1502 |

Positions 193–196:

```
[-120.782445, 47.201201, 500.9]
[-120.782048, 47.201291,   0.0]
[-120.781370, 47.201386,   0.0]
[-120.780529, 47.201546, 425.8]
```

A published reference track for this corridor gives 3,188 m by the same naive summation. The
corrected figure of 3,605 m is consistent with that once the denser geometry is accounted for;
the reported 6,729 m is not.

## What we ruled out

- **Not an accumulation bug in ORS.** `properties.ascent` equals a naive sum of positive
  deltas over the returned elevations, to the metre, on every route measured. The arithmetic
  is right; the input is wrong.
- **Not geometry density.** `cycling-mountain` returns denser geometry, which raises a naive
  sum slightly — measured elsewhere at roughly `spacing^-0.08`, predicting ~370 m of a
  3,100 m discrepancy.
- **Not elevation noise.** Filtering out climbs below 20 m changes the total by under 3%, so
  the excess is not small sign-alternating error.
- **Not a genuinely steeper route.** On Chinook Pass both profiles follow the same 16–18 km
  pass road and differ fourfold in reported climb.

## Suggested behaviour

Any of these would be an improvement, in rough order of preference:

1. Omit the third ordinate for positions where the elevation lookup fails, so a client can see
   the gap rather than inferring it.
2. Interpolate between neighbouring valid elevations.
3. Exclude failed lookups from the `ascent` and `descent` sums.

The present behaviour is the difficult one for a consumer, because `0` is a legal elevation and
nothing distinguishes a failed lookup from a genuine sea-level point except implausibility
relative to its neighbours.

## What we do meanwhile

Compute ascent ourselves from the returned elevations, discarding an exact `0` when the route's
median elevation makes it implausible. Recorded in
`backend/src/motorooter/routing/providers/ors.py`.
