# setup-box: the local site file

Site values live in `~/.tt-project/boxes.json` on the project box (override with
`$TTP_BOXES_FILE` or `--site <path>`). Never commit it, and never copy its values into a repo,
prompt template or skill.

## Schema

```json
{
  "version": 1,
  "boxes": {
    "<box-alias>": {
      "route": "slurm",
      "jump": "<jump-alias>",
      "workdir_root": "/<shared-path>",
      "scratch_root": "/<node-local-path>",
      "slurm": {
        "login": "<login-alias>",
        "partitions": [
          {"name": "<partition>", "chips": 4, "board": "<board-type>", "exclusive": true}
        ]
      },
      "demo_nodes": ["<node>"],
      "excluded_nodes": ["<node>"],
      "mcp": {"<server-name>": "<endpoint>"}
    }
  }
}
```

Box entries always sit inside the `{"version": 1, "boxes": {...}}` wrapper; a file of bare entries
fails `box.py site check` with `missing version/boxes wrapper`.

| Field | Needed | What |
|---|---|---|
| `boxes` key | yes | the box's `ttp machines` alias, also its ssh config alias |
| `route` | yes | `bare`, `ird` or `slurm` (exabox is `slurm`: `exabox.md`) |
| `jump` | no | ssh alias of the jump host; the box's ssh config uses it as `ProxyJump` |
| `workdir_root` | yes | absolute path on the box for checkouts and build trees (shared storage on a cluster) |
| `scratch_root` | no | absolute node-local scratch path; may be wiped when a job ends |
| `ird.mgmt` | `ird` | ssh alias of the reservation management host |
| `ird.pools` | `ird` | non-empty list of pool names the project may reserve from |
| `ird.arch` | no | the arch `ird_e2e.sh` reserves (`ird.md`) |
| `slurm.login` | `slurm` | ssh alias of the login node |
| `slurm.partitions` | `slurm` | non-empty list: `name`, `chips` (whole number > 0), `board` (board type), optional `exclusive` (whole-node only) |
| `demo_nodes` | no | nodes kept for demos: never allocate them |
| `excluded_nodes` | no | nodes never to use |
| `mcp` | no | site-only MCP endpoints by server name |
| `note` | no | free text for the reader |

Other keys are reported as warnings, not errors.

## Check it

`<plugin-root>/skills/setup-box/scripts/box.py site check` exits 0 and prints one line per box
when the file is valid, 1 with one line per problem when it is not or is missing.
`box.py site show <box-alias>` prints one box's entry as JSON.
