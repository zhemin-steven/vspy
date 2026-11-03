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
| `.env.example` | 环境变量示例 |

## 快速启动

1. 生成 UUID：

   ```bash
   python3 -c "import uuid; print(uuid.uuid4())"
   ```

2. 创建配置：

   ```bash
   cp .env.example .env
   ```

   编辑 `.env`，把 `VLESS_UUID` 替换为上一步生成的 UUID。

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

服务默认监听容器内的 `10086` 端口，WebSocket 路径默认为 `/vless`。`HOST_PORT` 只改变宿主机映射端口。

如果部署平台注入了 `PORT` 环境变量（Railway、Render、Zeabur 等常见行为），入口脚本会自动使用该端口；显式设置 `VLESS_PORT` 时以 `VLESS_PORT` 为准。

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

平台注入 `PORT` 时会自动使用该端口。若平台要求手动指定容器端口，填写 `10086`。

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
| `VLESS_PORT` | `10086` | 容器内监听端口，健康检查也使用该值 |
| `VLESS_PATH` | `/vless` | WebSocket 路径 |
| `VLESS_LOG_LEVEL` | `INFO` | `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL` |
| `VLESS_CERT` | 空 | TLS 证书路径，必须与 `VLESS_KEY` 同时设置 |
| `VLESS_KEY` | 空 | TLS 私钥路径，必须与 `VLESS_CERT` 同时设置 |
| `HOST_PORT` | `10086` | Compose 暴露到宿主机的端口 |

未设置 `VLESS_PORT` 时，也支持平台注入的 `PORT` 变量。

也可以直接传入 `vs.py` 的参数，例如：

```bash
docker run --rm -p 10086:10086 ghcr.io/alice/pyvless:latest \
  --uuid <uuid> --path /vless --log-level INFO
```

显式命令行参数会覆盖环境变量。

## 启用 TLS

把证书放入 `./certs`，在 `.env` 中设置：

```dotenv
VLESS_CERT=/certs/fullchain.pem
VLESS_KEY=/certs/privkey.pem
```

然后取消 `docker-compose.yml` 中 `volumes` 的注释并重启：

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
| UUID | `.env` 中的 `VLESS_UUID` |
| 传输 | WebSocket |
| 路径 | `VLESS_PATH`，默认 `/vless` |
| Host 请求头 | 与地址一致；使用 CDN 时填 CDN 域名 |
| TLS | 直连 TLS 或反向代理时开启 |

若通过 CDN 或反向代理转发，请确保其支持 WebSocket Upgrade，并放行对应路径。

## 安全建议

- 不要继续使用 `vs.py` 中的默认 UUID，部署时务必设置随机 UUID。
- 公网部署建议启用 TLS，并优先使用反向代理管理证书。
- 镜像已经使用非 root 用户、只读根文件系统、丢弃 Linux capabilities，并设置 `no-new-privileges`。
