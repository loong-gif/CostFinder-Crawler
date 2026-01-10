# 上传代码到 GitHub 的步骤

## 方法一：使用 Git 命令行（推荐）

如果您的系统已安装 Git，请按照以下步骤操作：

### 1. 初始化 Git 仓库
```bash
git init
```

### 2. 添加远程仓库
```bash
git remote add origin https://github.com/loong-gif/CostFinder-Crawler.git
```

### 3. 添加所有文件
```bash
git add .
```

### 4. 提交更改
```bash
git commit -m "Initial commit: Social Media Finder and Cost Finder Crawler"
```

### 5. 设置主分支（如果需要）
```bash
git branch -M main
```

### 6. 推送到 GitHub
```bash
git push -u origin main
```

如果遇到认证问题，您可能需要：
- 使用 Personal Access Token 代替密码
- 或者配置 SSH 密钥

## 方法二：使用 GitHub Desktop

1. 下载并安装 [GitHub Desktop](https://desktop.github.com/)
2. 登录您的 GitHub 账户
3. 点击 "File" -> "Add Local Repository"
4. 选择项目目录 `K:\Project\Social_Media_Finder`
5. 点击 "Publish repository" 并选择 `loong-gif/CostFinder-Crawler`

## 方法三：使用 GitHub Web 界面

1. 访问 https://github.com/loong-gif/CostFinder-Crawler
2. 点击 "uploading an existing file"
3. 拖拽项目文件到浏览器
4. 提交更改

## 注意事项

- 确保 `.gitignore` 文件已正确配置，避免上传不必要的文件（如 `venv/`, `__pycache__/` 等）
- 如果仓库已存在内容，可能需要先拉取：`git pull origin main --allow-unrelated-histories`
- 确保您有该仓库的写入权限
