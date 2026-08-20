# Inference Lab live telemetry contract

The v0.5 telemetry foundation consumes bounded Prometheus text snapshots from a
lab runtime. It does not embed Prometheus, retain raw scrape bodies or accept
arbitrary metric/label semantics.

## Hard bounds

- response body: maximum 64 KiB;
- canonical series: maximum 64;
- retained samples per run: maximum 3,600;
- offset: finite, non-negative seconds;
- values: finite numeric values only;
- labels: explicit per-metric allowlist, bounded safe values only.

Unknown metrics are discarded before value/label parsing. This prevents an
untrusted, high-cardinality or credential-bearing metric from becoming evidence
or an error message. An allowlisted malformed metric fails the entire snapshot;
it is never partially accepted.

## Canonical sample

Each immutable sample binds:

- monotonic run offset;
- canonical metric ID and original allowlisted source name;
- finite value, unit and direction;
- procedure version;
- sorted allowlisted label pairs;
- fixed status `ok`.

Unknown labels are dropped. Allowed label values reject path-like and
credential-like text. NaN and infinities are invalid; missing telemetry is not
converted to zero.

## Buffer

`TelemetryBuffer` is thread-safe and atomically appends immutable sample tuples.
It evicts the oldest samples at the configured bound. A candidate append that
would exceed the series limit is rejected without partially changing the
buffer.

Scrape failures are stored separately with only fixed categories:

- `timeout`
- `invalid`
- `unavailable`

Raw HTTP errors, endpoint paths and response content are not retained.

## Product boundary

The live chart layer consumes snapshots; it cannot change verdict semantics.
Only normalized samples sealed into a verified lab-run artifact may become
decision evidence. Long-term production retention remains an external
Prometheus/OpenTelemetry concern.
