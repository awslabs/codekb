# CodeKB Roadmap

This roadmap shows what the CodeKB team is working on and what we plan to look at next. It reflects current priorities, not commitments. Items can change order, change shape, or be dropped, and work that isn't listed here can still ship.

- **Now:** in progress or starting next.
- **Next:** planned after the current work.
- **Later:** planned, but further out.
- **Exploring:** we're evaluating whether and how to do it.

To ask for something, or to tell us an item matters to you, [open an issue](https://github.com/awslabs/codekb/issues). Describe your use case, not only the feature. Please report security issues through the process in [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications), not in a public issue.

## Now

- **Scale-out deployments:** MCP and the MCP OAuth flow across multiple containers, with any load balancer and no session affinity.
- **Infrastructure-managed authentication:** your infrastructure (for example a load balancer or gateway) authenticates users, and CodeKB enforces which spaces and hyperspaces each user can read and change.
- **`.codekbignore`:** per-repository exclusion patterns, also editable in the UI.
- **ZIP uploads with symbolic links.**
- **OpenTelemetry metrics** (API and MCP latency and errors, token usage) with a CloudWatch dashboard.
- **Tests:** automated front-end tests and expanded backend unit tests.

## Next

- **AI gateway support:** route model calls through a gateway you run, such as Portkey.
- **Benchmarking** to measure changes to models, prompts and parsing.
- **Resumable analysis:** analysis and refresh resume automatically after container restarts and deployments.
- **Stop a running analysis** or refresh.
- **Space-level locking** for operations that change a space.
- **More Bedrock model families.**
- **Model choice per space and per Agent Hopper chat,** in the UI.
- **Cost and time estimates** before analyzing a repository.
- **Automatic refresh:** on a schedule.
- **MCP OAuth with Client ID Metadata Documents,** alongside dynamic client registration.
- **Audit log** of who did what, with its own dashboard.
- **Saved Agent Hopper chat history** across sessions.
- **Custom Agent Hopper prompts** and a custom vocabulary per deployment.
- **UI localization,** starting with Japanese.
- **New languages and file types:** Scala, Groovy, CloudFormation templates (YAML and JSON), and Vue single-file components (`.vue`).
- **Bedrock Converse API** for all model calls.

## Later

- **Dynamic hyperspaces:** CodeKB finds which spaces are connected (for example through service calls or shared libraries) and creates hyperspaces around them.
- **Free-threaded Python 3.15,** once benchmarks show a gain and our native dependencies support it.
- **Backend for frontend:** a server-side layer that handles tokens and API composition.
- **Amazon Bedrock AgentCore** as a hosting option.
- **Agent Hopper built on top of Kiro CLI.**
- **More languages:** COBOL, SQL, Swift and Objective-C, then RPG.

## Exploring

- **Amazon Verified Permissions** for authorization policies.
- **IAM SigV4 authentication** for machine-to-machine access.
- **Non-code material:** design documents, ADRs and API specifications, linked to the code they describe.
- **Deeper code insights:** change impact analysis, dead-code detection, and mapping tests to the components they cover.
