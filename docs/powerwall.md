# Tesla Powerwall Monitoring

The cluster runs a [Telegraf](https://github.com/influxdata/telegraf) [instance](../kubernetes/apps/observability-agents/telegraf/powerwall/helmrelease.yaml) that polls a [pypowerwall](https://github.com/jasonacox/pypowerwall) proxy running on a Raspberry Pi connected to the lead Powerwall's internal Wi-Fi network. The collected data mostly used in a [Grafana dashboard](../kubernetes/apps/observability/grafana/dashboards/configmaps/powerwall.json).

## Proxy container running on pwdash

```sh
docker run -d -p 8675:8675 --name pypowerwall --restart always \
    -e PW_PORT='8675' \
    -e PW_HOST='192.168.91.1' \
    -e PW_GW_PWD='<redacted>' \
    -e PW_TIMEZONE='America/Angeles' \
    -e TZ='America/Los_Angeles' \
    -e PW_CACHE_EXPIRE='5' \
    -e PW_HTTPS='no' \
    -e PW_STYLE='clear' \
    jasonacox/pypowerwall:<tag>
```

`PW_GW_PWD` is a secret stored in my spouse-shared 1Password vault.

## pypowerwall-server

<https://github.com/jasonacox/pypowerwall-server>

Next-generation replacement for the proxy, supporting the proxy endpoints, a web UI, and MQTT streaming.

```sh
docker run -d -p 8675:8675 --name pypowerwall-server --restart always \
    -e PW_BIND_ADDRESS='0.0.0.0' \
    -e PW_CACHE_EXPIRE='5' \
    -e PW_GW_PWD='<redacted>' \
    -e PW_HOST='192.168.91.1' \
    -e PW_HTTPS='no' \
    -e PW_PORT='8675' \
    -e PW_STYLE='clear' \
    -e PW_TIMEZONE='America/Angeles' \
    -e TZ='America/Los_Angeles' \
    jasonacox/pypowerwall-server:<tag>
```
