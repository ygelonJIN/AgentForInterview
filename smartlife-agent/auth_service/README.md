# SmartLife Auth Service

一个可独立运行、也可挂载到别的 FastAPI 应用上的通用身份服务：账号注册、登录、
JWT 会话、refresh 轮换、改密、角色和审计事件。

它**不知道** SmartLife 的商品、订单或行程；项目相关的用户资料和订单放在
`app/api/` 适配层。这样 `auth_service/` 可以整体搬到别的项目。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/auth/register` | 注册，返回账号信息 |
| POST | `/v1/auth/login` | 登录，返回 access + refresh |
| POST | `/v1/auth/refresh` | 轮换 refresh，返回新的一对 token |
| POST | `/v1/auth/logout` | 撤销 refresh |
| GET | `/v1/auth/me` | 当前账号（需要 Bearer） |
| POST | `/v1/auth/change-password` | 改密并让旧 token 失效 |
| GET | `/v1/health` | 健康检查 |

```bash
curl -s http://127.0.0.1:8000/v1/auth/register \
  -H 'content-type: application/json' \
  -d '{"username":"alice","password":"alice1234"}'

curl -s http://127.0.0.1:8000/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"alice","password":"alice1234"}'
```

错误统一为：

```json
{"error": {"code": "invalid_credentials", "message": "用户名或密码错误"}}
```

## 运行

```bash
cd smartlife-agent
source venv312/bin/activate
uvicorn auth_service.app:create_app --factory --host 127.0.0.1 --port 8000
```

最小配置只需要 `AUTH_JWT_SECRET`。没有设置时会生成
`data/auth_secret.key`（0600）并复用；这个文件必须保留，删掉等于让所有
已签发 token 失效。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `AUTH_DB_PATH` | `data/accounts.db` | 账号库 |
| `AUTH_JWT_SECRET` | 自动生成 | HS256 密钥，生产建议显式设置 |
| `AUTH_SECRET_FILE` | `data/auth_secret.key` | 未设置密钥时的落盘位置 |
| `AUTH_ACCESS_TTL` | `7200` | access token 秒数 |
| `AUTH_REFRESH_TTL` | `604800` | refresh token 秒数 |
| `AUTH_BCRYPT_ROUNDS` | `12` | bcrypt cost |
| `AUTH_ALLOW_REGISTER` | `1` | 是否开放注册 |
| `AUTH_INVITE_CODE` | 空 | 设置后注册必须带邀请码 |
| `AUTH_CORS_ORIGINS` | `http://localhost:8501,http://127.0.0.1:8501` | 允许的前端来源 |
| `AUTH_RATE_LIMIT_ATTEMPTS` | `5` | 窗口内失败次数上限 |
| `AUTH_RATE_LIMIT_WINDOW` | `300` | 限速窗口秒数 |

## 数据与 token

三张表：`accounts`（账号、密码哈希、角色、token_version）、`sessions`
（refresh 哈希、UA、IP、过期、撤销）、`auth_events`（注册/登录/失败/改密/撤销）。

- 密码用 bcrypt（带盐），不存明文。
- access token 是 JWT，带 `sub`/`roles`/`scopes`/`ver`/`jti`/`exp`。
- refresh 只存 SHA-256 哈希；每次刷新轮换，旧 refresh 立即作废。
- 改密把 `token_version` +1，旧 access 立即失效。
- 登录失败按 IP 和用户名双计数，超限返回 429。
- 登录失败不区分“账号不存在”和“密码错误”。

## 复用

### 1. 整目录复制

把 `auth_service/` 复制到新项目，设置 `AUTH_DB_PATH`、`AUTH_JWT_SECRET`、
`AUTH_CORS_ORIGINS` 即可。新项目只需要自己写登录页和“账号 → 业务用户”的映射。

### 2. 安装成包

```bash
pip install ./auth_service
# 或者
pip install -e ./auth_service
```

然后 `uvicorn auth_service.app:create_app --factory --port 8000`。

### 3. 挂到已有 FastAPI

```python
from auth_service.app import create_app

app.mount("/auth", create_app())
```

### 4. Docker

在仓库根目录：

```bash
docker build -f auth_service/Dockerfile -t smartlife-auth .
docker run --rm -p 8000:8000 -v auth-data:/data smartlife-auth
```

### 其他项目要改什么

- 登录/注册页面：每个前端框架各写一套；
- 账号 id 到业务用户表的映射：像本项目 `app/api/me.py` 那样写一个适配层；
- 角色和页面权限：按项目需要决定 `admin` 等角色；
- CORS 来源、邀请码、数据库路径。

### 不用改什么

接口路径与字段、密码策略、token 生命周期、refresh 轮换、会话撤销、审计、测试。

## 限制

- 不是 OIDC/OAuth2：没有 Google/微信/企业 SSO。需要这些时换 Keycloak，
  客户端契约也要跟着换。
- 不做邮箱验证、自助找回密码（需要 SMTP）；忘记密码目前只能由管理员直接改库。
- 默认 SQLite，适合单机；高并发要换 Postgres（SQL 都收在 `repository.py`）。
- 限速是进程内计数，多实例部署要换 Redis。

## 测试

```bash
venv312/bin/python -m pytest auth_service/tests -q
```

`tests/test_no_project_imports.py` 会阻止 `auth_service/` 反向依赖本项目或
Streamlit——这是“可复用”的硬约束，不靠自觉。
