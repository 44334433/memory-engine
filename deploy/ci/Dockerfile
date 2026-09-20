# CI database image: PostgreSQL 18 + pgvector + PGroonga in one container.
#
# Why not `pgvector/pgvector:pg18` directly: schema.sql requires PGroonga (CJK FTS
# index + pgroonga_score()). Install source (fixed 2026-09-19, matches the proven
# production path in deploy/pg/stage1_install.sh and docker/Dockerfile):
#   - PG18 server + pgvector come from PGDG (noble-pgdg).
#   - PGroonga comes from packages.groonga.org/ubuntu noble universe.
#   - The groonga Launchpad PPA is NOT usable: it ships no PG18 build for noble
#     (apt-cache madison verified 2026-09-16 — earlier CI recipe used it and
#     failed with exit 100 on `apt-get install postgresql-18-pgdg-pgroonga`).
#   - Keyring path (fixed 2026-09-20): the keyring file lives under the
#     packages.groonga.org/debian/ prefix — the /ubuntu/ path 404s (curl 22 was
#     the CI run-31 build failure). Signature verified locally with gpgv:
#     /debian/groonga-archive-keyring.gpg = RSA 624CF77434839225 "Groonga Key
#     (Groonga Official Signing Key)" makes a Good signature on
#     ubuntu/dists/noble/InRelease (the very Release apt must trust here).
#     Production host runs the same repo (packages.groonga.org/ubuntu noble
#     universe, postgresql-18-pgdg-pgroonga 4.0.8-1 installed via
#     /etc/apt/trusted.gpg.d/groonga.gpg).
FROM ubuntu:noble

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg \
 && install -d /usr/share/postgresql-common/pgdg /etc/apt/keyrings /usr/share/keyrings \
 && curl -fsS -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main' \
      > /etc/apt/sources.list.d/pgdg.list \
 && curl -fsSL https://packages.groonga.org/ubuntu/groonga-archive-keyring.gpg \
      -o /usr/share/keyrings/groonga-archive-keyring.gpg \
 && echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/groonga-archive-keyring.gpg] https://packages.groonga.org/ubuntu noble universe' \
      > /etc/apt/sources.list.d/groonga-org.list \
 && apt-get update -o Acquire::Retries=3 \
 && apt-get install -y --no-install-recommends \
      postgresql-18 postgresql-client-18 \
      postgresql-18-pgvector postgresql-18-pgdg-pgroonga \
 && rm -rf /var/lib/apt/lists/*

COPY pg-entrypoint.sh /usr/local/bin/pg-entrypoint.sh
RUN chmod +x /usr/local/bin/pg-entrypoint.sh \
 && mkdir -p /var/lib/postgresql/data /var/run/postgresql \
 && chown -R postgres:postgres /var/lib/postgresql /var/run/postgresql

EXPOSE 5432
ENTRYPOINT ["/usr/local/bin/pg-entrypoint.sh"]
