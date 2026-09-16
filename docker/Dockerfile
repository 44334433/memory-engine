# PG18 + pgvector + PGroonga 一体镜像
# 基座=pgvector 官方 pg18（Debian）；PGroonga 经 packages.groonga.org apt 源安装
# （ubuntu noble 轮子兼容 Debian trixie 基线；如构建失败改用
#   deploy/pg/stage1_install.sh 的 PPA/回退源链路排查）
FROM pgvector/pgvector:pg18

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://packages.groonga.org/ubuntu/groonga-archive-keyring.gpg \
      -o /usr/share/keyrings/groonga-archive-keyring.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/groonga-archive-keyring.gpg] https://packages.groonga.org/ubuntu noble universe" \
      > /etc/apt/sources.list.d/groonga-org.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-18-pgroonga postgresql-contrib \
 && rm -rf /var/lib/apt/lists/*
