# FAQ

- [How much does CodeKB cost to run?](#how-much-does-codekb-cost-to-run)
- [How large a codebase can it handle, and how big should the container be?](#how-large-a-codebase-can-it-handle-and-how-big-should-the-container-be)
- [Can several people share one instance?](#can-several-people-share-one-instance)
- [How do I analyze a private repository?](#how-do-i-analyze-a-private-repository)
- [My repository changed. Do I have to analyze it again?](#my-repository-changed-do-i-have-to-analyze-it-again)
- [How do exclusion patterns work?](#how-do-exclusion-patterns-work)
- [What data does CodeKB store, and where?](#what-data-does-codekb-store-and-where)
- [Will my spaces still work after an upgrade?](#will-my-spaces-still-work-after-an-upgrade)
- [How do I turn on debug logging?](#how-do-i-turn-on-debug-logging)
- [Can I run CodeKB without Docker?](#can-i-run-codekb-without-docker)

### How much does CodeKB cost to run?

You pay for the AWS services it uses. Per analysis, the main cost is Amazon
Bedrock, mostly for summarization. After each analysis the space shows its token
usage per operation, with a link to
[Bedrock pricing](https://aws.amazon.com/bedrock/pricing/).

To spend less on Bedrock, choose a cheaper model (Claude Sonnet costs less than
Opus) or use the analysis option that skips components with low business
relevance. `CODEKB_SUMMARIZATION_CONCURRENCY` changes speed, not cost.

The CDK deployment also has continuous infrastructure costs; see
[Deploying to AWS → Cost](deployment.md#cost). Storage cost depends on source
size, vector count, object versions, access logs and retention.

### How large a codebase can it handle, and how big should the container be?

An analysis reads at most 10,000 eligible source files and skips the rest, so
analyze focused repositories. Runtime depends mainly on parsed component count,
model latency, Bedrock quota and `CODEKB_SUMMARIZATION_CONCURRENCY`; measure a
representative repository in your account rather than relying on a fixed estimate.

Analysis holds the component catalog and working tree in memory and on disk. Size
one analysis from its measured peak memory and disk use, then multiply by the
number of concurrent jobs you allow and leave headroom for the API and cache. More
vCPUs help when summarization concurrency is high.

### Can several people share one instance?

Yes, with sign-in on: use the [CDK deployment](deployment.md). Each user then
sees the spaces their Git access allows
([space authorization](security.md#space-authorization)). Analyses share the
instance's CPU, memory and Bedrock quota, so several large ones at once slow each
other down.

### How do I analyze a private repository?

Save a personal access token on the **Git credentials** page, then create the
space from the repository's normal HTTPS URL. CodeKB encrypts the token and uses
it to clone; it is never put in the URL or the stored source archive. Use a
read-only token limited to the repositories you need.

When you create a space, CodeKB asks the provider whether the repository is
public or private. If it can't tell, creation fails: 400 for an unsupported host
or a rejected token, 503 if the provider is unreachable or rate-limiting (retry).

### My repository changed. Do I have to analyze it again?

No. For a Git space, click **Refresh space**. CodeKB clones the latest commit,
finds the added, changed and removed files, and re-analyzes only those. Zip spaces
can't be refreshed; upload the new archive as a new space.

### How do exclusion patterns work?

CodeKB skips files matching the built-in patterns in `default.yaml` (tests, build
output, dependencies, AI-agent folders) plus `CODEKB_EXCLUDE_PATTERNS_EXTRA`. The
**Settings** page shows the current list.

A space keeps the patterns it was created with, and every refresh uses them. So a
new pattern, or a release with different defaults, affects new spaces only. To
apply the current patterns to an existing repository, delete the space and create
it again. CodeKB asks you to confirm before refreshing a space whose patterns
differ from the current ones.

### What data does CodeKB store, and where?

Persistent data is in your AWS account. The S3 bucket holds knowledge bases,
archives of the analyzed source, generated overviews and saved Git tokens. S3
Vectors holds embeddings of component summaries. CodeKB also keeps temporary
working trees and an extracted-source cache under `/tmp/codekb` on the host or
container disk.

A completed space deletion removes its live S3 objects and vectors. In a
CDK-created, versioned bucket, older object versions remain until the lifecycle
policy expires them. See [Decommissioning](security.md#decommissioning) for complete removal.

### Will my spaces still work after an upgrade?

Usually, yes. Upgrades don't modify stored spaces. If a space made by a much older
version fails to load, refresh it (Git spaces) or upload it again (zip spaces).

### How do I turn on debug logging?

Set `CODEKB_LOG_LEVEL=DEBUG`. It adds payload dumps truncated to
`CODEKB_LOG_PAYLOAD_MAX` characters (4,000 by default). For AWS SDK and HTTP
request traces as well, also set `CODEKB_THIRD_PARTY_LOG_LEVEL=DEBUG`.
Setting the limit to `0` can record complete prompts, responses and analyzed source.
Use DEBUG briefly, restrict access to the logs, and restore the previous level.
See [Configuration → Logs](configuration.md#logs) for where logs go.

### Can I run CodeKB without Docker?

For development: `./scripts/run.sh` runs the backend and the Web UI from source
(see [Development](development.md)). Otherwise use the image, which pins the Python
version, the parser libraries and the built UI.
