# CSV schemas

`<p>` is a profile: `p100`, `p150`. Quasar uses its own columns.

All CSVs are UTF-8 with a BOM (`utf-8-sig`) so Excel and Numbers show the status emoji;
write them with `opa_schema.write_csv` or open files with `encoding="utf-8-sig"`.

## op_table.csv (static, one row per unique device-op variant)

`id` (1..N in row order), `stage`, `ttnn_api`, `op_code` (Tracy OP CODE, e.g.
`MatmulDeviceOperation`; empty only when unknown), `device_op`, `program_factory`, `call_site`,
`evidence`, `confidence` (`verified` | `unverified`), `grid_dependency` (the hardcoded grid, shard
spec or program config this op depends on, with its value, or `automatic` when it is derived from
the device grid), then per Blackhole profile `<p>:shapes`, `<p>:launches`, `<p>:status`, and for
Quasar `quasar:as_written`, `quasar:port`, `quasar:evidence`, `quasar:bf16_projection`,
`quasar:bf16_projection_evidence`. The Quasar columns are defined in `quasar-status.md`.

Status values: `✅`, `⚠️`, `❌`, `n/a`, `variant not used by port` (not in
`quasar:bf16_projection`).

## call_trace.csv (static, execution order)

`id`, `profile`, `stage`, `ops` (device ops in order), `repeats`, `launches_per_repeat`, `notes`.
For each Blackhole profile, sum of `repeats * launches_per_repeat` must equal the sum of
`<p>:launches` in `op_table.csv`.

## quasar_blockers.csv, host_ops.csv (static)

Free-form columns: `id`, `applies_to`, `blocker`, `affected_ids`, `evidence`, `required_change`;
`id`, `host_work`, `when`, `counts_as_host_op` (`yes` when the work runs on the host once per
inference inside the measured forward, such as torch calls in model code, so it must be added by
hand to the host op time of `host_fallback.csv`, which Tracy does not see; `no` for test setup,
weight preparation and reference computation; each with the reason).

## measured_ops.csv

`id`, `op_code`, `op_type`, `attributes`, `inputs`, `outputs`, `core_count`, `device_kernel_ns`,
`host_ns`.

## host_fallback.csv

`scope`, `window`, `device_ops`, `host_ops`, `device_op_time_ms`, `host_op_time_ms`,
`total_time_ms`, `host_pct_of_total`. Measured values only; no threshold.

## footprint.csv

`op_code`, `launches`, `max_core_count`, `peak_dram_mb` (`not measured` unless supplied).

## diff.csv

`category`, `op_code`, `static_launches`, `measured_launches`, `static_ids`, `detail`.

## changes_<table>.csv

`change` (`added` | `removed` | `changed`), `key`, `column`, `old`, `new`.
