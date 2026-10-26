# Install the Service Limits Utilization Dashboard on OCI

This guide installs a new instance of the application from this repository. It covers the current deployment scripts, which target **Ashburn (`us-ashburn-1`)** and a compartment named **`Sandbox`**. Existing installations should use the upgrade instructions below rather than repeat the first-install steps.

The result is a browser application with OCI sign-in, a private data snapshot, a daily refresh at **12:00 UTC**, an **Update service limits** button, and Excel export. Cloud functions use resource principals; your workstation API key stays on your workstation.

## 1. Prepare your OCI environment

Have a tenancy administrator and identity-domain administrator approve the deployment permissions. The deploying account must be able to create or manage Functions, container repositories, Object Storage buckets, networking, API Gateway deployments, IAM policies and dynamic groups, identity-domain applications and user grants, logs, and Resource Scheduler schedules. Creating IAM policies and dynamic groups requires tenancy-level access. These administrative permissions are for installation; the application receives narrower runtime policies.

In Ashburn, prepare these existing resources:

| Requirement | Value expected by the scripts |
| --- | --- |
| Compartment | `Sandbox` |
| Functions network | An existing VCN with an existing subnet named `FunctionSubnet` |
| Public API Gateway | A gateway named `test gateway`, dedicated to this application |
| Gateway network | A public subnet with a route to an Internet Gateway |
| Identity domain | Its HTTPS domain URL and an existing user to assign to the application |

The gateway must start without other API deployments. The HTTPS helper refuses to attach its rule unless the gateway contains exactly this application's two deployments. The gateway can use a different VCN from the Functions VCN.

The infrastructure helper adds `10.90.0.0/24` to the Functions VCN, creates a private subnet at `10.90.0.0/25`, and adds a dedicated NAT gateway and route table. Confirm the additional CIDR does not overlap your network or connected networks. Ensure the private subnet's applicable security rules permit DNS resolution and outbound HTTPS, and that the gateway has network connectivity to OCI Functions. Check available service limits for these resources before installation. Resource usage and network traffic can incur OCI charges.

The collector currently queries Ashburn, Chicago, Phoenix, and Frankfurt. Your tenancy must be subscribed to all four regions. If you need a different set, edit `REGIONS` in `function/func.py` before building. The deployment location and registry remain Ashburn unless you also adapt the region-specific deployment scripts.

## 2. Prepare your workstation

Use macOS or Linux with Git, Python 3.11, and the OCI CLI installed. Configure the CLI's **DEFAULT** profile for the target tenancy, with `region=us-ashburn-1`, a valid API signing key, and its fingerprint. If configuration is not already present, use `oci setup config` and follow Oracle's instructions for registering the public API key.

Keep `~/.oci/config` and its private key outside this repository. Confirm that a simple OCI CLI read works before continuing. The helpers use the DEFAULT profile; setting the CLI's `--profile` for another command does not change their profile.

```sh
git clone https://github.com/chill78023/OCI_Service_Limits_Check.git
cd OCI_Service_Limits_Check
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r function/requirements.txt
python -m unittest test_app.py
```

Run all remaining commands from this repository root with the environment activated. Docker and the Fn CLI are not required by the included image-packaging workflow. The image targets Linux AMD64, including when built on an Apple Silicon workstation.

## 3. Supply existing resource information

```sh
cp infrastructure-inventory.example.json infrastructure-inventory.json
```

Edit `infrastructure-inventory.json` and replace every placeholder:

- `compartment_id`: the OCID of your `Sandbox` compartment.
- `vcn_id`: the OCID of the Functions VCN.
- `display_name` under `subnets`: retain `FunctionSubnet`; confirm that subnet exists.
- Gateway `id`: the OCID of your dedicated public gateway.
- Gateway `hostname`: its OCI hostname, without `https://` or a path.
- Gateway `display_name`: retain `test gateway`.
- Identity-domain `url`: its HTTPS URL, without a trailing slash.
- `dashboard_user_name`: the exact identity-domain username to authorize.

Use the example file's JSON structure. Do not put a private key, password, or client secret in this file. Local inventory and deployment-state files are ignored by Git.

## 4. Create infrastructure and browser sign-in

```sh
python setup_infrastructure.py
```

This creates the private bucket and registry, private function network, Functions application, public PKCE OAuth client, API resource registration, and user assignment. It saves generated identifiers in `deployment.json`; preserve this file securely. Do not replace it with state from another installation.

The browser redirect and landing URL are the gateway hostname followed by `/oci-limits/`. The API path is `/oci-limits-data/`. The OAuth resource audience is `https://oci-limits-dashboard.sandbox/`, and the requested scope is that audience followed by `read`. The issued token's scope is `read`; the API gateway checks it together with the exact audience.

This step does not deploy the function image or produce a data snapshot yet.

## 5. Retrieve public token-verification keys

Use authorized identity-domain access to retrieve the domain's public signing keys. The following workstation-only command signs the request using your existing local configuration. It writes only public RSA verification fields to the ignored `public-signing-keys.json` file.

```sh
python - <<'PYKEYS'
import json
from pathlib import Path
import oci
from oci._vendor import requests
from oci_local_access import config

state = json.loads(Path('deployment.json').read_text())
signer = oci.signer.Signer(
    tenancy=config['tenancy'], user=config['user'],
    fingerprint=config['fingerprint'],
    private_key_file_location=config['key_file'],
    pass_phrase=config.get('pass_phrase'))
result = requests.get(
    state['identity_domain_url'].rstrip('/') + '/admin/v1/SigningCert/jwk',
    auth=signer, timeout=30)
result.raise_for_status()
keys = []
for key in result.json()['keys']:
    if key.get('kty') != 'RSA' or key.get('alg', 'RS256') != 'RS256':
        continue
    keys.append({**{name: key[name] for name in ('kid', 'kty', 'n', 'e')},
                 'alg': 'RS256'})
if not 1 <= len(keys) <= 10:
    raise RuntimeError('Expected 1–10 supported RSA verification keys')
Path('public-signing-keys.json').write_text(json.dumps({'keys': keys}, indent=2))
print('Public verification keys saved; no private key was exported.')
PYKEYS
```

If the request returns 401 or 403, have the identity-domain administrator provide the JWKS or the required administrative access. Leave the domain-wide anonymous signing-certificate setting unchanged. The gateway uses its local public-key copy.

## 6. Build and upload the function image

```sh
python inspect_build.py
python prepare_dependencies.py
python package_image.py --push --tag initial-install-v1
```

The first command retrieves the official Fn Python 3.11 runtime manifest. The second downloads the pinned Linux dependencies into `build/python`. The third builds the image, checks for the configured private key and credential paths, and uploads the image to the private Ashburn registry. Stop if the audit reports findings.

Only the runtime layers, dependencies, and three files under `function/` enter the image. Workstation configuration, inventory, deployment state, and private keys are excluded. Registry upload uses a signed short-lived token, so this workflow does not require putting a registry password in source code. `deployment.json` records the image tag and digest.

## 7. Deploy the functions, API, schedule, and logging

```sh
python deploy_functions.py
python enable_logs.py
python enable_gateway_https.py
```

The function deployment creates:

| Function | Purpose | Memory | Timeout |
| --- | --- | --- | --- |
| `oci-limits-web` | Browser page and OAuth code exchange | 256 MB | 30 seconds |
| `oci-limits-api` | Authenticated snapshot, export, refresh request, and status | 512 MB | 60 seconds |
| `oci-limits-collector` | Read OCI limits and usage; publish a complete snapshot | 1,024 MB | 300 seconds sync; 900 seconds detached |

The scripts also configure exact-function dynamic groups and policies, JWT authentication, scope authorization, a private snapshot bucket, and daily detached collection. The API gets `COLLECTOR_FUNCTION_ID` and `COLLECTOR_INVOKE_ENDPOINT` and may invoke only that collector. The collector may read limits and usage and read/write its named snapshot bucket. The web function has no snapshot-reading permission.

Invocation logs retain diagnostics for 30 days. The HTTPS helper attaches TCP 443 ingress to the gateway through a dedicated network security group. The data API continues to require a valid signed token with the correct audience and `read` scope. Allow time for IAM memberships and policy updates to propagate before the first invocation.

## 8. Populate the first snapshot

You can sign in and select **Update service limits** once a snapshot exists. For the initial installation, invoke the collector from your workstation to create that first snapshot:

```sh
python - <<'PYFIRST'
import json
from pathlib import Path
import oci
from oci_local_access import client, config

state = json.loads(Path('deployment.json').read_text())
function = client(oci.functions.FunctionsManagementClient).get_function(
    state['collector_function_id']).data
invoke = oci.functions.FunctionsInvokeClient(
    config, service_endpoint=function.invoke_endpoint, timeout=(10, 30))
invoke.invoke_function(function.id, invoke_function_body=b'{}',
                       fn_invoke_type='detached')
print('Collection accepted. Check status after a few minutes.')
PYFIRST
```

Detached acceptance means collection has started or been queued; it does not confirm completion. Check the private status object:

```sh
python - <<'PYSTATUS'
import json
from pathlib import Path
import oci
from oci_local_access import client

state = json.loads(Path('deployment.json').read_text())
store = client(oci.object_storage.ObjectStorageClient)
result = store.get_object(state['namespace'], state['bucket'],
                          'refresh-status.json')
print(json.dumps(json.loads(result.data.content), indent=2))
PYSTATUS
```

Repeat the status check as needed until `status` is `Succeeded`. Check that `snapshot.json` exists in the same private bucket. If the status is `Failed`, inspect the collector's invocation log, correct the cause, and invoke it again. A complete pull can take several minutes. Existing snapshots are retained if a later pull fails.

## 9. Open and verify the application

Print the URL, then open it in your browser:

```sh
python - <<'PYURL'
import json
from pathlib import Path
print(json.loads(Path('deployment.json').read_text())['app_url'])
PYURL
```

Sign in as the user assigned during installation and complete any required MFA. Verify:

1. The title is **Service Limits Utilization Dashboard**.
2. Limits, current use, utilization, and the last-pull timestamp appear.
3. Filters and **Download Excel** work; utilization of 80% or more is red.
4. **Update service limits** shows progress, retains the old snapshot during collection, and changes the timestamp after success. **Reload snapshot** only rereads saved data.
5. In the OCI Console, the Resource Scheduler schedule `oci-limits-daily-refresh` is ACTIVE. Confirm that the next daily run completes; the schedule uses `0 12 * * *` UTC. Browser timestamps use the browser's local timezone.
6. An anonymous request to the data endpoint is denied. You can run this read-only check:

```sh
python - <<'PYANON'
import json
from pathlib import Path
from oci._vendor import requests
state = json.loads(Path('deployment.json').read_text())
result = requests.post(state['api_url'], json={'action': 'snapshot'}, timeout=30)
print('Anonymous response:', result.status_code)
assert result.status_code == 401
PYANON
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Hostname does not resolve / `NXDOMAIN` | Verify the hostname in `deployment.json` against the gateway. Check DNS on the affected network before changing the application or OAuth settings. |
| OCI sign-in succeeds but the API returns 401/403 | Check the user assignment, exact redirect URL, token audience, issuer, public signing key, and the resource-relative scope `read`. Check gateway execution logs if enabled. |
| First pull fails with 401/403/404 | Check IAM propagation and the collector's limits/resource-availability and bucket permissions. Check all configured regions are subscribed. |
| Manual update cannot start | Check both collector environment variables and the API dynamic group's permission to invoke the exact collector. |
| Update remains Running | Check collector logs. The function's detached timeout is 15 minutes; the concurrency lease expires after 20 minutes. Avoid editing a valid active lease. |
| Container build/push fails | Check outbound HTTPS to Docker Hub, PyPI and OCIR, registry permissions, and the credential-audit output. |
| HTTPS helper refuses the gateway | Use a dedicated gateway containing only the two dashboard deployments; retain the guard. |
| Existing snapshot remains after an error | This is expected: unsuccessful collection does not replace the last complete snapshot. |

## Upgrades, key rotation, and local state

Use a new image tag for every release, rerun tests and the image audit, and upload the new image. **`deploy_functions.py` does not change existing functions to a newly built image.** Update all three functions through the OCI Console or Functions update API to the new image reference and digest in local `deployment.json`; preserve their role-specific configurations, including the API's collector settings. Verify one manual refresh after an upgrade.

When the identity domain rotates its signing keys, repeat step 5 and rerun `deploy_functions.py` to update the API gateway's verification keys. The current helper also reconciles its runtime policy and API collector environment variables. Review the planned change and keep local state intact.

Keep `deployment.json`, inventory, state files, `.oci` files, and private keys out of Git. Back up local deployment state securely. The scripts are tailored deployment helpers, not a general infrastructure reconciliation or automated uninstall system. Changing compartment names, deployment region, registry hostname, CIDRs, or resource names requires reviewing the relevant scripts and IAM statements together.

## Administrator notifications at 80%

After deploying the current image to all three functions, run the following from the repository root:

```sh
python setup_notifications.py --administrator-group Administrators
```

The setup helper discovers the active members of the exact IAM `Administrators` group, creates `oci-service-limits-80-alerts` in Sandbox, and creates email subscriptions for their current email addresses. It configures the collector with `NOTIFICATION_TOPIC_ID`, `NOTIFICATION_ENDPOINT`, and `NOTIFICATION_COMPARTMENT_ID`. A separate IAM policy grants the collector only `ONS_TOPIC_PUBLISH` and `ONS_SUBSCRIPTION_INSPECT` in the notification compartment. It cannot create topics or change subscriptions at runtime. The installing account needs permission to manage notification topics, subscriptions, IAM policy, and collector configuration.

Each administrator must select the confirmation link in the OCI Notifications email before receiving alerts. The application checks subscription readiness on every complete pull. While no recipient is active it shows a confirmation warning and does not mark thresholds as already notified. Topic acceptance does not guarantee email delivery; OCI Notifications handles delivery to confirmed subscribers.

The subject names the first affected service and limit, for example:

```text
80% of "Services Limit" reached: compute / standard-e5-core-count
```

The message body names every affected limit in that notification and includes its service, exact limit name, description, scope, region, availability domain, current use, maximum, utilization, detection timestamp, and dashboard link. A subject with multiple limits ends with a count; long subjects are shortened while complete names remain in the body. Notifications reflect completed daily or manual snapshots, not continuous monitoring between pulls.

The collector notifies on the first observed breach at or above 80%. It records successful publishes in private `notification-state.json` to suppress repeated alerts while a limit remains high. An observed recovery below 80% re-arms a later crossing. Unsupported usage does not re-arm an alert. Global limits repeated by region are grouped, using their highest observed utilization. Larger sets are split below the message-size limit. OCI Notifications provides at-least-once delivery; a process or state-write failure after publishing can result in a duplicate. A notification failure is reported separately and does not discard the fresh data snapshot.

Rerun the setup helper to add new administrators. Review subscriptions in OCI Notifications and remove departed recipients when administrator membership changes. Local `notification-setup-state.json` contains recipient details and must remain ignored by Git. Do not add email addresses or API keys to application source. If using a differently named IAM administrator group, pass its exact name with `--administrator-group`.

## Oracle references

- [Configure an OCI API signing key](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm)
- [Identity-domain APIs with an OCI API signing key](https://docs.oracle.com/en/learn/ociiam-id-restapi/index.html)
- [Invoking OCI Functions, including detached invocation](https://docs.oracle.com/en-us/iaas/Content/Functions/Tasks/functionsinvokingfunctions.htm)
- [API Gateway token and scope authorization](https://docs.oracle.com/en-us/iaas/Content/APIGateway/Tasks/apigatewayusingjwttokens-requestpolicy.htm)
