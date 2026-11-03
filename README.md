# pyvless

基于 `vs.py` 的 VLESS over WebSocket 服务端 Docker 镜像。运行时只依赖 Python 标准库，镜像使用非 root 用户运行。

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `Dockerfile` | 构建运行镜像 |
| `docker-entrypoint.sh` | 把环境变量转换为 `vs.py` 参数 |
| `healthcheck.py` | 容器健康检查，支持 HTTP 和直接 TLS |
| `docker-compose.yml` | 本地或服务器上的 Compose 部署 |
| `.github/workflows/docker.yml` | 推送到 GitHub 后自动构建并发布 GHCR 镜像 |
| `.env.example` | `docker run --env-file` 的环境变量示例（Compose 不使用） |

## 快速启动

1. 生成 UUID：

   ```bash
   python3 -c "import uuid; print(uuid.uuid4())"
   ```

2. 编辑 `docker-compose.yml`，把 `VLESS_UUID` 替换为上一步生成的 UUID。

3. 构建并启动：

   ```bash
   docker compose up -d --build
   ```

4. 检查状态：

   ```bash
   docker compose ps
   curl http://127.0.0.1:10086/health
   docker compose logs -f vless
   ```

服务默认监听并映射 `10086` 端口，WebSocket 路径默认为 `/vless`。`docker-compose.yml` 中的端口使用固定字面量 `10086:10086`；需要改宿主机端口时直接编辑这一行。

**Cloady 注意：不要写成 `10086:10086/tcp`。** Cloady 会把带 `/tcp` 的端口登记为原始 TCP 端口，而不是 Web 端口。此时 `https://你的域名/health` 不会路由到应用，而会返回平台路由器的 `404 page not found`；原始 TCP 端口又只通过 IPv6 发布，所以 `http://你的域名:10086/health` 还可能超时。请在 `ports` 中去掉 `/tcp`，重新部署。

`docker-compose.yml` 使用全字面量配置，不依赖环境变量文件或 `${...}` 插值，适合校验严格的部署平台。修改 UUID、路径或端口时直接编辑该文件。只用 `docker run` 部署且未设置 `VLESS_PORT` 时，入口脚本才会自动使用平台注入的 `PORT` 变量。

## 容器内排查 502

镜像已安装 `curl`、`ps`、`ss`、`netstat`、`dig`/`nslookup`、`nc` 和 `lsof`。进入容器：

```bash
docker exec -it pyvless sh
```

进入后可直接运行一键诊断：

```sh
diagnose-pyvless
```

它会输出环境变量、`vs.py` 进程、监听端口、IPv4/IPv6 本地健康检查、DNS 解析，以及到公网的出站 TCP/HTTPS 连通性。

平台没有 `docker exec` 时，使用平台控制台的“终端 / Exec / Web Shell”进入实例，然后依次执行：

```sh
# 1. 进程是否存在
ps -ef | grep vs.py

# 2. 是否监听 0.0.0.0:10086
ss -lntp
netstat -lntp

# 3. 容器内部健康检查
curl -v --max-time 5 http://127.0.0.1:10086/health
curl -i --max-time 5 http://127.0.0.1:10086/time
```

判断方式：

- 第 3 步返回 `200`，说明服务本身正常，502 来自平台反向代理。重点检查平台配置的容器端口是否为 `10086`、健康检查路径是否为 `/health`、是否支持 WebSocket Upgrade。
- 第 2 步没有监听记录，查看容器日志。常见原因是 UUID 无效、证书和私钥只配置了一个，或端口被平台覆盖。
- 只监听 `127.0.0.1:10086` 而不是 `0.0.0.0:10086`，说明 `VLESS_HOST` 配错了，应为 `0.0.0.0`。
- 平台如果要求应用监听它注入的 `PORT`，把 `docker-compose.yml` 中的 `VLESS_PORT`、`ports` 和平台容器端口统一改成该端口，三者必须一致。

### 内部 200、外部 502 的排查顺序

502 表示平台反向代理已经收到请求，但没有从容器拿到有效响应。按下面顺序排查：

1. **确认平台代理目标端口**

   在容器内执行：

   ```sh
   env | sort | grep -Ei 'port|vless'
   ss -lntp
   ```

   如果存在 `PORT=3000` 之类的变量，而 `ss` 显示应用只监听 `10086`，平台代理很可能在连 `3000`，于是返回 502。把 `VLESS_PORT`、`docker-compose.yml` 的 `ports`、平台“容器端口”三处统一改成同一个端口。

   启动日志现在也会在 `PORT` 与 `VLESS_PORT` 不一致时输出 `WARNING`。

2. **确认监听地址**

   `ss -lntp` 应显示 `0.0.0.0:端口` 或 `*:端口`。如果显示 `127.0.0.1:端口`，平台代理从容器外部无法连接，设置 `VLESS_HOST=0.0.0.0`。

3. **确认 IPv6 回环**

   ```sh
   curl -g -6 -v --max-time 3 'http://[::1]:10086/health'
   ```

   如果 IPv4 正常、IPv6 拒绝，而平台代理使用 `localhost` 且优先解析到 `::1`，可把 `VLESS_HOST` 改为 `::` 让服务同时监听 IPv6（需重新部署验证 IPv4 仍可访问）。

4. **区分所有路径都 502，还是只有 WebSocket 502**

   在外部执行：

   ```bash
   curl -v https://你的域名/health
   curl -v https://你的域名/time
   ```

   - 所有路径都 502：端口、监听地址或平台代理上游配置问题。
   - `/health` 正常但 `/vless` 502：平台未开启 WebSocket Upgrade，或路径配置不一致。

5. **查看平台代理日志**

   重点找上游错误：

   - `connect() failed (111: Connection refused)`：端口写错或应用只监听回环地址。
   - `upstream timed out`：端口可达但响应超时，检查平台超时和容器资源。
   - `no live upstreams` / unhealthy：平台健康检查失败，确认健康检查路径为 `/health`、端口与应用一致。

6. **确认平台到容器的协议**

   容器默认提供 HTTP。平台应使用 HTTP 访问上游并在边缘终止 TLS；如果平台把上游协议配置成 HTTPS，而容器没有配置证书，会直接 502。

### VLESS 握手成功、目标连接超时

容器日志出现下面两行，说明客户端已经连上 VLESS 服务，UUID、路径、WebSocket 和平台转发都正常：

```text
client <地址> command=1 -> www.google.com:443 ...
WARNING: vless error ... connect target timeout after 10s: www.google.com:443
```

`command=1` 是 TCP 转发请求，后面的地址是客户端要求访问的目标。失败发生在容器向该目标建立出站连接时，与 VLESS 客户端配置无关。

进入容器执行：

```sh
diagnose-pyvless
```

也可以手动检查：

```sh
env | grep -i proxy
getent ahosts www.google.com
getent ahostsv4 www.google.com
ip -6 route
nc -z -w 5 www.google.com 443
curl -4 -sS -o /dev/null -w 'IPv4 %{http_code}\n' https://www.google.com
curl -6 -sS -o /dev/null -w 'IPv6 %{http_code}\n' --max-time 10 https://www.google.com
curl -v --max-time 10 https://www.google.com
curl -v --max-time 10 https://example.com
```

如果 `curl` 成功但 `vs.py` 仍然超时，先看 `curl -v` 是否出现 `Uses proxy`，
以及环境中是否存在 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`。`curl`
会读取这些变量并通过代理访问外网，而 `vs.py` 为目标站点建立的是直连
TCP；代理可用不代表直连可用。容器镜像已启用 Happy Eyeballs，会同时
尝试解析出的 IPv4 和 IPv6 地址，避免不可达的 IPv6 地址耗尽连接超时。

判断方式：

- `curl` 输出 `Uses proxy`：外网访问依赖平台代理，VLESS 直连会被阻断。
- `ip -6 route` 没有 `default`，`curl -4` 成功而 `curl -6` 卡住：容器没有
  IPv6 出网路由。旧版 `vs.py` 会先尝试 DNS 返回的 IPv6 地址并一直等到
  `--connect-timeout`；更新镜像后会在 0.25 秒内并行尝试 IPv4 并回退。
- `getent ahosts` 同时返回 IPv4/IPv6，但 IPv6 直连超时：原因同上。
- DNS 能解析，但 `nc` 显示超时或 `SYN-SENT`：平台的出站 TCP 被阻断。
- `www.google.com` 失败而 `example.com` 正常：服务器网络到 Google 不可达或被限制，不代表 VLESS 整体不可用。
- 所有目标都超时：Cloady 实例没有公网出站权限，或需要配置 NAT/防火墙放行。VLESS 必须能主动连接客户端请求的网站，否则只能换到允许任意出站 TCP 的实例或 VPS。

## 直接使用镜像

GitHub Actions 会把镜像发布到：

```text
ghcr.io/<github-owner>/<github-repo>:latest
```

如果仓库是 `alice/pyvless`，启动命令为：

```bash
docker run -d \
  --name pyvless \
  --restart unless-stopped \
  -p 10086:10086 \
  -e VLESS_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')" \
  -e VLESS_PATH=/vless \
  -e VLESS_LOG_LEVEL=INFO \
  ghcr.io/alice/pyvless:latest
```

私有仓库的 GHCR 镜像需要先登录：

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-username> --password-stdin
```

`GHCR_TOKEN` 需要是一个具有 `read:packages` 权限的 GitHub PAT。

## 不依赖 GitHub Actions 的平台

只要平台支持“从 Git 仓库构建 Dockerfile”，就不需要 Actions。Actions 仅用于提前构建镜像，Dockerfile 本身包含全部运行依赖。

平台通常只需要配置：

```text
VLESS_UUID=<随机 UUID>
VLESS_PATH=/vless
```

只用 Dockerfile 部署时，平台注入的 `PORT` 会自动生效；使用本项目的 `docker-compose.yml` 时端口固定为 `10086`。若平台要求手动指定容器端口，填写 `10086`。

| 平台 | 配置要点 |
| --- | --- |
| Railway / Render / Zeabur | 选择 Dockerfile 部署，设置 `VLESS_UUID`；平台注入的 `PORT` 自动生效 |
| Fly.io | `fly.toml` 的 `internal_port` 设为 `10086`，用 `fly secrets set VLESS_UUID=...` 注入 UUID |
| Koyeb | 选择 Dockerfile，容器端口填 `10086`，设置 `VLESS_UUID` 环境变量 |
| VPS | 使用本项目的 `docker compose up -d --build`，或用 `docker build` + `docker run` |

平台若自带 HTTPS 入口，通常让容器保持 HTTP 模式，由平台终止 TLS；确保平台支持 WebSocket Upgrade，并保持客户端路径与 `VLESS_PATH` 一致。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VLESS_UUID` | 无，必填 | VLESS 用户 UUID |
| `VLESS_HOST` | `0.0.0.0` | 容器内监听地址 |
| `VLESS_PORT` | `10086` | 容器内监听端口；Compose 中固定为 `10086` |
| `VLESS_PATH` | `/vless` | WebSocket 路径 |
| `VLESS_LOG_LEVEL` | `INFO` | `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL` |
| `VLESS_CERT` | 空 | TLS 证书路径，必须与 `VLESS_KEY` 同时设置 |
| `VLESS_KEY` | 空 | TLS 私钥路径，必须与 `VLESS_CERT` 同时设置 |
直接用 `docker run` 且未设置 `VLESS_PORT` 时，也支持平台注入的 `PORT` 变量。

也可以直接传入 `vs.py` 的参数，例如：

```bash
docker run --rm -p 10086:10086 ghcr.io/alice/pyvless:latest \
  --uuid <uuid> --path /vless --log-level INFO
```

显式命令行参数会覆盖环境变量。

## 启用 TLS

把证书放入 `./certs`，取消 `docker-compose.yml` 中 `volumes` 的注释，并在 `environment` 下增加：

```yaml
      VLESS_CERT: "/certs/fullchain.pem"
      VLESS_KEY: "/certs/privkey.pem"
```

然后重启：

```bash
docker compose up -d
```

容器以 UID `10001` 的非 root 用户运行。如果私钥在宿主机上是 `root:root 600`，需要让容器用户可读，例如：

```bash
sudo chown 10001:10001 certs/fullchain.pem certs/privkey.pem
sudo chmod 600 certs/privkey.pem
```

也可以在反向代理（Nginx、Caddy、Traefik）终止 TLS，此时容器只需保持 HTTP 模式，客户端连接反向代理的域名和端口，并保持相同的 WebSocket `path`。

## GitHub 自动发布

1. 把项目推送到 GitHub 的 `main` 或 `master` 分支。
2. 打开仓库的 Actions 页面，确认 `Build and publish Docker image` 成功。
3. 在仓库的 Packages 页面把镜像可见性设为 `Public`（如果需要匿名拉取）。

工作流使用仓库自带的 `GITHUB_TOKEN` 推送到 GHCR，无需额外配置 Secret。每次推送 `main`/`master` 会生成 `latest` 和分支标签，推送 `v1.2.3` 这类 Git tag 会生成版本标签。

## 客户端配置

在 Xray、sing-box、Clash.Meta 或 Hiddify 中填写：

| 项目 | 值 |
| --- | --- |
| 地址 | 服务器域名或 IP |
| 端口 | 映射到宿主机的端口，默认 `10086` |
| UUID | `docker-compose.yml` 中的 `VLESS_UUID` |
| 传输 | WebSocket |
| 路径 | `VLESS_PATH`，默认 `/vless` |
| Host 请求头 | 与地址一致；使用 CDN 时填 CDN 域名 |
| TLS | 直连 TLS 或反向代理时开启 |

若通过 CDN 或反向代理转发，请确保其支持 WebSocket Upgrade，并放行对应路径。

### Cloady / Kubernetes 平台示例

Cloady 会把容器的 `10086` 端口作为 Web/HTTP 端点暴露，因此 `docker-compose.yml` 中必须写成 `10086:10086`，不要写成 `10086:10086/tcp`。重新部署后，先用下面的地址确认平台已经正确路由到容器：

```text
https://<平台分配域名>/health
```

返回 `200` 后，客户端按以下参数连接：

| 项目 | 值 |
| --- | --- |
| 地址 | `<平台分配域名>` |
| 端口 | `443` |
| UUID | 环境变量 `VLESS_UUID` 的值 |
| 传输 | WebSocket |
| 路径 | `/vless` |
| TLS | 开启 |
| SNI / Host | `<平台分配域名>` |

对应的 VLESS URI：

```text
vless://<VLESS_UUID>@<平台分配域名>:443?encryption=none&security=tls&sni=<平台分配域名>&type=ws&host=<平台分配域名>&path=%2Fvless#pyvless
```

不要在 Cloady 的客户端配置中使用 `:10086`；该端口只供平台内部转发。若 `/health` 仍是平台返回的 `404 page not found`，说明端点仍然是 TCP 类型或尚未重新创建，VLESS 也不会连通。

## 安全建议

- 不要继续使用 `vs.py` 中的默认 UUID，部署时务必设置随机 UUID。
- 公网部署建议启用 TLS，并优先使用反向代理管理证书。
- 镜像已经使用非 root 用户、只读根文件系统、丢弃 Linux capabilities，并设置 `no-new-privileges`。
