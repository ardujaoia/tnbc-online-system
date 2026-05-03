# 智破三阴 TNBC 联网平台版

这个新系统的页面结构按系统1重做，包含工作台、多模态数据管理、新患者上传、化疗敏感性预测、三阴性识别、治疗方案推荐、知识图谱、模型可解释性、历史病例与报告、系统设置等模块。

区别是：它不读取 `E:\TNBC_Project` 的本地数据，不依赖系统1的本机模型权重。它通过后端代理实时访问公开数据库接口：

- GDC API: `https://api.gdc.cancer.gov`
- cBioPortal API: `https://www.cbioportal.org/api`

## 本机运行

双击：

```text
start_online_system.bat
```

或手动运行：

```powershell
D:\anaconda\python.exe scripts\serve_online_system.py --host 127.0.0.1 --port 8020
```

浏览器打开：

```text
http://127.0.0.1:8020/
```

## 适合发给别人吗？

可以。别人电脑上只需要：

1. 有 Python；
2. 能访问 GDC / cBioPortal；
3. 解压整个 `TNBC_Online_System` 文件夹后双击启动脚本。

如果你想真正做到“别人不用装 Python，直接访问网址”，下一步应该把这个目录部署到云服务器或 Render/Fly.io/Railway 这类平台。

## 公网部署

已内置 Render 部署配置：

- `render.yaml`
- `requirements.txt`
- `Procfile`
- `Dockerfile`

推荐先用 Render 部署，具体步骤见：

```text
DEPLOY_RENDER.md
```

部署成功后，别人只需要打开 Render 给你的 `https://xxx.onrender.com` 地址即可。

## 边界说明

这个联网平台版展示的是“系统1式完整平台外观 + 联网取公开数据 + 在线交互 + 轻量评分演示”。它不会自动下载超大的 WSI/MRI 原始数据，也不会在浏览器里训练完整深度学习模型。系统1仍然是完整科研训练版；这个系统是适合外发、答辩和部署的公网展示版。
