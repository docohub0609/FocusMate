# 第三方依赖说明

MIT 许可适用于 FocusMate 原创代码，不改变第三方软件的许可。此仓库不分发 `node_modules`、Python 环境或 Docker 基础镜像；安装和构建时从各自分发渠道获取依赖。

以下根据发布验证时的包元数据整理，具体版本见 `package-lock.json` 和 Python 安装元数据：

| 依赖 | 许可 |
| --- | --- |
| React、React DOM、React Router、TanStack Query | MIT |
| CodeMirror、Lezer | MIT |
| Vite、Vite React 插件、Rolldown | MIT |
| TypeScript、Playwright | Apache-2.0 |
| jsonschema、PyYAML、markdown-it-py、pytest | MIT |
| Docker SDK for Python | Apache-2.0 |
| pywin32（Windows） | PSF |
| source-map-js（间接依赖） | BSD-3-Clause |
| Lightning CSS（构建依赖） | MPL-2.0 |
| picocolors（间接依赖） | ISC |

完整许可正文随安装包提供，应在再分发相关包或构建产物时一并保留适用的版权和许可声明。Dockerfile 使用官方 Python/Debian 基础镜像，镜像内各组件遵循各自许可。DeepSeek 为外部 API 服务，使用者自行配置账号并遵守服务条款。
