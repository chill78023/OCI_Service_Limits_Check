# Service Limits Utilization Dashboard

A browser dashboard hosted on OCI Functions that shows service limits, current usage, and utilization across Ashburn, Chicago, Phoenix, and Frankfurt.

- Highlights utilization at or above 80% in red.
- Sends OCI administrator email notifications naming limits that reach 80%, after each daily or manual pull.
- Filters by region, service, search text, and alerts.
- Exports the filtered results to Excel.
- Refreshes daily at 12:00 UTC and displays the last successful pull.
- Pulls fresh limits and usage on demand with **Update service limits**, showing progress while the previous snapshot stays available.
- Keeps the previous complete snapshot if collection fails.
- Protects usage data with OCI identity-domain sign-in.

## Architecture

Three functions share the same image: `web` serves the sign-in page, `api` reads the private snapshot and exports Excel, and `collector` retrieves service limits and usage. OCI Resource Scheduler invokes the collector daily. API Gateway validates OAuth access tokens before invoking the data API. Object Storage holds the snapshot in a private bucket.

## Credentials

Cloud functions use OCI resource principals. No OCI API key, local OCI configuration, or client secret is packaged in the application. The browser uses a public OAuth client with PKCE. Workstation deployment scripts use your existing `~/.oci/config`; credentials and live deployment state are excluded from version control.

## Test

Install the pinned dependencies in a Python environment, then run from this repository root:

```sh
python -m pip install -r function/requirements.txt
python -m unittest test_app.py
```

For a complete first installation, follow the [OCI installation guide](DEPLOYMENT.md), including prerequisites, copyable deployment commands, sign-in setup, initial collection, verification, and troubleshooting. This repository contains source code only; account inventory, usage snapshots, and existing deployment identifiers are not published.
