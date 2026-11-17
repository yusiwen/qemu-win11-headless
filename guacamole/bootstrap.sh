#!/usr/bin/env bash
# 从仓库还原 Guacamole 网关。
#
#   生成库表结构 → 起三个容器 → 轮换管理员密码并建连接 → 协议级自检
#
# 幂等：重复运行只会更新配置，不会重建库（除非你手动删了 db/）。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    cat <<'EOF'
错误：缺 .env（里面是 DB 密码和管理员密码，绝不入库）。

生成一份：
  umask 077
  printf 'DB_PASSWORD=%s\nADMIN_PASSWORD=%s\nWEB_PORT=8081\nRDP_HOST=host.docker.internal\nRDP_PORT=13389\nRDP_USER=<微软账户邮箱>\n' \
    "$(openssl rand -hex 16)" "$(openssl rand -hex 16)" > .env
  chmod 600 .env

详见 README.md §3。
EOF
    exit 1
fi

# WEB_PORT 允许写成 127.0.0.1:8081（TLS 覆盖层会这么写），取最后一段当端口
WEB_PORT_RAW=$(grep -E '^WEB_PORT=' .env | cut -d= -f2- || true)
PORT="${WEB_PORT_RAW##*:}"
PORT="${PORT:-8081}"

# 库表结构必须与镜像版本一致：文件头记着来源 tag，这里校验
IMAGE_TAG=$(sed -n 's/^ *image: *guacamole\/guacamole:\(.*\)$/\1/p' docker-compose.yml | head -1)
IMAGE_TAG="${IMAGE_TAG:-1.6.0}"
SOURCE=$(sed -n '1s/^-- source: *//p' initdb.sql 2>/dev/null || true)

if [ ! -s initdb.sql ]; then
    echo "==> 生成 initdb.sql（必须来自镜像自带的 initdb.sh，不要在别处找）"
    {
        echo "-- source: guacamole/guacamole:${IMAGE_TAG}"
        echo "-- regenerate: docker run --rm guacamole/guacamole:${IMAGE_TAG} /opt/guacamole/bin/initdb.sh --postgresql"
        docker run --rm "guacamole/guacamole:${IMAGE_TAG}" /opt/guacamole/bin/initdb.sh --postgresql
    } > initdb.sql
    # postgres 镜像以 uid 70 执行它；umask 077 会让它读不到，症状是容器全 running 但一张表都没有
    chmod 644 initdb.sql
elif [ "$SOURCE" != "guacamole/guacamole:${IMAGE_TAG}" ]; then
    echo "!! 警告：initdb.sql 来自 \"${SOURCE:-未知来源}\"，而 compose 用的是 guacamole/guacamole:${IMAGE_TAG}" >&2
    echo "   库表结构可能与该版本不匹配。确认后重新生成：" >&2
    echo "   docker run --rm guacamole/guacamole:${IMAGE_TAG} /opt/guacamole/bin/initdb.sh --postgresql > /tmp/new.sql && \\" >&2
    echo "     { echo '-- source: guacamole/guacamole:${IMAGE_TAG}'; cat /tmp/new.sql; } > initdb.sql && chmod 644 initdb.sql" >&2
else
    echo "==> initdb.sql 已存在，来源 ${SOURCE}，与 compose 的镜像版本一致"
fi

echo "==> 启动容器"
docker compose up -d

echo "==> 等待网页就绪 http://127.0.0.1:${PORT}/guacamole/"
for _ in $(seq 1 60); do
    if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/guacamole/"; then
        echo "    网页已就绪"
        break
    fi
    sleep 2
done
curl -sf -o /dev/null "http://127.0.0.1:${PORT}/guacamole/" \
    || { echo "网页没起来，看 docker compose logs guacamole"; exit 1; }

echo "==> 管理员密码轮换 + 建连接"
./bin/guac-setup.py

echo "==> 协议级自检（不带 Windows 密码时会停在认证阶段，属正常）"
./bin/guac-probe.py || true

echo
echo "完成。浏览器打开 http://<本机IP>:${PORT}/guacamole/ ，用户 guacadmin，密码见 .env"
