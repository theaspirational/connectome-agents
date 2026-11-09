# Publication gate coverage

`scripts/check-public.sh` scans tracked and nonignored untracked working-tree
files, including their paths. Explicit file arguments replace file discovery.
`--range A..B` additionally checks each selected commit's full message and both
raw author/committer names and emails. It does not inspect every ref, historical
file blobs, ignored files or remote repositories. Run it on the exact publication
branch; do not mirror unrelated refs. It performs no network operations.

## External private policy

The private repository keeps the denylist and runs this checker against the
public candidate before publishing. No real private identifiers, fragments or
encoded versions belong in this public repository, including fixtures and docs.
Tests load only neutral synthetic terms from temporary deny files.

Pass `--deny-file PATH` (repeatable) and/or set `CONNECTOME_PUBLIC_DENYLIST` to an
external file path. All supplied files are combined. Files are UTF-8, one literal
term per line; surrounding whitespace, blank lines and full-line `#` comments
are ignored. Terms match case-insensitively with non-word boundaries (or text
edges) on both sides; regex punctuation is literal. They apply to every view,
including prose and Git identities, with no attribution exception.

With no loaded terms, the checker prints a clear warning and checks only generic
rules. **Use `--require-denylist` in private publication automation**: it rejects
missing, empty or comment-only policy. Explicit unreadable or invalid UTF-8 files
always fail, even without this flag. The shell wrapper forwards all options:

```sh
scripts/check-public.sh --require-denylist --deny-file <external-policy-file> --range <initial-commit>..HEAD
```

Keep the file outside the public tree. Supplying a policy does not exempt it
from scanning if it is otherwise in scope. Generic-only local checks are useful
but are not private-identifier publication approval.

## Plain config policy

**Public config must be plain scalars; the checker refuses the rest.**
For files with case-insensitive `.yml`, `.yaml` or `.json` suffixes, all string
keys and values must be unescaped, single-line scalars. Simple single/double
quote delimiters are allowed, as required by JSON; YAML doubled-apostrophe
escapes and all backslashes in scalar source text are refused. The gate refuses
physical or decoded multiline scalars, literal/folded blocks, explicit tags
(including `!!binary`), anchors, aliases, merge keys and complex mapping keys.
Malformed config is rejected, not silently skipped. JSON must also parse as JSON.
YAML mappings, sequences, ordinary numbers, booleans and nulls remain allowed.
PyYAML composes nodes without constructing tagged objects.

There are only two multiline exceptions, both in
`roles/connectome_agent/tasks/main.yml`: the existing `connectome_effective`
and `connectome_resolved_secrets` Ansible expressions. `MULTILINE_SCALARS` records
exact repository-relative file and node paths (including sequence indexes), not
basenames, globs or content-based exemptions. These exceptions allow multiline
syntax only, never escapes, tags, anchors or aliases. Every physical source line
and every logical value line gets the full rule table with its parent key.
Adding or moving a block requires an explicit checker-policy review.

## One rule table, raw text plus config context

Every input retains its raw text, including comments. The same active rule table
(`RULES` plus the external denylist rule, when supplied)
checks raw files, paths and requested Git metadata. Accepted config scalars also
get raw scalar, synthetic value (`scalar: <line>`), key (`<line>:`) and parent-key
(`parent: "<line>"`) views. Keys are scalars too; sequences retain their parent
key for their members. Every rule runs over every view; no decoded-only subset.
Nonempty scalar credential values are treated as literals even when unquoted.
Per-input findings are deduplicated by rule; diagnostics identify the file or
commit/metadata field and rule names, never suspected content values.

Markdown documents and all other non-config files receive **raw-text scanning
only**. Fenced examples are not parsed as config. There is no Unicode-escape,
percent, base64 or recursive decoding pipeline. Arbitrary encodings, binaries,
encrypted data and concatenated program strings have no decoded-coverage claim.

## Rules applied to every view

The implementation's rule table is authoritative for spelling, boundaries and
case sensitivity. The policies are:

| Rule | Coverage |
| --- | --- |
| Denied identifier (external policy only) | Literal terms from the supplied deny files, case-insensitive and bounded as described above. |
| Private path | Selected service, home-account and support-agent installation paths defined in the table. |
| IPv4 address | Four decimal groups of one to three digits, without validating octet ranges. |
| IPv6 address | The table's conservative colon-separated hex heuristic, not full address validation. |
| Private hostname | Hostnames ending in `.internal`, `.lan`, `.local` or `.ts.net`, case-insensitively. |
| Provider token | Git hosting token prefixes with at least 12 token characters; Slack token prefixes with at least 12 characters. |
| API token | OpenAI-style prefix with at least 16 token characters, or Vault-style prefix with a nonempty token. |
| Bot token or private key | An 8–10 digit bot identifier followed by at least 30 token characters, or a private-key PEM header. |
| JWT | Three base64url-style dot-separated components with the expected encoded-JSON prefix. |
| Secret store path | Case-insensitive vault/secret path prefix followed by at least two path components. |
| Inventory key | The four explicit host/account keys in the table, in key context. |
| Literal credential | Keys ending in the table's token/password/API-key/secret suffixes with a nonempty quoted literal; parent-key config views supply quotes, including sequence members. Neutral secret **names** in lists remain allowed. |
| Non-public email address | Only `users.noreply.github.com`, or locals `noreply` and `contributors` at `example.invalid`, are allowed (case-insensitive comparison). No arbitrary personal-style example locals. |
| Unreviewed repository identity | HTTPS GitHub owner/repository URLs must match `ALLOWED_REPOS`, ignoring a trailing `.git`. Other URL schemes/hosts are not repository-allowlist checked. |

Tests require a fixture for every rule and exercise accepted scalar keys/values,
nested sequences, refusal of escaped keys/values (`x`, `u`, `U`, newline and
continuation forms), multiline/JSON review probes, tags, aliases, merge keys,
malformed config, exact-path exceptions and redacted diagnostics.
This is a conservative denylist gate, not a complete secret, address or personal
identity detector. Manual publication review remains required.
