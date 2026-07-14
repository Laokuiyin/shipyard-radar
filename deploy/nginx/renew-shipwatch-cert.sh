#!/bin/sh
set -eu

cd /opt/dify-1.14.2/docker
/usr/local/bin/docker-compose run --rm --entrypoint certbot certbot renew --quiet
docker exec docker-nginx-1 nginx -s reload
