# SSH-only model connection administration

This runbook configures the live AgentMesh Console/API for administrator operations over an SSH
tunnel when the server has no domain or trusted HTTPS endpoint. It does not deploy anything or
configure secrets. The normal `compose.yaml` remains unchanged; the Linux override is opt-in.

For employee/model and Memory onboarding steps, see
[Set up a real model and employee memory](../model-and-memory-setup.md).

## Security boundary

The override runs only the API in host-network mode so the API can see an SSH-forwarded request as
real loopback traffic. It listens on `0.0.0.0:80` with Uvicorn proxy-header processing disabled.
Because that bind covers every host interface, unauthenticated API requests must be rejected by
global identity/RBAC. The static Console may remain publicly reachable and can display secure-access
guidance, but do not enter or submit bearer tokens, provider keys, or other secrets over public
plain HTTP. Keep SSH access restricted to authorized administrators. Operators may additionally
restrict inbound TCP/80 at the host firewall/security group to trusted sources; this is optional and
does not require publishing a separate informational website. Do not put this API behind an
unverified proxy or Docker port mapping.

From an administrator workstation, open a local tunnel:

```sh
ssh -N -L 18000:127.0.0.1:80 operator@SERVER_ADDRESS
```

Then open `http://127.0.0.1:18000` locally. This is the live Console/API origin; administrator
authentication remains enabled. Secret writes are accepted because the API sees the SSH server's
loopback peer, not a Docker bridge address. `--no-proxy-headers` ensures a client cannot spoof this
check with `X-Forwarded-For`. Do not weaken the loopback/HTTPS check or add trusted-proxy headers.
Before saving a model key, verify the configured SSH path and successful administrator
authentication in the Console. The static UI may also be loaded publicly, but authenticated
administration and secret entry should be done only through the SSH tunnel (or a separately
verified HTTPS ingress).

## Local, untracked configuration

Use a root-owned secret manager or a local env file with mode `0600`; `.env.*` files are ignored by
Git, but verify your repository's ignore rules before use. Never put bearer tokens, provider keys,
the model-connection encryption key, or database credentials in a committed file, task, screenshot,
issue, or shell transcript.

The override requires the following values in the Compose interpolation environment (for example,
an untracked `--env-file .env.ssh-admin`):

- `AGENTMESH_SSH_DATABASE_URL`: API PostgreSQL URL using `127.0.0.1:5432`.
- `AGENTMESH_SSH_CHECKPOINT_DATABASE_URL`: checkpoint PostgreSQL URL using `127.0.0.1:5432`.
- `AGENTMESH_SSH_REDIS_URL`: optional; defaults to `redis://127.0.0.1:6379/0`.
- `AGENTMESH_SSH_IDENTITY_PRINCIPALS_JSON`: static administrator-principal JSON containing the
  same `tenant_id` as `AGENTMESH_TENANT_ID`, role `TENANT_ADMIN`, and only the SHA-256 digest of a
  long random bearer token. Do not put the raw token here. Protect the digest as authentication
  configuration and keep the raw token in an approved secret manager.
- `AGENTMESH_MODEL_CONNECTION_ENCRYPTION_KEY`: one valid Fernet key shared by API and Worker,
  stored outside Git and backed up separately from the database.
- `AGENTMESH_SSH_FEATURE_GATES`: optional extra feature overrides, without
  `identity_rbac=true`; the override always appends that required gate. Defaults enable
  `agent_registry_management=true` and `coordinated_execution=true` so administrators can bind
  model connections to named employees and run coordinated tasks. Preserve feature dependencies
  when adding gates; API and Worker receive the same values.

Keep the PostgreSQL credentials consistent with the database service. The repository's Compose
database credentials are development defaults, not production credentials. For persistent or
production use, change them using your approved database procedure and update every client URL;
prefer a managed PostgreSQL/Redis service where appropriate. Changing an environment password does
not rotate an existing database user's password by itself.

The existing, explicitly scoped provider-key environment variables remain available to the API and
Worker for connections configured to use environment references. Keep those variables scoped to
these services and out of public client assets, logs, screenshots, and committed files. As an
alternative, users can save credentials as encrypted Model Connections through the authenticated
Console. API and Worker must use the same encryption key or the Worker cannot decrypt saved
credentials. Losing the key makes encrypted credentials unusable; key rotation requires a tested
migration/re-entry plan, not simply replacing the variable.

## Validate and start

This override was validated with Docker Compose 2.40.3 and uses the Compose `!reset` merge tag.
Validate the merged configuration with the installed Compose version without printing resolved
values:

```sh
docker compose --env-file .env.ssh-admin \
  -f compose.yaml -f compose.ssh-admin.yaml config --quiet
```

`config --quiet` validates without dumping environment values. Avoid plain `docker compose config`
in terminals or CI logs for this secret-bearing profile. Once the firewall and required local
configuration are reviewed, the operator may start the stack with the same file order:

```sh
docker compose --env-file .env.ssh-admin \
  -f compose.yaml -f compose.ssh-admin.yaml up -d --build
```

The API uses loopback host URLs for PostgreSQL, checkpoints, and Redis. Worker, migrations, and
other services retain ordinary Compose networking and their service-name URLs. PostgreSQL and
Redis remain published only on host loopback by the base Compose file.

## Backups and rollback

Before accepting provider credentials or meaningful task data, back up the database and separately
back up the encryption key in an access-controlled secret store. Test restoring both together;
the database backup alone is not enough to recover encrypted provider credentials. Do not print
the key or bearer token during backup/restore verification.

Do not roll back by restarting the default anonymous Compose profile after data or credentials have
been stored. Keep identity/RBAC, the administrator principal configuration, and the same encryption
key configured for both API and Worker when updating or reverting application images; preserve the
database and secret-store backups together. If changing away from SSH access, keep the API offline
until a separately verified authenticated HTTPS ingress is ready. Do not use `down -v`: it deletes
the database volume. Credential writes must use the verified SSH loopback path or authenticated
HTTPS, never a Docker bridge address.
