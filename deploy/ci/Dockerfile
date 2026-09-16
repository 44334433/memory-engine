# CI database image: PostgreSQL 18 + pgvector + PGroonga in one container.
#
# Why not `pgvector/pgvector:pg18` directly: schema.sql requires PGroonga (CJK FTS
# index + pgroonga_score()), and no Debian build of PGroonga exists for PG 18 —
# verified 2026-09-17: packages.groonga.org (Debian) ships up to PG17 only; the
# PG18 package (postgresql-18-pgdg-pgroonga) lives in the groonga Ubuntu PPA.
# Recipe mirrors deploy/pg/stage1_install.sh (the proven production install path).
FROM ubuntu:noble

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg python3 \
 && install -d /usr/share/postgresql-common/pgdg /etc/apt/keyrings \
 && curl -fsS -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main' \
      > /etc/apt/sources.list.d/pgdg.list \
 && FP=$(curl -fsS https://api.launchpad.net/1.0/~groonga/+archive/ubuntu/ppa \
      | python3 -c "import json,sys;print(json.load(sys.stdin)['signing_key_fingerprint'])") \
 && curl -fsS "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${FP}" \
      | gpg --dearmor -o /etc/apt/keyrings/groonga-ppa.gpg \
 && echo 'deb [signed-by=/etc/apt/keyrings/groonga-ppa.gpg] https://ppa.launchpadcontent.net/groonga/ppa/ubuntu noble main' \
      > /etc/apt/sources.list.d/groonga-ppa.list \
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
