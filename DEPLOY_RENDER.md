# Render 公网部署步骤

目标：让别人不用安装任何东西，直接打开一个 `https://xxx.onrender.com` 网址访问系统。

## 1. 上传到 GitHub

把整个 `TNBC_Online_System` 文件夹上传为一个 GitHub 仓库，例如：

```text
tnbc-online-system
```

仓库根目录里应当能看到：

```text
frontend/
scripts/
render.yaml
requirements.txt
Procfile
start_online_system.bat
README.md
```

## 2. 在 Render 创建 Web Service

1. 登录 https://render.com
2. 点击 `New +`
3. 选择 `Web Service`
4. 连接刚刚上传的 GitHub 仓库
5. Render 会读取仓库里的 `render.yaml`

如果手动填写，配置如下：

```text
Runtime: Python
Build Command: pip install -r requirements.txt
Start Command: python scripts/serve_online_system.py --host 0.0.0.0
```

Render 会自动提供 `PORT` 环境变量，本系统已经适配。

## 3. 部署完成后

部署成功后 Render 会给你一个公网地址，例如：

```text
https://tnbc-online-system.onrender.com
```

别人只需要打开这个网址就能使用，不需要安装 Python，也不需要你电脑上的 E 盘数据。

## 4. 系统边界

这个云部署版读取的是公开 API：

- GDC API
- cBioPortal API

它不会读取你的本地 `E:\TNBC_Project`，也不会把旧系统的大数据文件上传到云端。
