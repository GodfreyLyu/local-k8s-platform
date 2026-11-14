<!-- Purpose: Record completed checks, native Terraform results, and the limits of the evidence. -->

# Validation

This report includes the original build checks from 2026-11-13. Typed Terraform inputs were revalidated on 2026-11-14. The results cover the implementation and static Terraform validation. Live deployment and functional acceptance remain pending.

## Results

| Check | Result |
|---|---|
| Offline Python tests | 72 passed on Python 3.10. Coverage includes configuration, workspace files, JSON I/O failure handling, plan integrity, ownership, credentials, startup, and validation orchestration. |
| Local certificate preparation | OpenSSL generated and verified temporary certificates. A second run reused the credentials. Test material was removed afterward. |
| Independent agent trial | A PostgreSQL-only request produced the expected stages and application settings. It made no calls to kubectl, Terraform, Minikube, or OpenSSL. |
| Python lint and formatting | Ruff checks passed. Ruff is optional for development; the runtime needs no third-party Python packages. |
| Skill structure and documentation | The skill validator, relative links, and required file and function comments passed inspection. |
| Terraform formatting | All five stage roots passed recursive format checking. |
| Native provider check | Schema loading passed with Terraform `1.15.8` and Kubernetes provider `2.38.0`. |
| Native configuration matrix | All 12 profiles and 40 stage instances passed, with zero errors and zero warnings. |
| Typed input contracts | All five exact stage variable declarations accepted the normalized environment in provider-free, resource-free console fixtures. Ten negative cases rejected missing `cluster.kubeconfig` or an invalid `cluster.context` type. |
| Terraform IDE inspections | All 12 Terraform files passed IDE inspection with no errors or warnings after adding explicit stage input types. |
| Live acceptance | Pending. These checks did not deploy resources or test application connections. |

## Native Terraform run

The report returned `status=pass`, `provider_probe=pass`, and `cases_passed=cases_planned=40`.

The audit checked every command result. All 40 initializations and all 40 validations exited 0. Each validation returned `valid=true`, no errors, no warnings, and an empty diagnostic list.

These static checks cover attribute references and configuration consistency. The additional console fixtures evaluate actual input conversion against each stage's explicit `object` type. They contain no providers, resources, or remote backend.

All 24 stage source files and lockfiles matched the copies used by the native run. Generated variables also matched the current configuration logic. Coverage included every Terraform root:

| Stage | Passing profile instances |
|---|---:|
| `00-bootstrap` | 12 |
| `05-storage` | 6 |
| `30-postgres` | 8 |
| `31-kafka` | 8 |
| `40-monitoring` | 6 |

[Native validation evidence](native-validation.json) lists the profile coverage and source hashes. It also stores the original report digest and digests for all 80 command diagnostics. Private workspaces and raw TRACE logs are excluded from the package.

These results establish static Terraform and provider-schema consistency. They do not prove live CRD compatibility, service connectivity, permissions, network isolation, alert delivery, or recovery.

## I/O documentation review

The documentation review used `io_utils.py` and its actual callers as the reference. All 14 JSON storage tests and six boundary tests passed on Python 3.10. Seven temporary probe groups also passed. They covered literal arguments, stdin, child permissions, nonzero exits, timeouts, environment filtering, and path-check limits.

These checks do not establish crash recovery or complete protection against concurrent path changes. See [Artifact I/O and subprocesses](io.md) for the implemented limits.

## Run the checks

Run the offline suite from the repository root:

```sh
python3 -B -m unittest discover -s tests -v
```

Check Terraform formatting:

```sh
terraform fmt -check -recursive assets/infra
```

For the validation matrix, choose a new private report directory outside the repository:

```sh
python3 -B scripts/validate.py --report-dir "$HOME/local-k8s-validation/new-run"
```

The runner first checks provider communication. It then validates PostgreSQL-only, Kafka-only, and combined profiles. Each runs with monitoring off/on and storage reuse/create. That gives 12 profiles and 40 selected stage instances.

The runner uses explicit variable files. It needs a Terraform release whose `validate -help` lists `-var-file`; the tested release is `1.15.8`.

The runner does not call Kubernetes or run plan, apply, or credential commands. Provider installation may contact the registry. Add `--plugin-dir /path/to/mirror` to use an offline provider mirror.

Check `report.json` and the private diagnostics after each run. Static validation and live acceptance are separate results.

## Provider startup in a restricted environment

The first run failed before loading the provider schema. TRACE logging showed this error; the temporary path is shortened here:

```text
plugin init error: listen unix <temporary-directory>/plugin<id>: bind: operation not permitted
```

A minimal configuration with only `required_providers` and the lockfile failed in the same way. It contained no platform resources or Kubernetes connection. The execution environment denied the local socket used for provider RPC.

The same provider loaded successfully in the user's native terminal. The complete matrix then passed without changes to the pinned provider or Terraform assets.

If the provider probe hits this permission error, the runner returns `blocked` with `local_socket_denied`. It stops before the stage matrix. A later stage failure is recorded against that case, and the runner continues with other cases. Other provider failures keep their own diagnostic category.

## Packaging

The archive includes a source manifest and the published validation evidence. It excludes private runtime files. You can unpack it and run offline configuration without the original project directory.

Generated application settings remain marked `planned-not-connectivity-verified`. Deployment and functional tests must provide the remaining evidence.
