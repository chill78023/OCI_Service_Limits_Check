# Deploying the OCI utilization dashboard

The deployment helpers implement a Sandbox deployment in Ashburn. Review their resource names, region, CIDR ranges, and IAM policies before using them in another tenancy. They create cloud resources and may incur charges. Run commands from this repository root with the OCI Python SDK and pinned application dependencies installed.

## Prerequisites

- A working local OCI CLI configuration in `~/.oci/config` with sufficient permissions for Functions, networking, Object Storage, registry, IAM, identity-domain apps, API Gateway, logging, and Resource Scheduler.
- An existing compartment named `Sandbox`, a Functions VCN with a subnet named `FunctionSubnet`, and a public gateway named `test gateway` with an Internet Gateway route.
- Available VCN address space for `10.90.0.0/24`; the helper creates a private subnet at `10.90.0.0/25` with NAT egress.
- An identity domain and an existing user to assign to the dashboard.

Copy `infrastructure-inventory.example.json` to `infrastructure-inventory.json` and replace the placeholders with your existing resource details. This local inventory and generated `deployment.json` must remain private.

## Build and deploy

1. Run `python inspect_build.py` to retrieve the official Fn Python 3.11 runtime manifest and configuration for AMD64.
2. Run `python prepare_dependencies.py` to install the pinned dependencies into `build/python` for the target Linux runtime.
3. Run `python setup_infrastructure.py` to create the Functions application, private networking, private bucket and registry, OAuth applications, and user assignment. It records resource identifiers in `deployment.json`.
4. Obtain the identity domain's public RSA signing verification keys through authorized administrative access. Save them as a JSON Web Key Set (`{"keys": [...]}`) in the ignored `public-signing-keys.json`. Do not publish private signing material or enable domain-wide public certificate access merely for this app.
5. Run `python package_image.py --push`. Only the three files under `function/`, pinned dependencies, and runtime layers enter the image. The credential check must pass before upload. The generated image reference and digest are saved to local deployment state.
6. Run `python deploy_functions.py` to create the three functions, exact-resource dynamic groups, scoped runtime policies, two gateway deployments, and daily schedule.
7. Run `python enable_logs.py` to enable invocation diagnostics. Run `python enable_gateway_https.py` to attach HTTPS ingress only after its guard confirms the gateway contains exactly the two dashboard deployments.
8. Allow IAM changes to propagate. Invoke the collector in detached mode through OCI Functions, then verify that the private bucket contains a complete `snapshot.json` and a successful `refresh-status.json`.

The application URL is written to local `deployment.json`. The browser path is `/oci-limits/`, and the authenticated data path is `/oci-limits-data/`.

## Verification and maintenance

Run `python -m unittest test_app.py` and `python check_credentials.py`. The credential audit requires the configured local private key and built image layout; it checks that this key is absent and that packaged application files match the source.

Verify that the browser page loads, unauthenticated data requests receive HTTP 401, OCI sign-in succeeds, and the displayed last-pull timestamp matches the latest complete snapshot. Check filters, Excel export, and the 80% threshold. Confirm the schedule is ACTIVE and verify its first automatic run; its expression is `0 12 * * *` UTC.

The gateway uses a static copy of the domain's public verification key. When the identity provider rotates its signing key, refresh local `public-signing-keys.json` and rerun `deploy_functions.py` to update the API deployment. No private key is required for token verification.

Deployment helpers retain identifiers to avoid recreating tracked resources, but are not a general infrastructure reconciliation tool. In particular, existing functions are not automatically updated to a new image by `deploy_functions.py`; use the OCI Functions update API or CLI to update all three functions to the newly verified image and digest for later releases. Preserve local state securely and inspect changes before rerunning deployment scripts.

OAuth authorization requests use the full scope URL, while the issued access token contains the resource-relative scope `read`. The gateway requires `read` through route authorization and independently validates the exact API audience, issuer, signature, and expiry.
