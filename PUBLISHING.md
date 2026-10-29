# Publishing Local Code Review

[English](#english) | [简体中文](#简体中文)

## English

This repository is distributed as a Git repository used as a local marketplace. The plugin runs on each user's Windows machine. Project code and review caches are not uploaded to the distribution repository or a plugin service.

### Publisher setup

```powershell
git init
git add .
git commit -m "Release Local Code Review 3.3.0"
git branch -M main
git remote add origin <YOUR_GIT_REPOSITORY_URL>
git push -u origin main
git tag v3.3.0
git push origin v3.3.0
```

Before committing:

- Exclude `plugin/.mcp.json`; each user generates it locally.
- Exclude `.code-review/`, `.test-artifacts/`, and `__pycache__/`.
- Keep the version in `plugin/.codex-plugin/plugin.json` consistent with the release tag.
- Add your chosen open-source license and retain third-party notices.
- Test the installer on a Windows environment without the developer's absolute paths.

### User installation

```powershell
git clone <YOUR_GIT_REPOSITORY_URL> LocalCodeReview
cd LocalCodeReview
.\install-plugin.cmd
```

The installer generates `plugin/.mcp.json` inside the clone and runs:

```powershell
python configure_plugin.py
codex plugin marketplace add . --json
codex plugin add local-code-review@local-review-tools --json
```

Users do not need to edit JSON or find absolute paths to Python and the repository. Run the installer again after updating the clone.

### Releasing a version

Update `version` in `plugin/.codex-plugin/plugin.json`, commit, and push a new version tag. Users run `git pull` in their clones and run the installer again. If their Codex version caches an older plugin, remove it through plugin management or the CLI and reinstall it.

### Public listing limitations

`.agents/plugins/marketplace.json` describes the local marketplace entry. Publishing the repository on GitHub, GitLab, or another Git host still requires users to clone it and add the local marketplace. It does not automatically distribute the plugin to all Codex users.

If a public marketplace submission process becomes available, use this repository as the release source and follow that process. Before submitting, check the official plugin documentation for current remote marketplace, signing, review, and versioning requirements.

---

## 简体中文

发布 Local Code Review

这个仓库的发布方式是 Git 仓库作为本地 marketplace。插件运行在每位用户自己的 Windows 机器上；代码和审查缓存不会上传到发布仓库或插件服务。

### 发布者准备

```powershell
git init
git add .
git commit -m "Release Local Code Review 3.3.0"
git branch -M main
git remote add origin <你的 Git 仓库地址>
git push -u origin main
git tag v3.3.0
git push origin v3.3.0
```

提交前确认：

- `plugin/.mcp.json` 不在提交中；它由每位用户本地生成。
- `.code-review/`、`.test-artifacts/` 和 `__pycache__/` 不在提交中。
- `plugin/.codex-plugin/plugin.json` 的版本与发布标签一致。
- 仓库补充你选择的开源许可证和第三方依赖说明。
- 至少在一台没有本机绝对路径的 Windows 环境中运行安装脚本。

### 用户安装

```powershell
git clone <Git 仓库地址>
cd LocalCodeReview
.\install-plugin.cmd
```

安装脚本会在克隆目录生成 `plugin/.mcp.json`，并执行：

```powershell
python configure_plugin.py
codex plugin marketplace add . --json
codex plugin add local-code-review@local-review-tools --json
```

因此用户不需要编辑 JSON，也不需要知道 Python 可执行文件和仓库的绝对路径。更新仓库后重新运行脚本即可。

### 版本发布

修改 `plugin/.codex-plugin/plugin.json` 的 `version`，提交并推送一个新的版本标签。用户在本地克隆目录执行 `git pull` 后重新运行安装脚本。若当前 Codex 版本缓存了旧插件，按 Codex 的插件管理界面或 CLI 移除旧版本后重新安装。

### 公开目录的边界

仓库中的 `.agents/plugins/marketplace.json` 描述了本地 marketplace 的插件条目。把仓库放到 GitHub、GitLab 或其他 Git 服务后，用户仍需主动克隆并添加本地 marketplace；这不会自动把插件推送到所有 Codex 用户。

如果未来 Codex 提供公开 marketplace 提交流程，再将这个仓库作为发布源提交审核。公开提交前应重新核对官方插件文档对远程 marketplace、签名、审核和版本策略的要求。
